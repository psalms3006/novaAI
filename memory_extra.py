"""
memory_extra.py
════════════════
FAISS semantic-memory functions extracted from nova.py (Phase 2). Zero
behavior change. Same partial-init-on-import contract as agents_extra.py.

_embedder / _memory_texts remain owned by nova.py's module namespace (read
and reassigned later in nova.py's main()/UI-server code), so they're
accessed here via nova_state._embedder / nova_state._memory_texts rather than local
aliases, to avoid stale snapshots. _faiss_index is only ever touched inside
this module, so it stays a local global here.
"""
from __future__ import annotations
import os, re, json, time, shutil, tempfile
import numpy as np
from typing import List, Dict

import nova_state
import nova as _nova

log = _nova.log
DIMENSION = _nova.DIMENSION
HAS_FAISS = _nova.HAS_FAISS
faiss = _nova.faiss if HAS_FAISS else None
MEMORY_META_FILE = _nova.MEMORY_META_FILE
MEMORY_TEXTS_FILE = _nova.MEMORY_TEXTS_FILE
MEMORY_INDEX_FILE = _nova.MEMORY_INDEX_FILE
_memory_lock = _nova._memory_lock
TOP_K = _nova.TOP_K
MIN_SCORE = _nova.MIN_SCORE
HAS_GEMINI = _nova.HAS_GEMINI
GEMINI_API_KEY = _nova.GEMINI_API_KEY
MAX_COMBINED_LENGTH = _nova.MAX_COMBINED_LENGTH
genai = _nova.genai if HAS_GEMINI else None
_gemini_generate_with_delay = _nova._gemini_generate_with_delay
VISION_MODEL = _nova.VISION_MODEL
#: Fact extraction runs on its own model. It used to share gemini-flash-latest
#: with typed chat -- on the free tier one pool of 20 requests a day -- so every
#: fact remembered cost the person a reply, and a busy day starved both.
MEMORY_MODEL = os.getenv("NOVA_MEMORY_MODEL", "").strip() or "gemini-flash-lite-latest"
#: Turns worth remembering that could not be processed yet (rate limit, model
#: busy, offline). Kept on disk and processed later -- never silently dropped.
MEMORY_PENDING_FILE = MEMORY_META_FILE.parent / "memory_pending.jsonl"
_PENDING_MAX = 200

_faiss_index = None

# ══════════════════════════════════════════════════════════════════════════════
#  MEMORY — Semantic Search
# ══════════════════════════════════════════════════════════════════════════════
#
# NOVA used to require faiss-cpu for *any* memory operation: add_memory_fact()
# returned early when FAISS was missing, so every "remember this" silently
# stored nothing and every recall came back empty — while the UI still reported
# memory as enabled. faiss-cpu is also a heavy, frequently-unbuildable wheel and
# is not bundled in the packaged app.
#
# A user's fact store is hundreds of items, not millions. An exact inner-product
# search over normalised vectors is a single numpy matmul at that size, so FAISS
# is optional: it is used when present, and this drop-in index is used otherwise.


class _NumpyFlatIP:
    """Exact inner-product index with the subset of the FAISS API NOVA uses.

    Mirrors faiss.IndexFlatIP semantics — normalised vectors in, (scores,
    indices) out, both shaped (n_queries, k) — so callers need no branching.
    """

    def __init__(self, dimension: int):
        self.d = dimension
        self._vectors = np.zeros((0, dimension), dtype=np.float32)

    @property
    def ntotal(self) -> int:
        return int(self._vectors.shape[0])

    def add(self, vectors: np.ndarray) -> None:
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.shape[1] != self.d:
            raise ValueError(f"expected dim {self.d}, got {vectors.shape[1]}")
        self._vectors = np.vstack([self._vectors, vectors]) if self.ntotal else vectors.copy()

    def search(self, query: np.ndarray, k: int):
        query = np.asarray(query, dtype=np.float32)
        if query.ndim == 1:
            query = query.reshape(1, -1)
        if self.ntotal == 0 or k <= 0:
            empty_s = np.zeros((query.shape[0], 0), dtype=np.float32)
            empty_i = np.full((query.shape[0], 0), -1, dtype=np.int64)
            return empty_s, empty_i
        k = min(k, self.ntotal)
        sims = query @ self._vectors.T                       # (n_queries, ntotal)
        idx = np.argsort(-sims, axis=1)[:, :k]
        scores = np.take_along_axis(sims, idx, axis=1)
        return scores.astype(np.float32), idx.astype(np.int64)

    def reset(self) -> None:
        self._vectors = np.zeros((0, self.d), dtype=np.float32)


