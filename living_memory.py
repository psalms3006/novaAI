"""
living_memory.py
════════════════
Living Memory System — persistent, structured, self-editing memory.

This is an additive layer over the existing FAISS semantic store (memory_extra.py).
It keeps its OWN canonical structured store (records.json) and mirrors plain-text
facts back into the legacy FAISS store so existing retrieval / /memory endpoint /
get_all_memory_text keep working.

Features (per NOVA memory spec):
  • memory types: fact / preference / instruction / decision / event / knowledge
                   / project / task / goal / constraint / correction
  • importance scoring (0..1) + confidence + source + timestamps + access counts
  • self-editing: contradiction detection supersedes (versioned), never blind-append
  • version history per superseded fact
  • explicit commands: remember / forget / update / search / stats (highest priority)
  • hybrid retrieval: lexical + optional semantic(FAISS) + importance + recency
  • structured context injection for the system prompt
  • safety: never auto-store secrets (API keys / passwords / tokens)
  • decay + consolidation + observability

Designed NOT to import nova at module top (only lazily), so it can be imported and
unit-tested standalone with injected dependencies.
"""
from __future__ import annotations
import json, os, re, time, threading, uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

__all__ = ["LivingMemory", "get_living_memory", "init_living_memory"]

MEMORY_TYPES = [
    "fact", "preference", "instruction", "decision", "event", "knowledge",
    "project", "task", "goal", "constraint", "correction",
]

DEFAULT_RECORDS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nova_memory_store", "records.json"
)

# ── Sensitive-info guard ───────────────────────────────────────────────────────
_SECRET_PATTERNS = [
    re.compile(r"\b(api[_-]?key|apikey)\b", re.I),
    re.compile(r"\b(secret|password|passwd|pwd)\b", re.I),
    re.compile(r"\btoken[s]?\b", re.I),
    re.compile(r"\b(bearer)\b", re.I),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\b(ak|sk|access|secret)[_-](key|token)[_-]?[A-Za-z0-9]{6,}\b", re.I),
    re.compile(r"\b[0-9]{16,19}\b"),  # long numeric identifiers
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),  # long base64-ish blobs
]

# ── Lexical classification keywords ────────────────────────────────────────────
_TYPE_KEYWORDS: Dict[str, List[str]] = {
    "preference": ["prefer", "prefers", "like", "likes", "love", "favorite",
                   "favourite", "hate", "dislike", "usually", "rather", "in style"],
    "instruction": ["always ", "never ", "make sure", "remember to", "don't",
                    "do not ", "must ", "should ", "required to", "no matter what"],
    "project": ["project", "building", "build ", "working on", "developing",
                "github", "repo", "repository", "architecture", "roadmap", "app "],
    "decision": ["decided", "decide", "deciding", "going with", "chose", "chosen",
                 "settled on", "final call", "we'll use", "we will use"],
    "goal": ["goal", "want to", "plan to", "aim ", "trying to", "objective",
             "target", "aspire", "going to build", "striving"],
    "constraint": ["can't", "cannot", "must not", "won't work", "doesn't support",
                   "limited to", "restricted", "constraint", "incompatible"],
    "event": ["happened", "today", "yesterday", "last night", "went to", "we met",
              "occurred", "recap", "progress update"],
    "knowledge": ["is a", "are a", "means", "refers to", "is used for", "concept",
                  "definition", "difference between", "explain"],
    "task": ["task", "todo", "reminder", "deadline", "due ", "to-do"],
    "correction": ["no longer", "that's wrong", "actually ", "actually,",
                   "correction", "i was wrong", "has changed"],
}
_FALLBACK_TYPE = "fact"

# keywords that raise importance
_HIGH_IMPORTANCE = ["remember", "important", "confirmed", "decided", "preferred",
                    "never", "always", "my name", "must", "critical", "final"]
_LOW_IMPORTANCE = ["maybe", "perhaps", "i think", "not sure", "just chatting",
                   "by the way", "anyway", "hmm", "whatever"]

