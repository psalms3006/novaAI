"""nova_learning.model — the reasoning engine learning runs on.

Learning is several model calls per session (extraction per batch, a
consolidation pass, test generation, answers, grading), so it runs on its own
models, not the one the conversation uses: on the free tier each Gemini model
has its own daily allowance, and learning a folder must not leave the person
unable to talk to NOVA for the rest of the day.

Order: gemini-flash-lite-latest -> gemini-flash-latest -> gemini-2.5-flash,
then the local model (Ollama) for text when no cloud model can be reached.
Images need a cloud model; without one they are skipped and said so.

`generate` is the one entry point, replaceable in tests.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

CLOUD_MODELS = [m for m in (os.getenv("NOVA_LEARNING_MODEL", "").strip(),
                            "gemini-flash-lite-latest", "gemini-flash-latest",
                            "gemini-2.5-flash") if m]
LOCAL_MODEL = os.getenv("NOVA_LEARNING_LOCAL_MODEL", "qwen2.5:1.5b")
OLLAMA = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


class ModelUnavailable(RuntimeError):
    """No model could take the request right now (quota, offline, no key)."""


@dataclass
class Image:
    mime: str
    data: bytes


def _gemini(prompt: str, images: list, want_json: bool) -> tuple:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise ModelUnavailable("no Gemini key is configured")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    parts = [types.Part(text=prompt)]
    for img in images or []:
        parts.append(types.Part(inline_data=types.Blob(mime_type=img.mime, data=img.data)))
    cfg = types.GenerateContentConfig(
        temperature=0.2, response_mime_type="application/json" if want_json else None)
    last = None
    for model in CLOUD_MODELS:
        try:
            r = client.models.generate_content(
                model=model, contents=[types.Content(role="user", parts=parts)], config=cfg)
            return (r.text or "").strip(), model
        except Exception as e:           # quota / 404 / overloaded: try the next model
            last = e
            continue
    raise ModelUnavailable(f"no Gemini model answered ({str(last)[:160]})")


def _ollama(prompt: str, want_json: bool) -> tuple:
    import requests
    try:
        r = requests.post(OLLAMA + "/api/generate",
                          json={"model": LOCAL_MODEL, "prompt": prompt, "stream": False,
                                **({"format": "json"} if want_json else {}),
                                "options": {"temperature": 0.2, "num_ctx": 8192}},
                          timeout=300)
    except Exception as e:
        raise ModelUnavailable(f"the local model is not running ({type(e).__name__})")
    if r.status_code != 200:
        raise ModelUnavailable(f"the local model refused ({r.status_code})")
    return (r.json().get("response") or "").strip(), f"ollama:{LOCAL_MODEL}"


def _default_generate(prompt: str, *, images: Optional[list] = None, want_json: bool = True,
                      allow_local: bool = True) -> tuple:
    try:
        return _gemini(prompt, images or [], want_json)
    except ModelUnavailable as cloud:
        if images or not allow_local:
            raise
        try:
            return _ollama(prompt, want_json)
        except ModelUnavailable as local:
            # Both reasons: "the local model is not running" alone hid that
            # Gemini had stopped answering (quota), which is the one to fix.
            raise ModelUnavailable(f"{cloud}; and {local}") from None


#: Replaceable: fn(prompt, *, images=None, want_json=True, allow_local=True) -> (text, model)
generate: Callable = _default_generate


def ask_json(prompt: str, *, images: Optional[list] = None, allow_local: bool = True) -> tuple:
    """(parsed JSON, model). Tolerates a fenced or chatty reply."""
    text, model = generate(prompt, images=images, want_json=True, allow_local=allow_local)
    return parse_json(text), model


def parse_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", t, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    raise ValueError("the model did not return valid JSON")


__all__ = ["generate", "ask_json", "parse_json", "Image", "ModelUnavailable", "CLOUD_MODELS"]