def _new_index():
    """A fresh empty index — FAISS when available, numpy otherwise."""
    if HAS_FAISS:
        return faiss.IndexFlatIP(DIMENSION)
    return _NumpyFlatIP(DIMENSION)


def _embed(text: str) -> np.ndarray:
    if nova_state._embedder is None:
        return np.zeros((1, DIMENSION), dtype=np.float32)
    return nova_state._embedder.encode([text], normalize_embeddings=True).astype(np.float32)


def remove_memory_facts(texts: List[str]) -> int:
    """Remove these exact facts (no longer believed: superseded, forgotten)."""
    gone = {t for t in texts if t}
    with _memory_lock:
        keep = [t for t in nova_state._memory_texts if t not in gone]
        n = len(nova_state._memory_texts) - len(keep)
        if n:
            nova_state._memory_texts = keep
            _rebuild_index()
            _atomic_save_memory({})
            log.info("Memory removed %d fact(s) no longer believed", n)
        return n


def _no_longer_believed() -> set:
    """Texts living memory has superseded or forgotten and does not also
    hold as a live record."""
    lm = getattr(nova_state, "_living_memory", None)
    if lm is None:
        return set()
    try:
        records = list(getattr(lm, "_records", []) or [])
    except Exception:
        return set()
    dead = {r.get("text") for r in records
            if r.get("superseded_by") or (r.get("decay") or {}).get("active")}
    live = {r.get("text") for r in records
            if not r.get("superseded_by") and not (r.get("decay") or {}).get("active")}
    return {t for t in dead - live if t}


def _drop_unfit_facts(meta: dict) -> None:
    """Remove stored facts that would be refused today (passing remarks,
    NOVA's own reports) or that living memory no longer believes, once, with
    the old file kept beside it. The checks in add_memory_fact stop new ones;
    this cleans what got in before them."""
    dead = _no_longer_believed()
    keep = [t for t in nova_state._memory_texts
            if isinstance(t, str) and not refusal_reason(t) and t not in dead]
    if len(keep) == len(nova_state._memory_texts):
        return
    dropped = [t for t in nova_state._memory_texts if t not in keep]
    try:
        backup = MEMORY_TEXTS_FILE.with_name(f"memory_texts.before-cleanup.{int(time.time())}.json")
        shutil.copy2(str(MEMORY_TEXTS_FILE), str(backup))
    except Exception as e:
        log.warning("Not cleaning memory: could not back it up first (%s)", e)
        return
    for t in dropped:
        why = ("no longer believed" if t in dead else refusal_reason(t)) if isinstance(t, str) else "not text"
        log.info("Memory removed (%s): %s", why, str(t)[:80])
    nova_state._memory_texts = keep
    _rebuild_index()
    _atomic_save_memory(meta)


def _collapse_preferences(embs: np.ndarray) -> np.ndarray:
    """Keep only the newest of preferences that say nearly the same thing.

    add_memory_fact does this for new ones; this does it for those stored
    before it did. Returns the embeddings of what is kept."""
    texts = nova_state._memory_texts
    prefs = [i for i, t in enumerate(texts) if isinstance(t, str) and _PREFERENCE_RE.search(t)]
    drop = {i for n, i in enumerate(prefs) for j in prefs[n + 1:]
            if float(embs[i] @ embs[j]) >= PREFERENCE_REPLACE_SIMILARITY}
    if not drop:
        return embs
    try:
        backup = MEMORY_TEXTS_FILE.with_name(f"memory_texts.before-cleanup.{int(time.time())}.json")
        if MEMORY_TEXTS_FILE.exists() and not backup.exists():
            shutil.copy2(str(MEMORY_TEXTS_FILE), str(backup))
    except Exception as e:
        log.warning("Not merging repeated preferences: could not back up first (%s)", e)
        return embs
    for i in sorted(drop):
        log.info("Memory preference replaced by a newer one: %s", texts[i][:80])
    keep = [i for i in range(len(texts)) if i not in drop]
    nova_state._memory_texts = [texts[i] for i in keep]
    return embs[keep]