# value-dimension vocabulary used for contradiction detection: two facts that
# share a subject noun but use DIFFERENT words from this cluster disagree.
_CLUSTER_WORDS = {
    "private", "public", "open", "closed", "proprietary", "confidential",
    "paid", "free", "local", "cloud", "remote", "online", "offline",
    "supabase", "sqlite", "postgres", "mysql", "mongodb", "postgresql",
    "fast", "slow", "large", "small", "new", "old",
}


class LivingMemory:
    """Persistent, structured, self-editing memory store."""

    def __init__(
        self,
        path: str = DEFAULT_RECORDS_PATH,
        search_fn: Optional[Callable[[str, int], List[str]]] = None,
        embedder: Any = None,
        top_k: int = 5,
        mirror: bool = True,
    ) -> None:
        self.path = path
        self.search_fn = search_fn          # semantic(non-structured) search
        self.embedder = embedder            # optional numpy encoder -> vector
        self.top_k = top_k
        self.mirror = mirror                # mirror facts into legacy FAISS store
        self._lock = threading.RLock()
        self._records: List[Dict[str, Any]] = []
        self._metrics = {
            "stored": 0, "updated": 0, "deleted": 0, "superseded": 0,
            "retrieval_count": 0, "last_consolidation": 0.0,
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._records = data if isinstance(data, list) else []
            except Exception:
                self._records = []

    def _save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._records, f, indent=2)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except Exception:
                    pass

    # ── classify + importance ────────────────────────────────────────────────
    def classify(self, text: str) -> Dict[str, Any]:
        low = text.lower()
        best_type, best_hits = _FALLBACK_TYPE, -1
        for t, kws in _TYPE_KEYWORDS.items():
            hits = sum(1 for k in kws if k in low)
            if hits > best_hits:
                best_type, best_hits = t, hits
        score = 0.5
        if any(k in low for k in _HIGH_IMPORTANCE):
            score += 0.2
        if any(k in low for k in _LOW_IMPORTANCE):
            score -= 0.15
        if best_type == "preference":
            score += 0.1
        if any(p in low for p in ("project", "architect", "build", "robotics",
                                  "mechatron", "user studies")):
            score += 0.05
        if "my " in low or "i " in low:
            score += 0.05
        return {"type": best_type, "importance": round(max(0.0, min(1.0, score)), 3)}

    @staticmethod
    def is_sensitive(text: str) -> bool:
        return any(p.search(text) for p in _SECRET_PATTERNS)

    # ── record ops ───────────────────────────────────────────────────────────
    def _project_of(self, text: str) -> str:
        for p in ("ORIN", "KIWI", "VYREN", "ARVO", "NOVA", "OMNIEL", "KIWI2"):
            if p.lower() in text.lower():
                return p.title()
        return ""

    def remember(self, text: str, source: str = "explicit",
                 project: str = "", confirmed: bool = True,
                 importance: Optional[float] = None) -> Dict[str, Any]:
        """Explicit command — highest priority, confirmed, strongly stored."""
        text = (text or "").strip()
        if not text:
            raise ValueError("nothing to remember")
        if self.is_sensitive(text):
            raise ValueError("refusing to store what looks like a secret (key/password)")
        return self._upsert(text, type="fact", source=source, confirmed=confirmed,
                            project=project or self._project_of(text),
                            importance=(importance if importance is not None else 0.9),
                            explicit=True)

    def _upsert(self, text: str, type: str, source: str, confirmed: bool,
                project: str, importance: float, explicit: bool) -> Dict[str, Any]:
        with self._lock:
            existing = self._find_exact(text)
            if existing:
                existing["updated"] = time.time()
                existing["access_count"] = existing.get("access_count", 0)
                existing["confirmed"] = existing["confirmed"] or confirmed
                if importance > existing.get("importance", 0):
                    existing["importance"] = round(importance, 3)
                existing["type"] = existing.get("type") or type
                self._metrics["updated"] += 1
                self._save()
                return existing

            conflict = self._detect_conflict(text, project)
            if conflict is not None and (explicit or importance >= conflict.get("importance", 0)):
                # supersede the old conflicting fact (version it) — self-edit
                self._supersede(conflict, text)
                self._metrics["superseded"] += 1

            rec = {
                "id": f"mem_{uuid.uuid4().hex[:12]}",
                "text": text[:2000],
                "type": type,
                "importance": round(max(0.0, min(1.0, importance)), 3),
                "confidence": 1.0 if confirmed else 0.6,
                "source": source,
                "project": project,
                "confirmed": bool(confirmed),
                "created": time.time(),
                "updated": time.time(),
                "last_accessed": time.time(),
                "access_count": 0,
                "decay": {"active": False, "expires": None},
                "superseded_by": None,
                "versions": [],
                "meta": {},
            }
            self._records.append(rec)
            self._metrics["stored"] += 1
            # mirror plain text into legacy FAISS semantic store (best-effort)
            self._mirror(rec["text"])
            self._save()
            return rec

    def _mirror(self, text: str) -> None:
        if not self.mirror:
            return
        try:
            import nova_state
            from memory_extra import add_memory_fact
            if nova_state._embedder is not None:
                add_memory_fact(text, {})
        except Exception:
            pass

    def _find_exact(self, text: str) -> Optional[Dict[str, Any]]:
        for r in self._records:
            if r.get("text") == text and not r.get("superseded_by"):
                return r
        return None

    def _detect_conflict(self, text: str, project: str) -> Optional[Dict[str, Any]]:
        """Find a live record that the new fact contradicts.

        Heuristic: when the new statement is a correction, contradictions are
        expressed in its LEADING clause ("ORIN is now private. KIWI will be ...").
        A candidate is a true conflict only if it 1) shares the leading clause's
        SUBJECT noun but 2) disagrees on a VALUE dimension (uses a different word
        from the polarity/attribute cluster). Candidates that already agree on a
        value dimension are treated as consistent and never superseded.
        """
        low = text.lower()
        is_correction = any(k in low for k in
                            ("no longer", "isn't", "is not", "not anymore",
                             "actually", "changed", "now", "instead", "correction"))
        if not is_correction:
            return None
        # leading clause — the unit that usually contains the correction
        lead = re.split(r"[.!,;:]", low)[0]
        lead_words = set(re.sub(r"[^a-z0-9 ]+", " ", lead).split())
        lead_cluster = lead_words & _CLUSTER_WORDS
        stop = {"a", "an", "the", "is", "are", "was", "were", "of", "to", "in",
                "on", "at", "and", "or", "but", "now", "being", "be"}
        subjects = (lead_words - lead_cluster) - stop
        if not subjects:
            return None
        best: Optional[Dict[str, Any]] = None
        best_overlap: Optional[int] = None
        for r in self._records:
            if r.get("superseded_by"):
                continue
            if project and r.get("project") and project != r.get("project"):
                continue
            r_words = set(re.sub(r"[^a-z0-9 ]+", " ", r["text"].lower()).split())
            if not (subjects & r_words):       # must share a leading-subject noun
                continue
            r_cluster = r_words & _CLUSTER_WORDS
            if lead_cluster and (r_cluster & lead_cluster):
                continue                        # already agrees on the value → consistent
            overlap = len(r_words & lead_words)
            if best_overlap is None or overlap < best_overlap:
                best, best_overlap = r, overlap
        return best

    def _supersede(self, old: Dict[str, Any], new_text: str) -> None:
        version = dict(old)
        version.pop("versions", None)
        old.setdefault("versions", []).append(version)
        old["decay"] = {"active": True, "expires": time.time() + 86400 * 30}
        old["superseded_by"] = new_text[:120]
        old["updated"] = time.time()
        self._metrics["deleted"] = 0  # versioned, not deleted

    def forget(self, predicate: str) -> int:
        """Delete all live records matching a text/subject (explicit command)."""
        with self._lock:
            low = predicate.lower()
            keep = []
            n = 0
            for r in self._records:
                if r.get("superseded_by"):
                    keep.append(r)
                    continue
                if low in r["text"].lower():
                    r["decay"] = {"active": True, "expires": time.time()}
                    r["confirmed"] = False
                    n += 1
                    keep.append(r)
                else:
                    keep.append(r)
            # hard-remove invented/dead ones only for exact "forget everything"
            if predicate.strip().lower() in ("everything", "all", "*"):
                self._records = [r for r in self._records if r.get("superseded_by")]
                n = len(self._records) + n
            self._metrics["deleted"] += n
            self._save()
            return n

    def all_records(self) -> List[Dict[str, Any]]:
        """Live snapshot of every stored record (superseded/decayed included)."""
        with self._lock:
            return [dict(r) for r in self._records]

    def delete_record(self, text: str, updated: float) -> bool:
        """Hard-delete a single record by its exact text + timestamp pair."""
        with self._lock:
            for i, r in enumerate(self._records):
                if r.get("text") == text and abs(float(r.get("updated", 0)) - float(updated)) < 1e-3:
                    self._records.pop(i)
                    self._metrics["deleted"] += 1
                    self._save()
                    return True
        return False

    # ── hybrid retrieval ─────────────────────────────────────────────────────
    def search(self, query: str, top_k: Optional[int] = None,
               project: Optional[str] = None,
               types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        with self._lock:
            self._metrics["retrieval_count"] += 1
            k = top_k or self.top_k
            low = query.lower()
            semantic_hits: List[str] = []
            if self.search_fn is not None:
                try:
                    semantic_hits = self.search_fn(query, k * 3) or []
                except Exception:
                    semantic_hits = []
            sem_texts = {s.lower() for s in semantic_hits}
            scored = []
            now = time.time()
            for r in self._records:
                if r.get("superseded_by") or r.get("decay", {}).get("active"):
                    continue
                if project and r.get("project") and project.lower() not in r["project"].lower():
                    continue
                if types and r.get("type") not in types:
                    continue
                text_low = r["text"].lower()
                score = 0.0
                if query and (query.lower() in text_low or all(
                        w in text_low for w in query.lower().split() if w)):
                    score = max(score, 0.55)  # strong lexical
                tokens = re.findall(r"\w+", low)
                overlap = sum(1 for t in tokens if t in text_low)
                if overlap:
                    score = max(score, min(0.5, 0.1 + overlap * 0.05))
                score += r.get("importance", 0.5) * 0.3
                # recency + access bonus
                recency = max(0.0, 1.0 - (now - r.get("updated", now)) / (86400 * 14))
                score += recency * 0.1 + min(r.get("access_count", 0) / 20, 1) * 0.05
                if text_low in sem_texts:
                    score += 0.2
                if score <= 0:
                    continue
                r["access_count"] = r.get("access_count", 0) + 1
                r["last_accessed"] = now
                scored.append((round(score, 3), r))
            scored.sort(key=lambda x: x[0], reverse=True)
            top = [r for _, r in scored[:k]]
            if top:
                self._save()
            return top

    def get(self, mem_id: str) -> Optional[Dict[str, Any]]:
        for r in self._records:
            if r.get("id") == mem_id:
                return r
        return None

    def history(self, mem_id: str) -> List[Dict[str, Any]]:
        r = self.get(mem_id)
        return r.get("versions", []) if r else []

    def all(self) -> List[Dict[str, Any]]:
        return [r for r in self._records if not r.get("superseded_by")]

    # ── context injection ────────────────────────────────────────────────────
    def build_context(self, query: str = "", project: Optional[str] = None,
                      top_k: Optional[int] = None) -> str:
        results = self.search(query, top_k=top_k, project=project) if query else \
            [r for r in self._records if not r.get("superseded_by")][:self.top_k]
        if not results:
            return ""
        lines = []
        for r in results:
            prefix = ""
            if r.get("type") == "preference":
                prefix = "Preference: "
            elif r.get("type") == "instruction":
                prefix = "Instruction: "
            elif r.get("type") == "project":
                prefix = "Project: "
            elif r.get("type") == "decision":
                prefix = "Decision: "
            elif r.get("type") == "goal":
                prefix = "Goal: "
            tag = "[confirmed]" if r.get("confirmed") else "[recalled]"
            lines.append(f"- {tag} {prefix}{r['text']}")
        return "\n".join(lines) if lines else ""

    def summarize(self) -> str:
        """Inspection — what do you remember about X / about me."""
        parts = []
        k = {
            "Project": "project", "Preference": "preference", "Decision": "decision",
            "Goal": "goal", "Instruction": "instruction", "Fact": "fact",
        }
        for label, t in k.items():
            rs = [r for r in self.all() if r.get("type") == t][:5]
            if rs:
                parts.append(f"{label}s:\n" + "\n".join(
                    f"  • {r['text']} [{'confirmed' if r.get('confirmed') else 'recalled'}]"
                    for r in rs))
        if not parts:
            return "I don't have detailed memories stored about that yet."
        return "\n".join(parts) + f"\n\n({self.count()} memories stored)"

    def count(self) -> int:
        return sum(1 for r in self._records if not r.get("superseded_by"))

    def stats(self) -> Dict[str, Any]:
        return dict(self._metrics, active=sum(1 for r in self.all()))

    # ── auto-extraction from turns + explicit commands ──────────────────────
    def on_turn(self, user_msg: str, ai_reply: str, meta: Optional[dict] = None,
                store_task: bool = False) -> int:
        """Cheap heuristic extraction after each turn. Stores only ACTIONABLE
        facts (explicit memory verbs, corrections, stable projects/preferences).
        NEVER blocks and NEVER stores small talk. Returns count stored."""
        meta = meta or {}
        stored = 0
        combined = (user_msg + " " + ai_reply).strip()
        if not combined:
            return 0
        if self.is_sensitive(combined):
            return 0
        low = combined.lower()

        # explicit memory verbs — treat as commands (highest priority)
        for verb in ("remember", "don't forget", "do not forget", "note that",
                     "keep in mind"):
            if verb in low and user_msg.strip():
                m = re.search(r"\bremember[^\n.]{0,200}\.?", low)
                candidate = (m.group(0) if m else user_msg)[:300] or user_msg[:300]
                try:
                    self.remember(candidate[0:1].upper() + candidate[1:] if candidate else candidate,
                                  source="explicit")
                    stored += 1
                except Exception:
                    pass

        # detection of "X is no longer Y" corrections inside a reply
        if re.search(r"\b(no longer|isn't|is not|has changed|instead)\b", low):
            m = re.search(r"[A-Za-z][^.!?]{5,200}", low)
            try:
                self._upsert(m.group(0).strip()[:300] if m else combined[:300],
                             type="correction",
                             source="ai" if ai_reply else "user",
                             confirmed=False, project=self._project_of(combined),
                             importance=0.6, explicit=False)
                stored += 1
            except Exception:
                pass

        # stable project statements ("I'm building X", "my project is X")
        for pat in (r"\b(?:i'?m|i am|we'?re|we are)\s+(?:building|making|developing|working on)\s+(\w[\w-]{1,40})",
                    r"\bmy\s+project\s+(?:is|called|named)\s+(\w[\w-]{1,40})"):
            if store_task:
                break
            m = re.search(pat, low)
            if m and not self._find_exact_low(f"{m.group(1).lower()}"):
                self._upsert(f"User's project: {m.group(1).title()}",
                             type="project", source="user", confirmed=False,
                             project=m.group(1).title(), importance=0.7, explicit=False)
                stored += 1

        # personal facts the user states ("I study X", "I prefer X", "my name is X")
        for pat, t in (
            (r"\bmy name is ([A-Za-z][\w -]{1,40})", "fact"),
            (r"\bI (?:study|work|live|go to|attend) ([^.!?]{2,80})", "fact"),
            (r"\bI prefer ([^.!?]{2,80})", "preference"),
            (r"\bI (?:am|'m) (?:from|based in) ([^.!?]{2,60})", "fact"),
        ):
            m = re.search(pat, user_msg, re.I)
            if m and not self._find_exact_low(m.group(0).lower()):
                try:
                    self._upsert(m.group(0).strip()[:300], type=t, source="user",
                                 confirmed=True if t == "preference" else False,
                                 project=self._project_of(combined),
                                 importance=0.75 if t == "preference" else 0.7,
                                 explicit=False)
                    stored += 1
                except Exception:
                    pass
        return stored

    def _find_exact_low(self, low_text: str) -> Optional[Dict[str, Any]]:
        for r in self._records:
            if not r.get("superseded_by") and low_text in r["text"].lower():
                return r
        return None

    def exec_command(self, cmd: str, text: str = "", project: str = "") -> str:
        """Explicit user memory commands (priority over everything else)."""
        c = (cmd or "").strip().lower()
        if c in ("remember", "rememberthis", "add"):
            rec = self.remember(text, project=project)
            return f"OK, I'll remember: {rec['text']}"
        if c in ("forget", "delete", "remove"):
            n = self.forget(text)
            return f"Forgot {n} matching memory/ies."
        if c in ("update", "correct"):
            rec = self._upsert(text, type="correction", source="explicit",
                               confirmed=True, project=project,
                               importance=0.9, explicit=True)
            return f"Updated memory: {rec['text']}"
        if c in ("search", "find", "recall", "what"):
            results = self.search(text, project=project or None)
            if not results:
                return ("I don't have that stored." if c in ("search", "find")
                        else self.summarize())
            return "\n".join(f"• [{'confirmed' if r.get('confirmed') else 'recalled'}] "
                             f"{r['text']}" for r in results)
        if c in ("stats", "count", "status"):
            s = self.stats()
            return (f"Memory store: {s['active']} active, {s['superseded']} superseded, "
                    f"{s['updated']} updated, {s['deleted']} decayed/deleted. "
                    f"Last consolidation: {s['last_consolidation']:.0f}s.")
        raise ValueError(f"unknown memory command: {cmd}")

    # ── maintenance ──────────────────────────────────────────────────────────
    def consolidate(self) -> int:
        """Merge exact-duplicate live records, drop decayed ones. Returns removed."""
        with self._lock:
            removed = 0
            by_text: Dict[str, Dict[str, Any]] = {}
            keep: List[Dict[str, Any]] = []
            now = time.time()
            for r in self._records:
                if r.get("superseded_by"):
                    keep.append(r)
                    continue
                if r.get("decay", {}).get("active") and \
                        now > (r.get("decay", {}).get("expires") or 0):
                    self._metrics["deleted"] += 1
                    removed += 1
                    continue
                if r["text"] in by_text:
                    keep[by_text[r["text"]].get("_idx")]["access_count"] = \
                        keep[by_text[r["text"]]["_idx"]].get("access_count", 0) + \
                        r.get("access_count", 0)
                    removed += 1
                else:
                    r["_idx"] = len(keep)
                    keep.append(r)
                    by_text[r["text"]] = r
            for r in keep:
                r.pop("_idx", None)
            self._records = keep
            self._metrics["last_consolidation"] = time.time()
            self._save()
            return removed

    start_maintenance: Any = None  # set below


def _maintenance_loop(mem: LivingMemory, interval: float) -> None:
    while True:
        time.sleep(interval)
        try:
            mem.consolidate()
        except Exception:
            pass


def start_maintenance(mem: LivingMemory, interval: float = 3600) -> threading.Thread:
    t = threading.Thread(target=_maintenance_loop, args=(mem, interval), daemon=True,
                         name="NOVALivingMemory")
    t.start()
    return t


LivingMemory.start_maintenance = start_maintenance

# ── module singleton ──────────────────────────────────────────────────────────
_singleton: Optional[LivingMemory] = None


def init_living_memory(path: Optional[str] = None, search_fn=None,
                       embedder=None, start_maintenance_loop: bool = True,
                       top_k: int = 5, mirror: bool = True) -> LivingMemory:
    global _singleton
    _singleton = LivingMemory(path=path if path else DEFAULT_RECORDS_PATH,
                              search_fn=search_fn,
                              embedder=embedder, top_k=top_k, mirror=mirror)
    if start_maintenance_loop:
        start_maintenance(_singleton)
    return _singleton


def get_living_memory() -> Optional[LivingMemory]:
    return _singleton