"""nova_core.rag.embeddings — three honest ways to search your documents.

The user picks one during setup, because the trade-off is theirs to make and
each choice is defensible:

    onnx     A small model runs on this machine. Semantic search, works
             offline, costs ~90 MB of disk and a one-off download.
    cloud    Embeddings come from the configured provider. Best quality, no
             disk cost, needs a connection and sends document text to that
             provider.
    lexical  No model at all. Keyword search with BM25 ranking. Instant,
             private, offline, and genuinely worse at "what did we decide
             about motors" when the document says "the drive unit selection".

The rule this module exists to enforce: **NOVA never silently substitutes a
weaker backend.** If the user chose `onnx` and the model is missing, retrieval
does not quietly fall back to keywords and let them believe they have semantic
search. `resolve()` returns what will actually be used *and* whether that was
what they asked for, so the UI can say so.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

log = logging.getLogger("nova.rag.embeddings")

#: The local backend runs whatever ONNX model is in the model directory.
#:
#: NOVA already ships all-MiniLM-L6-v2 (90 MB) in `nova_embedder/`, but the
#: packaged app could never load it: sentence-transformers needs torch, which
#: would add gigabytes to the installer, so the spec excludes it and semantic
#: memory silently degrades to keywords in the EXE. Exporting that same model
#: to ONNX fixes it -- onnxruntime is ~15 MB against torch's ~2 GB, so the
#: model NOVA already downloads becomes usable in the product.
ONNX_MODEL_REPO = "sentence-transformers/all-MiniLM-L6-v2"
ONNX_MODEL_FILES = ("onnx/model.onnx", "tokenizer.json", "config.json")
ONNX_DIM = 384


class BackendUnavailable(RuntimeError):
    """The chosen backend cannot run, with a reason the user can act on."""


class EmbeddingBackend(Protocol):
    name: str
    dim: int

    def available(self) -> tuple[bool, str]: ...
    def embed(self, texts: Sequence[str]) -> np.ndarray | None: ...


# -- lexical -----------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9']+")
_STOP = frozenset("""
a an the and or but if then than that this these those of in on at to for from
by with as is are was were be been being it its i you he she they we am do does
did not no so such can will would should could may might must have has had
what which who whom whose when where why how there here about into over under
me my mine our ours your yours their theirs him her his them us
""".split())


def tokenise(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower())
            if w not in _STOP and len(w) > 1]


class LexicalBackend:
    """No vectors. Retrieval scores with BM25 over the token index instead.

    Present as a first-class choice rather than a fallback: it is the only
    backend that costs nothing, needs nothing, and sends nothing anywhere.
    """

    name = "lexical"
    dim = 0

    def available(self) -> tuple[bool, str]:
        return True, "keyword search, always available"

    def embed(self, texts: Sequence[str]) -> np.ndarray | None:
        return None                  # by design; the store indexes tokens


def matched_terms(query: str, doc_tokens: list[str]) -> int:
    """How many distinct content words of the query this document contains."""
    return len(set(tokenise(query)) & set(doc_tokens))


def bm25_scores(query: str, docs_tokens: list[list[str]],
                k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    """Standard BM25. Beats naive term counting badly enough to matter."""
    q_terms = tokenise(query)
    n = len(docs_tokens)
    if not q_terms or n == 0:
        return np.zeros(n, dtype=np.float32)

    lengths = np.array([len(d) or 1 for d in docs_tokens], dtype=np.float32)
    avg_len = float(lengths.mean())
    counters = [Counter(d) for d in docs_tokens]

    scores = np.zeros(n, dtype=np.float32)
    for term in set(q_terms):
        df = sum(1 for c in counters if term in c)
        if df == 0:
            continue
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        for i, c in enumerate(counters):
            tf = c.get(term, 0)
            if not tf:
                continue
            denom = tf + k1 * (1 - b + b * lengths[i] / avg_len)
            scores[i] += idf * (tf * (k1 + 1)) / denom
    return scores


# -- onnx --------------------------------------------------------------------

def onnx_model_dir() -> Path:
    """Where the local model lives. Under the user's app data, not the install
    directory, so an app update never deletes a 90 MB download."""
    base = os.getenv("NOVA_MODEL_DIR", "")
    if base:
        return Path(base) / "embedder-onnx"
    try:
        from nova_secure_store import app_dir
        return app_dir() / "models" / "embedder-onnx"
    except Exception:
        return Path.home() / ".nova" / "models" / "embedder-onnx"


class OnnxBackend:
    """A small transformer on onnxruntime. No torch, no network at query time."""

    name = "onnx"
    dim = ONNX_DIM

    def __init__(self, model_dir: Path | None = None, max_length: int = 512):
        self.model_dir = Path(model_dir) if model_dir else onnx_model_dir()
        self.max_length = max_length
        self._session = None
        self._tokenizer = None
        self._pooling = "mean"
        self._lock = threading.Lock()

    def _read_meta(self) -> dict:
        """Pooling is a property of the model, not a constant.

        all-MiniLM-L6-v2 is mean-pooled; bge-* is CLS-pooled. Using the wrong
        one costs accuracy silently -- the vectors still look fine -- so it is
        recorded beside the model at export time rather than assumed.
        """
        meta = self.model_dir / "nova_model.json"
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    # -- readiness ---------------------------------------------------------

    def model_path(self) -> Path:
        direct = self.model_dir / "model.onnx"
        return direct if direct.exists() else self.model_dir / "onnx" / "model.onnx"

    def available(self) -> tuple[bool, str]:
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False, ("onnxruntime is not installed "
                           "(pip install onnxruntime)")
        try:
            import tokenizers  # noqa: F401
        except ImportError:
            return False, "tokenizers is not installed (pip install tokenizers)"
        if not self.model_path().exists():
            return False, (f"the local embedding model is not downloaded yet "
                           f"({self.model_dir})")
        if not (self.model_dir / "tokenizer.json").exists():
            return False, f"tokenizer.json is missing from {self.model_dir}"
        return True, f"local model at {self.model_dir}"

    # -- loading -----------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            ok, why = self.available()
            if not ok:
                raise BackendUnavailable(why)
            import onnxruntime as ort
            from tokenizers import Tokenizer

            opts = ort.SessionOptions()
            opts.graph_optimization_level = \
                ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            # One thread by default: embedding runs alongside a live voice
            # session, and saturating every core to index a PDF would be felt.
            opts.intra_op_num_threads = int(os.getenv("NOVA_ONNX_THREADS", "2"))
            self._session = ort.InferenceSession(
                str(self.model_path()), sess_options=opts,
                providers=["CPUExecutionProvider"])
            tok = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
            tok.enable_truncation(max_length=self.max_length)
            tok.enable_padding(length=None)
            self._tokenizer = tok
            meta = self._read_meta()
            self._pooling = str(meta.get("pooling", "mean")).lower()
            if meta.get("dim"):
                self.dim = int(meta["dim"])
            log.info("[RAG] local embedding model loaded from %s (%s pooling)",
                     self.model_dir, self._pooling)

    # -- inference ---------------------------------------------------------

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        self._ensure_loaded()
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        encoded = self._tokenizer.encode_batch([t or "" for t in texts])
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

        feed = {"input_ids": ids, "attention_mask": mask}
        names = {i.name for i in self._session.get_inputs()}
        if "token_type_ids" in names:
            feed["token_type_ids"] = np.zeros_like(ids)
        feed = {k: v for k, v in feed.items() if k in names}

        out = self._session.run(None, feed)[0]        # (batch, seq, hidden)
        if out.ndim == 3:
            if self._pooling == "cls":
                vecs = out[:, 0, :]
            else:
                # Mean over real tokens only. Averaging padding in would drag
                # every short chunk towards the same vector.
                m = mask.astype(np.float32)[:, :, None]
                vecs = (out * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        else:
            vecs = out
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return (vecs / np.maximum(norms, 1e-9)).astype(np.float32)


def download_onnx_model(progress=None) -> Path:
    """Fetch the local model once. Raises with a usable message on failure."""
    target = onnx_model_dir()
    target.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise BackendUnavailable(
            "huggingface_hub is needed to download the local model "
            "(pip install huggingface_hub), or place model.onnx and "
            f"tokenizer.json in {target} yourself.")
    for i, remote in enumerate(ONNX_MODEL_FILES, start=1):
        if progress:
            progress(i, len(ONNX_MODEL_FILES), remote)
        got = hf_hub_download(repo_id=ONNX_MODEL_REPO, filename=remote,
                              local_dir=str(target))
        log.info("[RAG] downloaded %s", got)
    return target


# -- personal memory ---------------------------------------------------------

class OnnxSentenceEncoder:
    """The ONNX model behind the one SentenceTransformer method memory uses.

    The packaged app excludes torch, so sentence-transformers never imports
    there and personal memory search was silently off in every EXE. The ONNX
    export is the same model (tools/export_embedder_onnx.py, mean pooled,
    normalised), so its vectors are interchangeable with the ones it replaces.
    """

    def __init__(self, backend=None):
        self.backend = backend if backend is not None else OnnxBackend()

    def encode(self, texts, normalize_embeddings: bool = True, **_ignored) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        return np.asarray(self.backend.embed(list(texts)), dtype=np.float32)

    def get_sentence_embedding_dimension(self) -> int:
        return int(self.backend.dim)


def load_memory_encoder(model_name: str):
    """(encoder, how) for personal memory; encoder is None with the reason."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        backend = OnnxBackend()
        ok, why = backend.available()
        if ok:
            return OnnxSentenceEncoder(backend), f"onnx ({why})"
        return None, f"sentence-transformers is not installed and the onnx model is unusable: {why}"
    return SentenceTransformer(model_name), f"sentence-transformers ({model_name})"