#: The encoder the current index was built with (None: zero vectors).
_index_encoder = None


def _rebuild_index() -> None:
    global _faiss_index, _index_encoder
    _faiss_index = _new_index()
    _index_encoder = nova_state._embedder
    if nova_state._memory_texts and nova_state._embedder is not None:
        embs = nova_state._embedder.encode(nova_state._memory_texts, normalize_embeddings=True).astype(np.float32)
        before = len(nova_state._memory_texts)
        embs = _collapse_preferences(embs)
        _faiss_index.add(embs)
        if len(nova_state._memory_texts) != before:
            _atomic_save_memory({})
        log.info(
            "Rebuilt memory index — %d facts (%s)",
            len(nova_state._memory_texts), "faiss" if HAS_FAISS else "numpy",
        )
    elif nova_state._memory_texts:
        # Row i must stay fact i, or the next add_memory_fact puts its vector
        # at row 0 and search answers with the wrong fact.
        _faiss_index.add(np.zeros((len(nova_state._memory_texts), DIMENSION), dtype=np.float32))


def load_memory() -> dict:
    global _faiss_index, _index_encoder
    meta: Dict[str, str] = {"user_name": "", "user_gender": ""}
    if MEMORY_META_FILE.exists():
        try:
            meta = json.loads(MEMORY_META_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error(f"Corrupted memory_meta.json: {e}")
            shutil.move(str(MEMORY_META_FILE), f"{MEMORY_META_FILE}.corrupted.{int(time.time())}")
    try:
        from agent.identity import get_assistant_name, get_product_name, get_company, get_wake_word
        meta.setdefault("assistant_name", get_assistant_name())
        meta.setdefault("product_name", get_product_name())
        meta.setdefault("company", get_company())
        meta.setdefault("wake_word", get_wake_word())
    except Exception:
        pass

    previous = list(nova_state._memory_texts or [])
    nova_state._memory_texts = []
    if MEMORY_TEXTS_FILE.exists():
        try:
            loaded = json.loads(MEMORY_TEXTS_FILE.read_text(encoding="utf-8"))
            nova_state._memory_texts = loaded if isinstance(loaded, list) else []
        except json.JSONDecodeError as e:
            log.error(f"Corrupted memory_texts.json: {e}")
            shutil.move(str(MEMORY_TEXTS_FILE), f"{MEMORY_TEXTS_FILE}.corrupted.{int(time.time())}")
    _drop_unfit_facts(meta)

    # The voice session calls this for every tool call and every turn. With
    # nothing changed on disk and the index built by the current encoder,
    # re-embedding every fact each time was pure cost.
    if (nova_state._memory_texts == previous and _faiss_index is not None
            and _faiss_index.ntotal == len(previous)
            and _index_encoder is nova_state._embedder):
        return meta

    _faiss_index = _new_index()
    if HAS_FAISS and nova_state._embedder and MEMORY_INDEX_FILE.exists() and nova_state._memory_texts:
        try:
            loaded_idx = faiss.read_index(str(MEMORY_INDEX_FILE))
            if loaded_idx.ntotal == len(nova_state._memory_texts):
                _faiss_index = loaded_idx
                _index_encoder = nova_state._embedder
                log.info(f"Memory loaded — {len(nova_state._memory_texts)} facts.")
            else:
                log.warning("Index/text mismatch. Rebuilding...")
                _rebuild_index()
        except Exception as e:
            log.error(f"Corrupted memory.index: {e}. Rebuilding...")
            _rebuild_index()
    elif nova_state._memory_texts:
        # Without FAISS there is no on-disk index to restore; the numpy index is
        # rebuilt from the saved texts, which is fast at this scale and removes
        # a whole class of index/text-mismatch corruption.
        _rebuild_index()

    return meta


def _atomic_save_memory(meta: dict) -> None:
    # Callers without the person's profile pass {} (living memory's mirror
    # does), and writing that replaced memory_meta.json -- their name and
    # gender -- with nothing.
    if not meta and MEMORY_META_FILE.exists():
        try:
            meta = json.loads(MEMORY_META_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            meta = {}
    with _memory_lock:
        temp_files: List[str] = []
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
                meta_tmp = f.name
                temp_files.append(meta_tmp)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
                json.dump(nova_state._memory_texts, f, indent=2)
                texts_tmp = f.name
                temp_files.append(texts_tmp)
            if HAS_FAISS and _faiss_index is not None:
                with tempfile.NamedTemporaryFile(suffix=".index", delete=False) as f:
                    faiss.write_index(_faiss_index, f.name)
                    index_tmp = f.name
                    temp_files.append(index_tmp)
                shutil.move(index_tmp, str(MEMORY_INDEX_FILE))
            shutil.move(meta_tmp, str(MEMORY_META_FILE))
            shutil.move(texts_tmp, str(MEMORY_TEXTS_FILE))
        except Exception as e:
            log.error(f"Atomic save failed: {e}")
            for tmp in temp_files:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            raise


#: NOVA's own output, not a fact about anyone: a research report heading, a
#: task's ending. Both reached this store on 2026-09-30.
_NOT_A_FACT_RE = re.compile(r"^\s*(?:#{1,6}\s|Task '.*' ended as\b)", re.S)

#: Statements of what someone likes or wants. Two of these that mean nearly
#: the same thing are one preference said twice, or a preference that changed;
#: either way the newer one is the one to keep. Three paraphrases of "give me
#: five pages with sources when I ask for something extensive" sat side by side
#: in the real store, at 0.70-0.78 similarity; unrelated facts there peaked at 0.51.
_PREFERENCE_RE = re.compile(
    r"\b(?:prefer\w*|dislikes?|likes?|wants?|expects?|hates?|loves?)\b", re.I)
PREFERENCE_REPLACE_SIMILARITY = 0.68


def refusal_reason(text: str) -> str:
    """Why *text* must not be stored as a durable fact, or ""."""
    try:
        from living_memory import LivingMemory
        if LivingMemory.is_sensitive(text):
            return "it looks like a secret"
        if LivingMemory.is_transient(text):
            return "it describes a moment, not something durable"
    except Exception:
        pass
    if _NOT_A_FACT_RE.match(text):
        return "it is NOVA's own output, not a fact about the user"
    return ""


def add_memory_fact(text: str, meta: dict) -> bool:
    """Store a fact. Returns True if it was written.

    A missing embedder no longer discards the fact: the text is still persisted
    (so nothing the user asked NOVA to remember is lost), it simply is not
    semantically searchable until an embedder is available. Previously this
    returned early whenever FAISS *or* the embedder was missing, so NOVA
    confirmed "Remembered" and stored nothing at all.
    """
    global _faiss_index
    text = (text or "").strip()
    if not text:
        return False
    why = refusal_reason(text)
    if why:
        log.info("Memory not stored (%s): %s", why, text[:80])
        return False
    with _memory_lock:
        if text in nova_state._memory_texts:
            return False
        if _faiss_index is None:
            _faiss_index = _new_index()
        if nova_state._embedder is not None:
            vec = _embed(text)
            replaced = []
            if _faiss_index.ntotal > 0:
                k = min(5, _faiss_index.ntotal)
                scores, idx = _faiss_index.search(vec, k)
                # Near-duplicate suppression, so paraphrases don't pile up.
                if scores.size and scores[0][0] > 0.95:
                    return False
                if _PREFERENCE_RE.search(text):
                    replaced = [int(i) for s, i in zip(scores[0], idx[0])
                                if s >= PREFERENCE_REPLACE_SIMILARITY
                                and 0 <= i < len(nova_state._memory_texts)
                                and _PREFERENCE_RE.search(nova_state._memory_texts[int(i)])]
            if replaced:
                for i in sorted(replaced, reverse=True):
                    log.info("Memory preference replaced: %s", nova_state._memory_texts[i][:80])
                    del nova_state._memory_texts[i]
                nova_state._memory_texts.append(text)
                _rebuild_index()
                _atomic_save_memory(meta)
                log.info(f"Memory stored: {text[:80]}")
                return True
            _faiss_index.add(vec)
        else:
            # Row i of the index must stay aligned with _memory_texts[i] —
            # search() maps result indices straight back into that list. A zero
            # vector scores 0 against every query (below MIN_SCORE), so the fact
            # is preserved and simply unsearchable until _rebuild_index() runs.
            _faiss_index.add(np.zeros((1, DIMENSION), dtype=np.float32))
        nova_state._memory_texts.append(text)
        _atomic_save_memory(meta)
        log.info(f"Memory stored: {text[:80]}")
        return True


_LEXICAL_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "do", "for", "from", "how",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "the", "to", "what",
    "when", "where", "which", "who", "why", "you", "your",
}


def _lexical_search(query: str, top_k: int) -> List[str]:
    """Word-overlap fallback used when no embedding model is loaded.

    The packaged app ships without torch/transformers, so the embedder is
    unavailable there. Without this, every stored fact became unreachable in
    the distributed build even though it had been saved correctly.
    """
    tokens = {
        w for w in re.findall(r"\w+", (query or "").lower())
        if len(w) > 2 and w not in _LEXICAL_STOPWORDS
    }
    if not tokens:
        return []
    scored = []
    for text in nova_state._memory_texts:
        low = text.lower()
        hits = sum(1 for t in tokens if t in low)
        if hits:
            scored.append((hits / len(tokens), text))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for score, t in scored[:top_k] if score >= 0.34]


