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


def _rebuild_index() -> None:
    global _faiss_index
    _faiss_index = _new_index()
    if nova_state._memory_texts and nova_state._embedder is not None:
        embs = nova_state._embedder.encode(nova_state._memory_texts, normalize_embeddings=True).astype(np.float32)
        _faiss_index.add(embs)
        log.info(
            "Rebuilt memory index — %d facts (%s)",
            len(nova_state._memory_texts), "faiss" if HAS_FAISS else "numpy",
        )


def load_memory() -> dict:
    global _faiss_index
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

    nova_state._memory_texts = []
    if MEMORY_TEXTS_FILE.exists():
        try:
            loaded = json.loads(MEMORY_TEXTS_FILE.read_text(encoding="utf-8"))
            nova_state._memory_texts = loaded if isinstance(loaded, list) else []
        except json.JSONDecodeError as e:
            log.error(f"Corrupted memory_texts.json: {e}")
            shutil.move(str(MEMORY_TEXTS_FILE), f"{MEMORY_TEXTS_FILE}.corrupted.{int(time.time())}")

    _faiss_index = _new_index()
    if HAS_FAISS and nova_state._embedder and MEMORY_INDEX_FILE.exists() and nova_state._memory_texts:
        try:
            loaded_idx = faiss.read_index(str(MEMORY_INDEX_FILE))
            if loaded_idx.ntotal == len(nova_state._memory_texts):
                _faiss_index = loaded_idx
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
    with _memory_lock:
        if text in nova_state._memory_texts:
            return False
        if _faiss_index is None:
            _faiss_index = _new_index()
        if nova_state._embedder is not None:
            # Near-duplicate suppression, so paraphrases don't pile up.
            if _faiss_index.ntotal > 0:
                scores, _ = _faiss_index.search(_embed(text), 1)
                if scores.size and scores[0][0] > 0.95:
                    return False
            _faiss_index.add(_embed(text))
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


def search_memory(query: str, top_k: int = TOP_K) -> List[str]:
    if not nova_state._memory_texts or _faiss_index is None or _faiss_index.ntotal == 0:
        return []
    k = min(top_k, len(nova_state._memory_texts))
    scores, indices = _faiss_index.search(_embed(query), k)
    return [nova_state._memory_texts[i] for i, s in zip(indices[0], scores[0])
            if i < len(nova_state._memory_texts) and s >= MIN_SCORE]


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
            for rec in _lm.search(query or "", top_k=4):
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
            for rec in _lm.all()[-15:]:
                _txt = (rec or {}).get("text", "").strip()
                if _txt and _txt not in nova_state._memory_texts:
                    lines.append(f"- {_txt}")
    except Exception:
        pass
    return " ".join(lines) if lines else "I have nothing stored about you yet."


def _is_rate_limited() -> bool:
    """Return True if we're still in a REST API backoff window."""
    return time.time() < nova_state._rest_backoff_until


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


def _should_extract_memory(user_msg: str, ai_reply: str) -> bool:
    """Heuristic gate — skips REST call if conversation has nothing worth storing."""
    combined = (user_msg + " " + ai_reply).lower()
    triggers = [
        "my name", "i am", "i'm", "call me", "i study", "i work", "i live",
        "i like", "i love", "i hate", "i prefer", "my favorite", "i'm from",
        "i was born", "my project", "i'm building", "i want to", "i plan to",
        "remember", "don't forget", "note that", "my age", "years old",
        "i use", "my goal", "i'm working on", "my team", "i go to",
    ]
    return any(k in combined for k in triggers)


def extract_memory_updates(user_msg: str, ai_reply: str, meta: dict) -> dict:
    """Extract and store personal facts from conversation using Gemini REST.
    Runs heuristic gate first — only hits the API if content is likely useful.
    Silently skips while rate-limited to avoid 429 spam in the log."""
    if not (HAS_GEMINI and GEMINI_API_KEY):
        return meta
    if not _should_extract_memory(user_msg, ai_reply):
        return meta  # Nothing worth storing — skip entirely
    if _is_rate_limited():
        return meta  # Quota exhausted — skip silently, don't spam log
    combined = (user_msg + " " + ai_reply)[:MAX_COMBINED_LENGTH]
    prompt = (
        "Extract personal facts from this conversation. "
        "Return ONLY a JSON object with optional fields: user_name, user_gender, new_fact, new_preference. "
        "Rules: Return {} if nothing new. new_fact: one atomic fact. No markdown, raw JSON only.\n\n"
        f"Conversation: {combined}"
    )
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = _gemini_generate_with_delay(
            client,
            model=VISION_MODEL,
            contents=[prompt]
        )
        text = (response.text or "{}").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()
        if not text:
            return meta
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
        return meta
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            _record_rate_limit()
        else:
            log.error(f"Memory extraction failed: {e}")
    return meta


# ══════════════════════════════════════════════════════════════════════════════
#  PLANNER
# ══════════════════════════════════════════════════════════════════════════════
