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

#: Who a record with no subject is about.
#:
#: Every record written before memory knew about people is one of these. They
#: were all written on a single-user machine about its owner, so attributing
#: them to the owner is not a guess -- it is what they already meant. The
#: alternative, leaving them unattributed, would hide them from the one person
#: they describe the moment retrieval started filtering by subject.
DEFAULT_SUBJECT = "owner"

# ── transient observations ────────────────────────────────────────────────────
#
# What is on the screen right now is not a fact about the user. NOVA stored
# this, explicitly, confirmed, at confidence 1.0:
#
#     "User opened setup screen, NOVA Cloud, own API key, or work offline
#      prompt is visible"
#
# It was true for about four seconds. It is now a permanent, confident belief
# about someone, sitting alongside their name and date of birth and competing
# with them in retrieval. Screen awareness makes this failure easy to reach:
# the model sees a window, decides it has learned something, and writes it
# down for ever.
_TRANSIENT_PATTERNS = [
    re.compile(r"\bis (?:currently )?(?:visible|displayed|shown|showing|open)\b", re.I),
    re.compile(r"\b(?:on|in) (?:the )?(?:screen|display)\b", re.I),
    re.compile(r"\bscreen (?:shows|displays|currently)\b", re.I),
    re.compile(r"\b(?:dialog|prompt|window|popup|modal|tab) is\b", re.I),
    re.compile(r"\b(?:right now|at the moment|just now|at present)\b", re.I),
    re.compile(r"\buser (?:opened|clicked|is viewing|is looking at)\b", re.I),
]

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


# ── claims: "<subject> is <value>" ────────────────────────────────────────────
#
# Contradiction detection used to require a correction word — "actually", "no
# longer", "now". That is how a person phrases a correction when they know
# they are correcting something, and it is not how they usually speak. Stating
# the same thing differently is far more common, and it was never caught:
#
#     "Project NOVA team's company name is OMNIEL"
#     "Project NOVA team's company name is Omnia."
#
# Both were stored, both confirmed, both at confidence 1.0, and retrieval then
# returned whichever was closest to the question. Two confident answers to one
# question is worse than none, because nothing downstream can tell that the
# memory is in disagreement with itself.
#
# So a copula sentence is read as a claim: this subject has this value. Two
# claims about the same subject with different values contradict, whatever
# words they are dressed in.
_COPULA_RE = re.compile(
    r"^(?P<subject>.{3,120}?)\s+(?:is|are|was|were)\s+(?P<value>.+)$", re.I)

#: Dropped from the subject before comparing, so "my full name" and "the full
#: name" are recognised as the same subject.
_SUBJECT_STOP = {"the", "a", "an", "of", "my", "our", "your", "their", "its",
                 "his", "her",
                 # the orphan left by stripping the apostrophe out of a
                 # possessive, so "user's full name" and "users full name"
                 # are one subject rather than two.
                 "s"}