def search_memory(query: str, top_k: int = TOP_K) -> List[str]:
    if not nova_state._memory_texts:
        return []
    if nova_state._embedder is None or _faiss_index is None or _faiss_index.ntotal == 0:
        return _lexical_search(query, top_k)
    k = min(top_k, len(nova_state._memory_texts))
    scores, indices = _faiss_index.search(_embed(query), k)
    hits = [nova_state._memory_texts[i] for i, s in zip(indices[0], scores[0])
            if i < len(nova_state._memory_texts) and s >= MIN_SCORE]
    # A semantically thin match is still better answered lexically than not at
    # all (exact names, IDs and rare tokens embed poorly).
    return hits or _lexical_search(query, top_k)


def build_memory_context(meta: dict, query: str = "") -> str:
    lines = []
    if meta.get("user_name"):
        lines.append(f"- User's name is {meta['user_name']}")
    if meta.get("user_gender"):
        lines.append(f"- User's gender is {meta['user_gender']}")
    for fact in (search_memory(query) if query else nova_state._memory_texts[-TOP_K:]):
        lines.append(f"- {fact}")
    # [living memory] recollect top relevant structured records too
    try:
        _lm = nova_state._living_memory
        if _lm is not None:
            for rec in _lm.search(query or "", top_k=6):
                if not _lm.is_personal(rec):
                    continue
                _txt = (rec or {}).get("text", "").strip()
                if _txt and _txt not in lines:
                    tag = "confirmed" if rec.get("confirmed") else "recalled"
                    lines.append(f"- [{tag}] {_txt}")
    except Exception:
        pass
    return "\n".join(lines) if lines else ""