# -- cloud -------------------------------------------------------------------

class CloudBackend:
    """Embeddings from the configured provider.

    Routed through NOVA's provider layer rather than a hardcoded SDK, so it
    follows the same model-agnostic rules as everything else.
    """

    name = "cloud"
    dim = 768

    def __init__(self, model: str = "text-embedding-004"):
        self.model = model

    def available(self) -> tuple[bool, str]:
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            return False, "no provider API key is configured"
        try:
            import google.genai  # noqa: F401
        except ImportError:
            return False, "the google-genai SDK is not installed"
        return True, f"provider embeddings ({self.model})"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        ok, why = self.available()
        if not ok:
            raise BackendUnavailable(why)
        from google import genai
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        out = []
        # Batched: one request per chunk would make ingesting a 200-page PDF
        # both slow and expensive.
        for start in range(0, len(texts), 64):
            batch = [t or "" for t in texts[start:start + 64]]
            resp = client.models.embed_content(model=self.model, contents=batch)
            for emb in resp.embeddings:
                out.append(np.asarray(emb.values, dtype=np.float32))
        arr = np.vstack(out) if out else np.zeros((0, self.dim), dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return (arr / np.maximum(norms, 1e-9)).astype(np.float32)


# -- selection ---------------------------------------------------------------

CHOICES = ("onnx", "cloud", "lexical")


@dataclass
class Resolution:
    """What will actually be used, and whether that is what was asked for."""

    backend: EmbeddingBackend
    requested: str
    honoured: bool
    reason: str

    @property
    def name(self) -> str:
        return self.backend.name

    @property
    def semantic(self) -> bool:
        return self.backend.dim > 0

    def message(self) -> str:
        if self.honoured:
            return f"Using {self.name} embeddings: {self.reason}."
        return (f"You chose {self.requested} embeddings, but {self.reason}. "
                f"NOVA is using {self.name} search instead, which finds fewer "
                "related passages.")


def _build(name: str) -> EmbeddingBackend:
    if name == "onnx":
        return OnnxBackend()
    if name == "cloud":
        return CloudBackend()
    return LexicalBackend()


def resolve(preference: str = "") -> Resolution:
    """Pick a backend, telling the truth about what happened.

    `auto` prefers local, then cloud, then lexical -- privacy first, since the
    local model neither sends documents anywhere nor needs a connection.
    """
    want = (preference or os.getenv("NOVA_EMBEDDINGS", "") or "auto").lower()

    if want == "auto":
        for candidate in ("onnx", "cloud"):
            backend = _build(candidate)
            ok, why = backend.available()
            if ok:
                return Resolution(backend, "auto", True, why)
        lex = LexicalBackend()
        return Resolution(lex, "auto", True,
                          "no embedding model is set up, so keyword search")

    if want not in CHOICES:
        lex = LexicalBackend()
        return Resolution(lex, want, False, f"{want!r} is not a known backend")

    backend = _build(want)
    ok, why = backend.available()
    if ok:
        return Resolution(backend, want, True, why)
    return Resolution(LexicalBackend(), want, False, why)


def describe_choices() -> list[dict]:
    """What to show the user during setup, with live availability."""
    out = []
    for name, summary, detail in (
        ("onnx", "On this computer",
         "Semantic search that works offline. One-off ~90 MB download. "
         "Nothing leaves your machine."),
        ("cloud", "Through your AI provider",
         "Best quality. Needs a connection, and document text is sent to the "
         "provider to be embedded."),
        ("lexical", "Keyword search only",
         "No model, no download, nothing sent anywhere. Finds exact words "
         "rather than related ideas."),
    ):
        backend = _build(name)
        ok, why = backend.available()
        out.append({"id": name, "label": summary, "description": detail,
                    "available": ok, "status": why,
                    "semantic": backend.dim > 0})
    return out


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


__all__ = [
    "EmbeddingBackend", "LexicalBackend", "OnnxBackend", "CloudBackend",
    "BackendUnavailable", "Resolution", "resolve", "describe_choices",
    "download_onnx_model", "onnx_model_dir", "tokenise", "bm25_scores",
    "content_hash", "CHOICES", "ONNX_MODEL_REPO", "ONNX_DIM",
    "OnnxSentenceEncoder", "load_memory_encoder",
]