def _claim(text: str) -> Optional[tuple]:
    """Read "<subject> is <value>" as (subject_key, value), or None.

    None for most sentences, which is the point: this is deliberately narrow.
    A copula asserts that a subject *has* a value, so a second value for the
    same subject is a correction. Verbs like "likes" or "uses" do not work
    that way — "I like coffee" and "I like tea" are both true — so only the
    copula is read this way.

    Subjects of fewer than two content words are rejected. "It is broken" and
    "NOVA is slow" carry nothing specific enough to match on, and matching
    them would supersede unrelated facts about the same single noun.
    """
    m = _COPULA_RE.match((text or "").strip().rstrip("."))
    if not m:
        return None
    subject = re.sub(r"[^a-z0-9 ]+", " ", m.group("subject").lower())
    words = tuple(w for w in subject.split() if w not in _SUBJECT_STOP)
    if len(words) < 2:
        return None
    value = " ".join(re.sub(r"[^a-z0-9 ]+", " ", m.group("value").lower()).split())
    if not value:
        return None
    return (words, value)


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

    @staticmethod
    def is_transient(text: str) -> bool:
        """Is this a passing observation rather than something durable?

        Refused rather than stored quietly at low confidence, because the
        caller is usually the model deciding it has learned something, and a
        refusal with a reason is what stops it deciding that again.
        """
        return any(p.search(text) for p in _TRANSIENT_PATTERNS)

    # ── record ops ───────────────────────────────────────────────────────────
    def _project_of(self, text: str) -> str:
        for p in ("ORIN", "KIWI", "VYREN", "ARVO", "NOVA", "OMNIEL", "KIWI2"):
            if p.lower() in text.lower():
                return p.title()
        return ""

    def remember(self, text: str, source: str = "explicit",
                 project: str = "", confirmed: bool = True,
                 importance: Optional[float] = None,
                 subject_id: str = DEFAULT_SUBJECT,
                 author_id: str = "") -> Dict[str, Any]:
        """Explicit command — highest priority, confirmed, strongly stored.

        `subject_id` is who the fact is *about*; `author_id` is who said it.
        They are usually the same person and occasionally are not, and the
        difference is the whole reason both exist: "Chizi prefers tea" said by
        Samuel is a fact about Chizi on Samuel's authority, and filing it
        under either name alone loses half of what is known.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("nothing to remember")
        if self.is_sensitive(text):
            raise ValueError("refusing to store what looks like a secret (key/password)")
        if self.is_transient(text):
            raise ValueError(
                "that describes what is happening right now, not something "
                "durable about the user — not storing it as a permanent fact")
        return self._upsert(text, type="fact", source=source, confirmed=confirmed,
                            project=project or self._project_of(text),
                            importance=(importance if importance is not None else 0.9),
                            explicit=True, subject_id=subject_id,
                            author_id=author_id)

    #: Subject NOVA's own world-knowledge research is filed under -- it is
    #: not a fact about a person, so it does not belong under any real
    #: subject_id, and giving it a fixed one keeps every researched topic
    #: queryable together regardless of who asked for it.
    RESEARCH_SUBJECT = "nova:research"

    def remember_research(self, topic: str, findings: str,
                          sources: Optional[List[str]] = None) -> Dict[str, Any]:
        """Store the outcome of researching *topic* as durable, retrievable
        knowledge -- what NOVA learned about the world, not a fact about
        the user, so this bypasses remember()'s transient/sensitive
        checks. Those exist for personal facts ("the user is at the
        airport right now" should not be stored as durable) and would
        incorrectly reject research text that happens to describe a
        current event, a person, or a topic that just sounds personal.

        Calling this twice for the same topic updates the existing
        record (via _upsert's own exact/near-duplicate matching) rather
        than piling up near-identical entries -- researching the same
        topic again should refresh what NOVA knows, not fork it.
        """
        topic = (topic or "").strip()
        findings = (findings or "").strip()
        if not findings:
            raise ValueError("no findings to remember")
        rec = self._upsert(findings, type="research", source="research",
                           confirmed=True, project="", importance=0.7,
                           explicit=True, subject_id=self.RESEARCH_SUBJECT,
                           author_id="nova")
        with self._lock:
            rec.setdefault("meta", {})
            rec["meta"]["topic"] = topic
            rec["meta"]["sources"] = list(sources or [])
            self._save()
        return rec

    #: Jaccard word-overlap between the asked-about topic and a stored
    #: research record's own topic, below which they are not considered
    #: the same research even though search() surfaced the record.
    RESEARCH_MATCH_THRESHOLD = 0.4

    def recall_research(self, topic: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Research NOVA has already done on *topic*, best first -- checked
        before starting a new research task so the same ground is not
        covered twice. Empty if nothing matches; the caller (the research
        workflow) then actually researches it and calls
        remember_research() when it's done.

        search() itself is not enough here: every record gets a nonzero
        score just from importance/recency (score += importance * 0.3,
        and the only filter is score > 0), which is the right behaviour
        for "give me loose context even with no real match" but the
        wrong one for "have I already researched THIS topic" -- searching
        an empty-of-relevance query against a single stored research
        record for a completely unrelated topic still returned it.
        Filtered here by comparing the asked topic's words against each
        candidate's own stored topic (recorded verbatim in meta['topic']
        by remember_research), not the free-text search score.
        """
        topic = (topic or "").strip()
        if not topic:
            return []
        topic_words = set(re.findall(r"\w+", topic.lower()))
        if not topic_words:
            return []
        candidates = self.search(topic, top_k=max(top_k * 3, 10), types=["research"])
        matches = []
        for rec in candidates:
            stored_topic = (rec.get("meta") or {}).get("topic", "")
            stored_words = set(re.findall(r"\w+", stored_topic.lower()))
            if not stored_words:
                continue
            overlap = len(topic_words & stored_words) / len(topic_words | stored_words)
            if overlap >= self.RESEARCH_MATCH_THRESHOLD:
                matches.append(rec)
        return matches[:top_k]

    def _upsert(self, text: str, type: str, source: str, confirmed: bool,
                project: str, importance: float, explicit: bool,
                subject_id: str = DEFAULT_SUBJECT,
                author_id: str = "") -> Dict[str, Any]:
        subject_id = (subject_id or DEFAULT_SUBJECT).strip() or DEFAULT_SUBJECT
        with self._lock:
            existing = (self._find_exact(text, subject_id)
                        or self._find_same_claim(text, subject_id))
            if existing:
                existing["updated"] = time.time()
                existing["access_count"] = existing.get("access_count", 0)
                existing["confirmed"] = existing["confirmed"] or confirmed
                if importance > existing.get("importance", 0):
                    existing["importance"] = round(importance, 3)
                existing["type"] = existing.get("type") or type
                self._metrics["updated"] += 1
                # Saying something NOVA already believes is still a correction
                # of anything that disagrees with it. Returning here without
                # checking is how a store that already held two answers to one
                # question kept holding them: the correct answer was present,
                # so restating it matched and changed nothing, and the wrong
                # answer beside it was never looked at. Found on the real
                # store, where "the company is OMNIEL" left "the company is
                # Omnia" standing next to it.
                clash = self._detect_conflict(text, project, subject_id)
                if clash is not None and clash is not existing:
                    self._supersede(clash, text)
                    self._metrics["superseded"] += 1
                self._save()
                return existing

            conflict = self._detect_conflict(text, project, subject_id)
            if conflict is not None and (explicit or importance >= conflict.get("importance", 0)):
                # supersede the old conflicting fact (version it) — self-edit
                self._supersede(conflict, text)
                self._metrics["superseded"] += 1

            rec = {
                "id": f"mem_{uuid.uuid4().hex[:12]}",
                "text": text[:2000],
                "type": type,
                #: Who this is about, and who said it.
                "subject_id": subject_id,
                "author_id": author_id or subject_id,
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

    @staticmethod
    def subject_of(record: Dict[str, Any]) -> str:
        """Who a record is about, including records written before subjects
        existed."""
        return (record.get("subject_id") or DEFAULT_SUBJECT)

    def _find_exact(self, text: str,
                    subject_id: str = DEFAULT_SUBJECT) -> Optional[Dict[str, Any]]:
        for r in self._records:
            if (r.get("text") == text and not r.get("superseded_by")
                    and self.subject_of(r) == subject_id):
                return r
        return None

    def _find_same_claim(self, text: str,
                         subject_id: str = DEFAULT_SUBJECT) -> Optional[Dict[str, Any]]:
        """A live record asserting the very same thing, worded differently.

        "…is OMNIEL" and "…is OMNIEL." are one fact, and storing them twice
        is how a store of five memories ends up with two of them saying the
        same thing. Exact-text matching cannot see it; the claim can.
        """
        claim = _claim(text)
        if claim is None:
            return None
        for r in self._records:
            if r.get("superseded_by"):
                continue
            if self.subject_of(r) != subject_id:
                continue
            if _claim(r.get("text", "")) == claim:
                return r
        return None

    def _detect_conflict(self, text: str, project: str,
                         subject_id: str = DEFAULT_SUBJECT) -> Optional[Dict[str, Any]]:
        """Find a live record that the new fact contradicts.

        Heuristic: when the new statement is a correction, contradictions are
        expressed in its LEADING clause ("ORIN is now private. KIWI will be ...").
        A candidate is a true conflict only if it 1) shares the leading clause's
        SUBJECT noun but 2) disagrees on a VALUE dimension (uses a different word
        from the polarity/attribute cluster). Candidates that already agree on a
        value dimension are treated as consistent and never superseded.
        """
        # Same subject, different value: a contradiction whatever words it
        # comes in. Checked before the correction-word path below because it
        # needs no announcement from the speaker, and people do not announce.
        claim = _claim(text)
        if claim is not None:
            subject, value = claim
            for r in self._records:
                if r.get("superseded_by"):
                    continue
                # Two people are allowed to disagree. "Samuel's favourite
                # colour is blue" does not contradict "Chizi's favourite
                # colour is green", and superseding across subjects would
                # mean the last person to speak overwrote everyone else.
                if self.subject_of(r) != subject_id:
                    continue
                if project and r.get("project") and project != r.get("project"):
                    continue
                other = _claim(r.get("text", ""))
                if other is not None and other[0] == subject and other[1] != value:
                    return r

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
            if self.subject_of(r) != subject_id:
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
               types: Optional[List[str]] = None,
               reader_id: Optional[str] = None,
               can_read_others: bool = True) -> List[Dict[str, Any]]:
        """Find relevant records.

        `reader_id` is who is asking. With `can_read_others=False` the search
        is confined to what is known about that one person, which is what a
        guest gets: "what do you know about Samuel" should not be a way to
        read the owner's memory out of a machine by standing next to it.

        The default is unrestricted, because every existing caller is NOVA
        working on the owner's behalf and silently narrowing those would be a
        quiet loss of memory rather than a security improvement.
        """
        visible = None
        if reader_id is not None and not can_read_others:
            visible = {reader_id}
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
                if visible is not None and self.subject_of(r) not in visible:
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