def get_all_memory_text(meta: dict) -> str:
    lines = []
    if meta.get("user_name"):
        lines.append(f"Your name is {meta['user_name']}.")
    for fact in nova_state._memory_texts:
        lines.append(f"- {fact}")
    try:
        _lm = nova_state._living_memory
        if _lm is not None:
            for rec in [r for r in _lm.all() if _lm.is_personal(r)][-15:]:
                _txt = (rec or {}).get("text", "").strip()
                if _txt and _txt not in nova_state._memory_texts:
                    lines.append(f"- {_txt}")
    except Exception:
        pass
    return " ".join(lines) if lines else "I have nothing stored about you yet."


def _is_rate_limited() -> bool:
    """Return True if we're still in a REST API backoff window."""
    return time.time() < nova_state._rest_backoff_until



#: Provider conditions that mean "come back later" rather than "something is
#: wrong". 429 was already understood; 503 is the same situation said
#: differently, and it used to be logged at ERROR -- four of them in three
#: minutes of one session, in the same log someone reads to find out why NOVA
#: went quiet. Logging a transient condition at ERROR buries the real failure
#: and teaches the reader to skim past the word.
_TRANSIENT_PROVIDER_MARKERS = (
    "429", "RESOURCE_EXHAUSTED",
    "503", "UNAVAILABLE", "high demand",
    "500", "INTERNAL",
    "504", "DEADLINE_EXCEEDED", "timed out", "timeout",
)


def _is_transient_provider_error(message: str) -> bool:
    text = (message or "")
    lowered = text.lower()
    return any(
        (marker.lower() in lowered) if not marker.isdigit() else (marker in text)
        for marker in _TRANSIENT_PROVIDER_MARKERS
    )


