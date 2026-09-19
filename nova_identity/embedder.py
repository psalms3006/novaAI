"""Turning a piece of speech into a 256-number description of the voice.

The embedding is the whole basis of telling people apart: two recordings of
one person land close together, two people land far apart, and everything
above this file is arithmetic on that distance.

The model is WeSpeaker's ResNet34, VoxCeleb-trained and large-margin
finetuned, running on onnxruntime. It is fetched separately by
`tools/fetch_speaker_model.py` and is deliberately optional — `available()`
answers honestly and every caller is expected to cope, because NOVA asking
who is speaking is a worse experience than recognising them but a far better
one than refusing to start.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from .fbank import SAMPLE_RATE, FbankExtractor

MODEL_NAME = "voxceleb_resnet34_LM.onnx"
DIR_NAME = "speaker-onnx"

#: Embeddings are 256-d and unit-normalised, so cosine similarity is a dot
#: product and lives in [-1, 1].
EMBEDDING_DIM = 256

#: Shortest utterance worth embedding. Under about a second there is not
#: enough voice in the signal to describe a person, and the embedding drifts
#: toward whatever phonemes happened to be in it.
MIN_SECONDS = 1.0


def data_dir() -> Path:
    env = os.getenv("NOVA_DATA_DIR", "").strip()
    if env:
        return Path(env)
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "NOVA"
    return Path(".")


def _search_paths() -> list[Path]:
    """Where a speaker model may live, most specific first.

    Model weights are a shared read-only asset, not user data, and the two
    must not be conflated: the test suite points NOVA_DATA_DIR at a throwaway
    directory so it cannot write into the developer's memory file, and
    looking for the model under that directory made an installed model
    invisible. Every speaker test then skipped, which reads exactly like
    passing.
    """
    out: list[Path] = []
    env = os.getenv("NOVA_MODELS_DIR", "").strip()
    if env:
        out.append(Path(env))
    out.append(data_dir() / "models")
    appdata = os.getenv("APPDATA")
    if appdata:
        out.append(Path(appdata) / "NOVA" / "models")
    # Beside the application, which is where a packaged build puts it.
    out.append(Path(__file__).resolve().parent.parent / "models")
    seen, unique = set(), []
    for d in out:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def model_path() -> Path:
    """The installed model, or where it should be installed if absent."""
    for root in _search_paths():
        candidate = root / DIR_NAME / MODEL_NAME
        try:
            if candidate.is_file():
                return candidate
        except Exception:
            continue
    return _search_paths()[0] / DIR_NAME / MODEL_NAME


def available() -> bool:
    """Is the model on disk? Asked before promising recognition."""
    try:
        return model_path().is_file()
    except Exception:
        return False


class SpeakerEmbedder:
    """One loaded model, shared. Loading it twice costs ~26 MB twice."""

    _instance: "SpeakerEmbedder | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, path: Optional[Path] = None):
        import onnxruntime as ort

        self.path = Path(path) if path else model_path()
        if not self.path.is_file():
            raise FileNotFoundError(
                f"speaker model not found at {self.path} — run "
                f"`python tools/fetch_speaker_model.py`")
        opts = ort.SessionOptions()
        # One thread. This runs beside a live audio pipeline, and a model that
        # grabs every core to embed two seconds of speech is heard as a
        # stutter in the conversation it is supposed to be improving.
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(
            str(self.path), sess_options=opts,
            providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        self._output = self._sess.get_outputs()[0].name
        self._fbank = FbankExtractor()
        self._lock = threading.Lock()

    @classmethod
    def shared(cls) -> "SpeakerEmbedder":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def embed(self, audio: np.ndarray, rate: int = SAMPLE_RATE) -> np.ndarray:
        """A unit-length 256-d embedding of one utterance.

        Raises ValueError on audio too short to describe a voice, rather than
        returning a confident-looking vector computed from nothing.
        """
        if rate != SAMPLE_RATE:
            raise ValueError(f"speaker model expects {SAMPLE_RATE} Hz, got {rate}")
        x = np.asarray(audio).reshape(-1)
        seconds = len(x) / float(rate)
        if seconds < MIN_SECONDS:
            raise ValueError(
                f"{seconds:.2f}s of audio is too short to identify a voice "
                f"(need {MIN_SECONDS:.0f}s)")

        feats = self._fbank.extract(x)
        if feats.shape[0] == 0:
            raise ValueError("no frames could be extracted from that audio")

        batch = feats[None, :, :].astype(np.float32)
        # onnxruntime sessions are not documented as thread-safe for
        # concurrent run() on all providers, and this is called from whichever
        # thread happens to have finished a turn.
        with self._lock:
            out = self._sess.run([self._output], {self._input: batch})[0]
        vec = np.asarray(out, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm < 1e-8:
            raise ValueError("model returned an empty embedding")
        return vec / norm


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two embeddings, in [-1, 1].

    A plain dot product would do for vectors this module produced, since they
    are already unit length; the normalisation is here so a caller that
    averaged several embeddings together still gets a meaningful number.
    """
    a = np.asarray(a, dtype=np.float32).reshape(-1)
    b = np.asarray(b, dtype=np.float32).reshape(-1)
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
