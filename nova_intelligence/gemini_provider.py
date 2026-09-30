"""nova_intelligence.gemini_provider — Gemini provider wrapper.

Wraps the existing google-genai SDK behind the IntelligenceProvider protocol.
This allows the router to treat Gemini and Ollama identically.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional

from .provider import GenerateResult, ProviderCapability, ProviderHealth

log = logging.getLogger(__name__)


def _to_gemini_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Any]]:
    """Convert NOVA's legacy TOOL_DECLARATIONS shape into google-genai SDK Tools.

    NOVA declares tools as:  {"name", "description", "parameters": {type, properties, required}}
    The google-genai SDK (v2.x) expects:  Tool(function_declarations=[FunctionDeclaration(...)])
    where FunctionDeclaration.parameters is a Schema with a Type enum (not a str).
    """
    if not tools:
        return None
    from google.genai import types as gtypes

    _TYPE_MAP = {
        "STRING": gtypes.Type.STRING,
        "OBJECT": gtypes.Type.OBJECT,
        "INTEGER": gtypes.Type.INTEGER,
        "NUMBER": gtypes.Type.NUMBER,
        "BOOLEAN": gtypes.Type.BOOLEAN,
        "ARRAY": gtypes.Type.ARRAY,
    }

    def _schema(params: Dict[str, Any]) -> gtypes.Schema:
        props = params.get("properties") or {}
        properties: Dict[str, gtypes.Schema] = {}
        for k, v in props.items():
            if not isinstance(v, dict):
                continue
            v_type = str(v.get("type") or "STRING").upper()
            properties[k] = gtypes.Schema(
                type=_TYPE_MAP.get(v_type, gtypes.Type.STRING),
                description=v.get("description"),
            )
        p_type = str(params.get("type") or "OBJECT").upper()
        return gtypes.Schema(
            type=_TYPE_MAP.get(p_type, gtypes.Type.OBJECT),
            properties=properties or None,
            required=params.get("required") or None,
        )

    declarations = []
    for d in tools:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        declarations.append(gtypes.FunctionDeclaration(
            name=d.get("name"),
            description=d.get("description"),
            parameters=_schema(d.get("parameters") or {}),
        ))

    return [gtypes.Tool(function_declarations=declarations)] if declarations else None


def _gemini_role(role: str) -> str:
    """Map NOVA's message roles onto Gemini's two-role model.

    Gemini only knows "user" and "model". Tool output is *input* to the model,
    so it belongs to the user side — mapping it to "model" (the old
    everything-that-isn't-user behaviour) produced two consecutive model turns
    and a request that ended on one, which Gemini rejects.
    """
    return "model" if role in ("assistant", "model") else "user"


#: Current Gemini models think before they answer, and the thinking counts
#: against max_output_tokens. Callers asked for 1024 -- the router's default --
#: which a detailed request spends almost entirely on thinking: the stream
#: then ends with no text at all and the person sees "couldn't produce a
#: response" (measured 2026-09-30: 581 thinking + 2598 answer tokens for a
#: design brief). The budget is a ceiling on cost, not a style setting.
MIN_OUTPUT_TOKENS = 8192


def _output_budget(requested: int) -> int:
    return max(int(requested or 0), MIN_OUTPUT_TOKENS)


def _response_text(response: Any) -> str:
    """Read `.text` without letting the SDK's accessor blow up the turn.

    `response.text` raises or warns when the candidate carries no text parts —
    which happens routinely for pure function-call responses and for
    safety/MAX_TOKENS finishes.
    """
    try:
        return response.text or ""
    except Exception:
        return ""


def _response_tool_calls(response: Any) -> List[Dict[str, Any]]:
    """Extract function calls defensively.

    `candidates[0].content.parts` is None whenever the model returns a
    candidate with no parts (safety block, MAX_TOKENS, empty turn). Iterating
    it unguarded raised "'NoneType' object is not iterable" from inside the
    provider — a long-standing crash that surfaced to users as a generic model
    failure and triggered a pointless fallback to the local model.
    """
    tool_calls: List[Dict[str, Any]] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                fc = getattr(part, "function_call", None)
                if not fc or not getattr(fc, "name", ""):
                    continue
                tool_calls.append({
                    "name": fc.name,
                    "args": dict(fc.args) if fc.args else {},
                })
            if tool_calls:
                break
    except Exception as e:
        log.warning("[GEMINI] could not parse tool calls: %s", e)
    return tool_calls


def _normalize_contents(contents: List[Any]) -> List[Any]:
    """Ensure the request does not end on a model turn.

    Gemini rejects those outright ("400 Requests ending with a model turn are
    not supported"). Callers that append a trailing assistant/tool turn are
    otherwise silently broken, so close the conversation with a minimal user
    turn rather than failing the request.
    """
    if not contents:
        return contents
    if getattr(contents[-1], "role", "user") != "model":
        return contents
    from google.genai import types as gtypes
    return contents + [gtypes.Content(
        role="user",
        parts=[gtypes.Part(text="Continue and give me your final answer.")],
    )]


class GeminiProvider:
    """Google Gemini intelligence provider."""

    #: Inference happens in the cloud; requires network.
    is_local = False

    # Transient error tolerance: retry overload/quota errors with backoff, then
    # fall through to a stable pinned model before giving up.
    MAX_RETRIES = 3
    RETRY_DELAYS = (1.0, 2.0, 4.0)
    FALLBACK_MODELS = ("gemini-2.5-flash",)

    @staticmethod
    def _is_retryable(err: str) -> bool:
        e = (err or "").lower()
        # A day's quota does not refill in four seconds.
        #
        # Every 429 used to be treated as a passing squall worth waiting out:
        # three retries at 1s, 2s and 4s, repeated against each fallback
        # model. Against a per-minute limit that is right. Against
        # GenerateRequestsPerDayPerProjectPerModel it is thirteen seconds of
        # certain failure before NOVA can say anything at all — and the user
        # sees only a very long pause. Fail fast so the fallback, or the
        # explanation, arrives immediately.
        if "perday" in e.replace("_", "").replace("-", ""):
            return False
        return any(tok in e for tok in (
            "503", "429", "500", "502", "504",
            "unavailable", "resource_exhausted", "high demand",
            "overloaded", "timed out", "timeout", "rate limit",
            "rate-limit", "temporarily", "connection reset",
            "connection", "internal",
        ))

    def __init__(
        self,
        model: str = "gemini-flash-latest",
        api_key: Optional[str] = None,
    ):
        self._model = model
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._health = ProviderHealth()
        self._capabilities = (
            ProviderCapability.CHAT
            | ProviderCapability.TOOL_CALLING
            | ProviderCapability.VISION
            | ProviderCapability.STREAMING
            | ProviderCapability.SYSTEM_INSTRUCTION
        )
        self._client = None

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def capabilities(self) -> ProviderCapability:
        return self._capabilities

    @property
    def health(self) -> ProviderHealth:
        return self._health

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            from google import genai
            return True
        except ImportError:
            return False

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types as gtypes
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=gtypes.HttpOptions(timeout=60000),
            )
        return self._client

    def complete(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> GenerateResult:
        t0 = time.time()
        from google.genai import types as gtypes

        client = self._get_client()

        # Build contents (deterministic — built once, reused across retries)
        contents = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system = content  # will be passed as config
                continue
            contents.append(gtypes.Content(
                role=_gemini_role(role),
                parts=[gtypes.Part(text=content or " ")],
            ))

        if not contents:
            return GenerateResult(error="No messages to process", provider="gemini")

        contents = _normalize_contents(contents)

        config = gtypes.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=_output_budget(max_tokens),
        )
        if system:
            config.system_instruction = system
        if tools:
            config.tools = _to_gemini_tools(tools)

        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_error = ""

        for model in models_to_try:
            for attempt in range(self.MAX_RETRIES):
                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=config,
                    )

                    latency = (time.time() - t0) * 1000
                    text = _response_text(response)
                    tool_calls = _response_tool_calls(response)

                    self._health.record_success(latency)
                    return GenerateResult(
                        text=text,
                        tool_calls=tool_calls,
                        provider="gemini",
                        model=model,
                        latency_ms=latency,
                        raw=response,
                    )
                except Exception as e:
                    last_error = str(e)
                    if self._is_retryable(last_error) and attempt < self.MAX_RETRIES - 1:
                        delay = self.RETRY_DELAYS[attempt]
                        log.warning(
                            "[GEMINI] transient error on %s (attempt %d/%d), retrying in %.1fs: %s",
                            model, attempt + 1, self.MAX_RETRIES, delay, last_error[:160],
                        )
                        time.sleep(delay)
                        continue
                    # Not retryable, or retries exhausted for this model
                    log.warning("[GEMINI] %s failed (attempt %d): %s",
                                model, attempt + 1, last_error[:160])
                    break

        latency = (time.time() - t0) * 1000
        self._health.record_failure(last_error)
        log.error("[GEMINI] complete failed after trying models %s: %s",
                  models_to_try, last_error[:200])
        return GenerateResult(
            error=last_error,
            provider="gemini",
            model=self._model,
            latency_ms=latency,
        )

    def stream_complete(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> Iterator[tuple]:
        """Stream a round that may also call tools.

        Yields ``("text", chunk)`` as words arrive and finally
        ``("done", GenerateResult)`` carrying the whole reply and any tool
        calls — so a caller can show the answer being written and still get
        the structured result it needs to dispatch tools.

        The reason this exists: a round with tools used to go through
        complete(), which waits for the entire response before anything can
        be shown. Measured, the first word of a twenty-word reply appeared
        after 9-10 seconds — the full round trip — when the model had begun
        producing it in about one. The whole wait was NOVA's, not the
        model's.

        Function calls do arrive in the stream, as parts on the chunks, so
        the old reason for not streaming here ("tool calls come in the full
        response") does not hold; they simply have to be accumulated across
        chunks rather than read once at the end.
        """
        from google.genai import types as gtypes

        t0 = time.time()
        client = self._get_client()

        contents = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system = content
                continue
            contents.append(gtypes.Content(
                role=_gemini_role(role),
                parts=[gtypes.Part(text=content or " ")],
            ))
        if not contents:
            yield ("done", GenerateResult(error="No messages to process",
                                          provider="gemini"))
            return

        contents = _normalize_contents(contents)
        config = gtypes.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=_output_budget(max_tokens),
        )
        if system:
            config.system_instruction = system
        if tools:
            config.tools = _to_gemini_tools(tools)

        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_error = ""

        for model in models_to_try:
            for attempt in range(self.MAX_RETRIES):
                parts_text: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                emitted = False
                try:
                    for chunk in client.models.generate_content_stream(
                        model=model, contents=contents, config=config,
                    ):
                        for call in _response_tool_calls(chunk):
                            if call not in tool_calls:
                                tool_calls.append(call)
                        piece = getattr(chunk, "text", None)
                        if piece:
                            parts_text.append(piece)
                            emitted = True
                            yield ("text", piece)

                    if not parts_text and not tool_calls:
                        # Nothing at all is not an answer: try the next model
                        # rather than show the person an empty reply.
                        last_error = f"{model} returned an empty response"
                        log.warning("[GEMINI] %s", last_error)
                        break
                    latency = (time.time() - t0) * 1000
                    self._health.record_success(latency)
                    yield ("done", GenerateResult(
                        text="".join(parts_text),
                        tool_calls=tool_calls,
                        provider="gemini",
                        model=model,
                        latency_ms=latency,
                    ))
                    return
                except Exception as e:
                    last_error = str(e)
                    if emitted:
                        # Half a reply is already on the user's screen. Retrying
                        # would write a second answer underneath the first, so
                        # keep what arrived and stop.
                        latency = (time.time() - t0) * 1000
                        yield ("done", GenerateResult(
                            text="".join(parts_text), tool_calls=tool_calls,
                            provider="gemini", model=model, latency_ms=latency,
                            error="" if parts_text else last_error))
                        return
                    if self._is_retryable(last_error) and attempt < self.MAX_RETRIES - 1:
                        delay = self.RETRY_DELAYS[attempt]
                        log.warning(
                            "[GEMINI] transient stream error on %s (attempt %d/%d),"
                            " retrying in %.1fs: %s",
                            model, attempt + 1, self.MAX_RETRIES, delay, last_error[:160],
                        )
                        time.sleep(delay)
                        continue
                    log.warning("[GEMINI] stream %s failed (attempt %d): %s",
                                model, attempt + 1, last_error[:160])
                    break

        self._health.record_failure(last_error)
        yield ("done", GenerateResult(
            error=last_error or "All Gemini models failed",
            provider="gemini", latency_ms=(time.time() - t0) * 1000))

    def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        from google.genai import types as gtypes

        client = self._get_client()
        contents = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system = content
                continue
            contents.append(gtypes.Content(
                role=_gemini_role(role),
                parts=[gtypes.Part(text=content or " ")],
            ))

        contents = _normalize_contents(contents)

        config = gtypes.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=_output_budget(max_tokens),
        )
        if system:
            config.system_instruction = system
        if tools:
            config.tools = _to_gemini_tools(tools)

        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_error = ""
        for model in models_to_try:
            for attempt in range(self.MAX_RETRIES):
                try:
                    for chunk in client.models.generate_content_stream(
                        model=model,
                        contents=contents,
                        config=config,
                    ):
                        if chunk.text:
                            yield chunk.text
                    self._health.record_success()
                    return
                except Exception as e:
                    last_error = str(e)
                    if self._is_retryable(last_error) and attempt < self.MAX_RETRIES - 1:
                        delay = self.RETRY_DELAYS[attempt]
                        log.warning(
                            "[GEMINI] stream transient error on %s (attempt %d/%d), retrying in %.1fs: %s",
                            model, attempt + 1, self.MAX_RETRIES, delay, last_error[:160],
                        )
                        time.sleep(delay)
                        continue
                    log.warning("[GEMINI] stream failed on %s (attempt %d): %s",
                                model, attempt + 1, last_error[:160])
                    break

        self._health.record_failure(last_error)
        log.error("[GEMINI] stream failed after trying models %s: %s",
                  models_to_try, last_error[:200])
        # Raise rather than yielding an error string so the router sees a real
        # failure and can fall over to the next provider.
        raise RuntimeError(last_error or "Gemini stream failed")

    def get_model_info(self) -> dict:
        return {
            "provider": "gemini",
            "model": self._model,
            "api_key_present": bool(self._api_key),
            "available": self.is_available(),
            "health": self.health.snapshot(),
        }