def _note_extraction_failure(message: str) -> None:
    """Back off quietly when the provider is busy; shout when it is our bug."""
    if _is_transient_provider_error(message):
        _record_rate_limit()
        log.info("Memory extraction deferred; the model is busy (%s)",
                 message[:120])
        return
    log.error("Memory extraction failed: %s", message)


def _record_rate_limit() -> None:
    """Called on any 429 — doubles the backoff window (max 30 min)."""
    nova_state._rest_backoff_secs = min(max(nova_state._rest_backoff_secs * 2, 120), 1800)
    nova_state._rest_backoff_until = time.time() + nova_state._rest_backoff_secs
    log.warning(
        f"REST API rate-limited — backing off {nova_state._rest_backoff_secs:.0f}s "
        f"(until {time.strftime('%H:%M:%S', time.localtime(nova_state._rest_backoff_until))})"
    )


def _reset_rate_limit() -> None:
    """Called on a successful REST call — resets backoff."""
    nova_state._rest_backoff_secs = 0.0


#: Standing instructions and preferences: things meant to hold beyond this
#: sentence, whoever they are about.
_MEMORY_STANDING = (
    "always", "never", "from now on", "going forward", "in future",
    "in the future", "stop ", "don't ", "do not ", "remember", "forget",
    "prefer", "preference", "instead of", "rather than", "make sure",
    "my goal", "note that", "keep in mind", "by default", "each time",
    "every time", "whenever",
)

#: First-person markers: something about the user, their work or their people.
_MEMORY_PERSONAL = (
    "i ", "i'm", "im ", "i've", "i'll", "i am", "my ", "mine", "me ",
    "we ", "we're", "our ", "call me",
)

#: Asking is not telling. A question with no standing instruction in it has
#: nothing to carry forward.
_MEMORY_QUESTION_OPENERS = (
    "what", "who", "where", "when", "why", "how", "which", "is ", "are ",
    "can you", "could you", "do you", "did you", "will you", "would you",
    "tell me", "show me", "open", "close", "play", "search", "find ",
)


def _should_extract_memory(user_msg: str, ai_reply: str) -> bool:
    """Is this exchange worth spending an extraction call on?

    A cheap gate in front of a model call, not the decision itself — the
    extractor still returns nothing for most of what reaches it. The point is
    only to avoid paying for "yes", "thanks" and "what time is it".

    It used to be a list of twenty-odd literal phrases, and it was far too
    narrow to be useful. Measured against realistic statements it kept one in
    six: "call me Psalms" passed, while "I always want my reports as PDF",
    "stop using bullet points", "always ask before deleting anything", "the
    OMNIEL launch is in March" and "my sister Ada is visiting next week" were
    all discarded before anything could look at them. That is the whole reason
    nothing seemed to be remembered.
    """
    text = (user_msg or "").strip().lower()
    if len(text.split()) < 4:
        return False                      # "yes", "stop", "thanks"

    padded = f" {text} "
    standing = any(k in padded for k in _MEMORY_STANDING)
    personal = any(k in padded for k in _MEMORY_PERSONAL)
    if standing:
        return True                       # holds regardless of who it is about

    if text.endswith("?") or text.startswith(_MEMORY_QUESTION_OPENERS):
        return False                      # asking, not telling

    # Anything left is a statement. First-person ones are obviously about the
    # user; the rest are only worth a look if they are substantial, which
    # catches durable project facts with no "I" in them — "the OMNIEL launch
    # is in March" would otherwise be dropped for lack of a pronoun. The
    # extractor still returns nothing for most of these; this gate exists to
    # bound the cost, not to make the decision.
    return personal or len(text.split()) >= 6


def _queue_pending(user_msg: str, ai_reply: str) -> None:
    try:
        with _memory_lock:
            lines = []
            if MEMORY_PENDING_FILE.exists():
                lines = MEMORY_PENDING_FILE.read_text(encoding="utf-8").splitlines()
            lines.append(json.dumps({"user": user_msg[:2000], "nova": (ai_reply or "")[:2000],
                                     "at": time.time()}, ensure_ascii=False))
            MEMORY_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
            MEMORY_PENDING_FILE.write_text("\n".join(lines[-_PENDING_MAX:]) + "\n", encoding="utf-8")
    except Exception as e:
        log.warning("Could not keep a turn for later memory processing: %s", e)


