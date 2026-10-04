"""Who NOVA is talking to.

Identity, voice profiles and the authority that follows from them. Kept apart
from the voice pipeline on purpose: recognising a speaker is arithmetic on an
embedding and must never sit in the path that carries audio to the model.
"""
from __future__ import annotations

__all__ = ["fbank", "embedder"]