def _take_pending(n: int = 3) -> list:
    try:
        with _memory_lock:
            if not MEMORY_PENDING_FILE.exists():
                return []
            lines = [l for l in MEMORY_PENDING_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
            take, keep = lines[:n], lines[n:]
            if keep:
                MEMORY_PENDING_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
            else:
                MEMORY_PENDING_FILE.unlink()
        return [json.loads(l) for l in take]
    except Exception as e:
        log.warning("Could not read turns waiting for memory processing: %s", e)
        return []


def pending_count() -> int:
    try:
        return sum(1 for l in MEMORY_PENDING_FILE.read_text(encoding="utf-8").splitlines() if l.strip())
    except Exception:
        return 0


def process_pending(n: int = 3) -> int:
    """Work through turns that were kept while the model was unavailable."""
    done = 0
    for item in _take_pending(n):
        if _is_rate_limited():
            _queue_pending(item.get("user", ""), item.get("nova", ""))
            break
        ok = _extract(item.get("user", ""), item.get("nova", ""), load_memory())
        if not ok:
            break                     # it was re-queued; try again later
        done += 1
    return done


def extract_memory_updates(user_msg: str, ai_reply: str, meta: dict) -> dict:
    """Extract and store personal facts from conversation using Gemini REST.

    Runs a heuristic gate first -- only calls the model if the turn is likely
    to hold something worth keeping. While the provider is rate-limited or
    unreachable the turn is *kept* (memory_pending.jsonl) and processed later,
    instead of being dropped as it used to be."""
    # Any turn is a chance to catch up on what was kept while the model was busy.
    if HAS_GEMINI and _api_key() and not _is_rate_limited() and pending_count():
        process_pending(2)
    if not _should_extract_memory(user_msg, ai_reply):
        return meta  # Nothing worth storing — skip entirely
    if not (HAS_GEMINI and _api_key()) or _is_rate_limited():
        _queue_pending(user_msg, ai_reply)
        return meta
    _extract(user_msg, ai_reply, meta)
    if pending_count():
        process_pending()
    return meta


def _api_key() -> str:
    # Read now, not at import: the key can arrive after NOVA started
    # (onboarding, a switch to the person's own key, managed mode).
    return os.environ.get("GEMINI_API_KEY", "").strip() or GEMINI_API_KEY


def _extract(user_msg: str, ai_reply: str, meta: dict) -> bool:
    """One extraction call. True if it reached a verdict; False if the turn
    had to be kept for later."""
    combined = (user_msg + " " + ai_reply)[:MAX_COMBINED_LENGTH]
    prompt = (
        "Extract personal facts from this conversation. "
        "Return ONLY a JSON object with optional fields: user_name, user_gender, new_fact, new_preference. "
        "Rules: Return {} if nothing new. new_fact: one atomic fact about the user, their "
        "life, work, people or standing wishes -- never a description of what is on the "
        "screen, which window is open, or what NOVA just did. No markdown, raw JSON only.\n\n"
        f"Conversation: {combined}"
    )
    try:
        client = genai.Client(api_key=_api_key())
        try:
            response = _gemini_generate_with_delay(client, model=MEMORY_MODEL, contents=[prompt])
        except Exception as e:
            if "404" not in str(e) and "NOT_FOUND" not in str(e):
                raise
            # A retired or unavailable model id must not end memory.
            response = _gemini_generate_with_delay(client, model=VISION_MODEL, contents=[prompt])
        text = (response.text or "{}").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()
        if not text:
            return True
        updates = json.loads(text)
        if updates.get("user_name"):
            meta["user_name"] = str(updates["user_name"])[:50]
        if updates.get("user_gender"):
            meta["user_gender"] = str(updates["user_gender"])[:20]
        if updates.get("new_fact"):
            add_memory_fact(str(updates["new_fact"])[:500], meta)
        if updates.get("new_preference"):
            add_memory_fact(f"Preference: {str(updates['new_preference'])[:500]}", meta)
        _reset_rate_limit()
        return True
    except json.JSONDecodeError:
        return True                   # the model answered, just not in JSON: nothing to keep
    except Exception as e:
        _note_extraction_failure(str(e))
        if _is_transient_provider_error(str(e)) or "getaddrinfo" in str(e) or "connect" in str(e).lower():
            _queue_pending(user_msg, ai_reply)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  PLANNER
# ══════════════════════════════════════════════════════════════════════════════
