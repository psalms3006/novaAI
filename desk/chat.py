"""desk.chat - one-turn message router for the NOVA desktop app.

Reuses nova.py's existing pipeline in-process: the bridge assembles memory +
system prompt + history into OpenAI-style messages, this module calls Gemini
REST (streaming when supported, same model + TOOL_DECLARATIONS as the legacy
chat path), executes any tool calls through the NOVA orchestrator (trust +
verification + audit), then produces the final reply. Fallbacks are
agent_process -> think_offline. Events are honest - tool activity reflects
real execution, failures are reported.

This is an ADDITIVE layer. No brain code is modified.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Tuple

import nova as _nova

log = _nova.log
HAS_GEMINI = getattr(_nova, "HAS_GEMINI", False)
GEMINI_API_KEY = getattr(_nova, "GEMINI_API_KEY", "")
VISION_MODEL = getattr(_nova, "VISION_MODEL", "gemini-flash-latest")
TOOL_DECLARATIONS = list(getattr(_nova, "TOOL_DECLARATIONS", []) or [])
NOVA_SYSTEM_PROMPT = getattr(_nova, "NOVA_SYSTEM_PROMPT", "")

genai = getattr(_nova, "genai", None)
gtypes = getattr(_nova, "gtypes", None)

# Event publishing functions — set by bridge.py on startup
_publish_event = None
_publish_task_start = None
_publish_task_done = None
_publish_agent_start = None
_publish_agent_progress = None
_publish_agent_done = None
_publish_orb_state = None

# ── Orchestrator integration (Hermes-independent task execution) ────────────────
_orchestrator = None

def _get_orchestrator():
    """Lazy-init the NOVA orchestrator for trust-verified tool execution."""
    global _orchestrator
    if _orchestrator is not None:
        return _orchestrator
    try:
        from orchestrator.orchestrator import NOVAOrchestrator
        from capabilities.registry import build_core_registry
        from core.verification_engine import VerificationEngine

        def _tool_dispatch(name, args, meta):
            """Bridge from CapabilityRegistry to nova._execute_tool_sync."""
            return str(_execute_tool_sync(name, args, meta) or "")

        registry = build_core_registry(fallback_fn=_tool_dispatch)
        verifier = VerificationEngine()
        _orchestrator = NOVAOrchestrator(
            registry=registry,
            verifier=verifier,
        )
        log.info("NOVA orchestrator initialized (Hermes-free)")
        return _orchestrator
    except Exception as e:
        log.warning("Orchestrator init failed, falling back to direct dispatch: %s", e)
        return None


def _resolve(name, default=None):
    return getattr(_nova, name, default)


_execute_tool_sync = _resolve("_execute_tool_sync")
_call_gemini_chat = _resolve("_call_gemini_chat")
_think_offline = _resolve("think_offline")
_is_rate_limited = _resolve("_is_rate_limited")
_record_rate_limit = _resolve("_record_rate_limit")

TOOL_LABELS = {
    "web_search": "Searching the web",
    "browser_control": "Driving the browser",
    "file_controller": "Working with files",
    "file_processor": "Processing a document",
    "vision": "Analyzing the screen or image",
    "computer_control": "Controlling the computer",
    "computer_settings": "Changing system settings",
    "open_app": "Opening an application",
    "close_app": "Closing an application",
    "planner": "Planning a task",
    "nova_task": "Running a task",
    "remember_fact": "Remembering that fact",
    "nova_memory": "Consulting memory",
    "self_editor": "Modifying Nova's own files",
}


def _ev(type_: str, **kw: Any) -> dict:
    d = {"type": type_}
    d.update(kw)
    return d


def tool_label(name: str) -> str:
    return TOOL_LABELS.get(name, name.replace("_", " "))


def _tool_summary(result: str) -> str:
    if not result:
        return ""
    line = str(result).strip().splitlines()
    return (line[0] if line else "")[:110]


def _execute_tool_via_orchestrator(name: str, args: dict, meta: dict) -> Tuple[bool, str]:
    """Execute a single tool call through the NOVA orchestrator (trust + verification).

    Returns (ok, result_string). Falls back to direct dispatch if orchestrator unavailable.
    """
    orch = _get_orchestrator()
    if orch is None:
        # Fallback: direct dispatch (no trust/verification)
        try:
            result = str(_execute_tool_sync(name, args, meta) or "")
            return result.strip() != "", result
        except Exception as e:
            return False, f"Tool error: {e}"

    try:
        from orchestrator.plan import GoalPlan, GraphStep
        from capabilities.contracts import CapabilityContext

        step = GraphStep(
            step_id="s1",
            tool=name,
            description=f"Execute {tool_label(name)}",
            parameters=args,
            critical=False,
        )
        plan = GoalPlan(goal=f"Execute {name}", steps=[step])

        ctx = CapabilityContext(platform="windows", speak=None, connectivity=None, session_facts=[], meta=meta)

        # Run through trust engine
        spec = orch.registry.get(name)
        if spec is not None:
            decision = orch.trust.decide(spec, action=args.get("action"), confidence=0.8, ambiguous=False)
            if decision.kind.value == "blocked":
                return False, f"Blocked by trust engine: {decision.reason}"

        # Execute via registry
        output = orch.registry.execute(name, args, ctx)

        # Verify result
        if orch.verifier is not None and spec is not None:
            verification = orch.verifier.verify(step, output, spec)
            if verification.status == "FAILURE":
                log.warning("Tool %s failed verification: %s", name, verification.reason)

        return output.strip() != "", output
    except Exception as e:
        log.error("Orchestrator execution failed for %s: %s", name, e)
        # Fallback to direct dispatch
        try:
            result = str(_execute_tool_sync(name, args, meta) or "")
            return result.strip() != "", result
        except Exception as e2:
            return False, f"Tool error: {e2}"


# ── Gemini primitives ──────────────────────────────────────────────────────────

def _system_text(messages: List[Dict]) -> str:
    parts = [
        m.get("content", "")
        for m in messages
        if (m.get("role") or "") == "system"
    ]
    return "\n".join(parts).strip()


def _image_part(path: str):
    """Build a Gemini Part from an image file (resized to <=1024px)."""
    if gtypes is None:
        return None
    try:
        from PIL import Image
        import io as _io
        img = Image.open(path)
        mx = 1024
        if img.width > mx or img.height > mx:
            ratio = min(mx / img.width, mx / img.height)
            img = img.resize(
                (int(img.width * ratio), int(img.height * ratio)),
                Image.Resampling.LANCZOS,
            )
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return gtypes.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")
    except Exception:
        try:
            with open(path, "rb") as f:
                return gtypes.Part.from_bytes(data=f.read(), mime_type="image/png")
        except Exception:
            return None


def _build_contents(messages: List[Dict], image_path: str = "") -> List:
    if gtypes is None:
        return []
    contents: List = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if role == "system":
            continue
        gr = "model" if role == "assistant" else "user"
        parts = [gtypes.Part(text=content)] if content else []
        if role == "user" and image_path and parts:
            part = _image_part(image_path)
            if part is not None:
                parts.append(part)
        if parts:
            contents.append(gtypes.Content(role=gr, parts=parts))
    return contents


def _make_config(use_tools: bool, messages: List[Dict]):
    if gtypes is None:
        return None
    kw: Dict[str, Any] = {}
    st = _system_text(messages)
    if st:
        kw["system_instruction"] = st
    if use_tools and TOOL_DECLARATIONS:
        kw["tools"] = [{"function_declarations": TOOL_DECLARATIONS}]
    return gtypes.GenerateContentConfig(**kw) if kw else None


def _parse_tool_calls(response) -> List[Dict]:
    calls = []
    for cand in (response.candidates or []):
        for part in (getattr(cand.content, "parts", None) or []):
            fc = getattr(part, "function_call", None)
            if fc:
                calls.append({
                    "id": str(int(time.time() * 1000)) + str(len(calls)),
                    "name": fc.name,
                    "args": dict(fc.args or {}),
                })
    return calls


# ── Turn router ────────────────────────────────────────────────────────────────

def _with_tools(disabled_tools=()):
    return [t for t in TOOL_DECLARATIONS
            if isinstance(t, dict) and t.get("name") not in disabled_tools]


def run_turn(
    messages: List[Dict],
    meta: dict,
    image_path: str = "",
    stop_event: threading.Event | None = None,
    streaming: bool = True,
):
    """Yield event dicts for one user turn. Events:

      {"type":"status","label":...}
      {"type":"token","text":...}
      {"type":"tool_start","name":...,"label":...}
      {"type":"tool_done","name":...,"ok":bool,"summary":...}
      {"type":"assistant","text":...}
      {"type":"done","latency":...}
      {"type":"error","message":...}
    """
    started = time.time()
    import uuid as _uuid
    task_id = str(_uuid.uuid4())[:8]

    # Publish task start
    if _publish_task_start:
        _publish_task_start(task_id, label="Processing")

    # Try Gemini-specific checks (rate limit) but always proceed to _first_round
    # which routes through IntelligenceRouter — works with or without Gemini key
    if HAS_GEMINI and GEMINI_API_KEY and genai and gtypes:
        try:
            if _is_rate_limited and callable(_is_rate_limited) and _is_rate_limited():
                yield _ev("error", message=(
                    "Gemini is rate-limited right now (free-tier quota). "
                    "Please wait a minute and retry."
                ))
                if _publish_task_done:
                    _publish_task_done(task_id, ok=False, summary="Rate limited")
                return
        except Exception:
            pass
    else:
        yield _ev("status", label="Using local intelligence")

    # 1. First model round (streaming preferred, full-response fallback)
    followup_msgs = list(messages)
    r_text, tool_calls, tokens = _first_round(followup_msgs, stop_event, streaming)
    for t in tokens:
        yield t
        # Publish progress for thinking agent while streaming tokens
        if t.get("type") == "token" and _publish_agent_progress:
            _publish_agent_progress("thinking-" + task_id, action="Generating response")
    tool_events = []

    # Publish thinking agent start if no tools (pure text response)
    if not tool_calls and _publish_agent_start:
        _publish_agent_start("thinking-" + task_id, task_id, name="Thinking", action="Generating response")

    # 2. Execute tools if requested (via orchestrator for trust/verification)
    if tool_calls:
        tc_spec = [
            {"name": tc.get("name", ""), "args": tc.get("args", {}), "id": tc.get("id", "")}
            for tc in tool_calls
        ]
        exec_list = []
        pending_tool_results: List[Tuple[str, str]] = []
        for tc in tool_calls:
            name = tc.get("name", "")
            if stop_event is not None and stop_event.is_set():
                break
            agent_id = f"agent-{name}-{task_id}"
            yield _ev("tool_start", name=name, label=tool_label(name))
            # Publish agent start event
            if _publish_agent_start:
                _publish_agent_start(agent_id, task_id, name=tool_label(name), action=f"Using {tool_label(name)}", tool=name)
            tool_events.append({"name": name, "label": tool_label(name), "ok": True, "summary": ""})
            try:
                ok, result = _execute_tool_via_orchestrator(name, tc.get("args", {}), meta)
            except Exception as e:
                log.error("Tool failure %s: %s", name, e, exc_info=True)
                result = f"Tool error: {e}"
                ok = False
            summary = _tool_summary(result)
            tool_events[-1]["summary"] = summary
            tool_events[-1]["ok"] = ok
            yield _ev("tool_done", name=name, ok=ok, summary=summary)
            # Publish agent done event
            if _publish_agent_done:
                _publish_agent_done(agent_id, ok=ok, summary=summary[:120])
            pending_tool_results.append((name, result))

        if stop_event is not None and stop_event.is_set():
            yield _ev("assistant", text=r_text)
            yield _ev("done", latency=round(time.time() - started, 2), tools=tool_events)
            if _publish_task_done:
                _publish_task_done(task_id, ok=True, summary=f"{len(tool_events)} tools executed")
            return

        # 3. Follow-up round to produce the final answer.
        #
        # Turn order matters. The assistant turn that *requested* the tools has
        # to come before their results, and the conversation must end on a user
        # turn — Gemini rejects a request whose last content is a model turn
        # with "400 Requests ending with a model turn are not supported", which
        # made every tool-using answer fall through to the local model.
        yield _ev("status", label="Finishing up")
        called = ", ".join(n for n, _ in pending_tool_results) or "tools"
        followup_msgs.append({
            "role": "assistant",
            "content": r_text or f"(calling {called})",
        })
        if pending_tool_results:
            results_block = "\n\n".join(
                f"[{name} result]\n{res}" for name, res in pending_tool_results
            )
            followup_msgs.append({
                "role": "user",
                "content": (
                    f"{results_block}\n\n"
                    "Using these tool results, reply to my original request "
                    "directly. Do not narrate your reasoning or mention that "
                    "tools were used."
                ),
            })
        final_text, _tokens2 = _finish_round(followup_msgs, stop_event, streaming)
        if final_text is None or not final_text.strip():
            final_text = r_text.strip()
        for t in _tokens2:
            yield t
            if t.get("type") == "token" and _publish_agent_progress:
                _publish_agent_progress("thinking-" + task_id, action="Synthesizing response")
    else:
        final_text = r_text

    final_text = (final_text or "").strip()
    if not final_text:
        # 4. Honest fallback chain
        yield _ev("status", label="Trying local agents")
        final_text = _fallback_reply(messages, meta)

    if not final_text:
        final_text = "NOVA couldn't complete that request. Please try again."

    yield _ev("assistant", text=final_text)
    yield _ev("done", latency=round(time.time() - started, 2), tools=tool_events)

    # Publish task done
    if _publish_task_done:
        _publish_task_done(task_id, ok=True, summary=final_text[:120])
    # Publish thinking agent done if it was started
    if not tool_calls and _publish_agent_done:
        _publish_agent_done("thinking-" + task_id, ok=True, summary="Response generated")


def _first_round(messages, stop_event, streaming):
    """Return (text, tool_calls, tokens_list_of_events).
    
    Provider-agnostic: routes through IntelligenceRouter when available,
    falls back to Gemini-specific path when router unavailable.
    Returns offline message if no AI backend is initialized.
    """
    # Check if any AI backend is available
    try:
        import nova
        router = getattr(nova, "_nova_router", None)
        if router:
            return _router_round(router, messages, use_tools=True, stop_event=stop_event, streaming=streaming)
    except Exception as e:
        log.debug("router round failed, using Gemini path: %s", e)

    # If brain not initialized yet (router is None), skip Gemini calls.
    # The Gemini client hangs for ~10s on SSL/network failures, which makes
    # the chat feel broken. Return offline response immediately.
    try:
        import nova
        router = getattr(nova, "_nova_router", None)
        if router is None:
            user_msg = messages[-1].get("content", "") if messages else ""
            log.info("Brain not initialized (router=None), returning offline response")
            return _get_offline_response(user_msg), [], []
    except Exception:
        pass

    # Gemini-specific path (only works if genai is initialized)
    if HAS_GEMINI and GEMINI_API_KEY and genai and gtypes:
        if streaming:
            try:
                return _stream_round(messages, use_tools=True, stop_event=stop_event)
            except Exception as e:
                log.error("streaming round failed; falling back to offline: %s", e)
                # Don't try _full_round too — if streaming failed (SSL/network),
                # full round will also fail and waste another 10+ seconds.
                user_msg = messages[-1].get("content", "") if messages else ""
                return _get_offline_response(user_msg), [], []
        text, tool_calls = _full_round(messages, use_tools=True)
        return text, tool_calls, []

    # No AI backend available — return offline message
    user_msg = messages[-1].get("content", "") if messages else ""
    log.info("No AI backend available, returning offline response")
    return _get_offline_response(user_msg), [], []


def _model_error_message(error: str) -> str:
    """Turn a raw provider/router error into an honest, user-facing message.

    Kept separate from the offline fallback so real failures (API quota,
    transient 5xx, config problems) are never disguised as "brain still
    starting up". The full detail is always logged at ERROR level by the
    caller before this message is produced.
    """
    err = (error or "").strip()
    detail = err[:200]
    if "No intelligence provider" in err:
        return ("I can't reach an AI model right now — neither the cloud nor a "
                "local model is available. Check your API key and that Ollama is running.")
    if any(tok in err for tok in ("11434", "ConnectionPool", "Read Timeout", "Read timed out",
                                  "Connection refused", "localhost", "ollama")):
        return ("The local AI model (Ollama) isn't responding. "
                "Add your Gemini API key in Settings → Account, or start Ollama and load a model.")
    if "503" in err or "UNAVAILABLE" in err or "high demand" in err:
        return ("The AI model is temporarily overloaded (503). "
                "Please wait a moment and try again.")
    if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
        return "The AI model quota was reached (429). Please wait and retry."
    if "401" in err or "API key" in err or "api key" in err:
        return "The AI API key was rejected (401). Check your GEMINI_API_KEY."
    if "CERTIFICATE_VERIFY_FAILED" in err or "certificate verify failed" in err.lower():
        return ("I can reach the network but can't establish a trusted HTTPS "
                "connection to the AI provider. This usually means antivirus "
                "HTTPS scanning or a corporate proxy is intercepting TLS. NOVA "
                "normally handles this via the system certificate store — "
                "check that the 'truststore' package is installed.")
    return f"The AI model call failed: {detail}"


def _get_offline_response(user_msg: str) -> str:
    """Return a helpful offline response when no AI backend is available."""
    msg_lower = user_msg.lower().strip()
    if not msg_lower:
        return "NOVA is starting up. I'll be fully operational once the AI backend initializes. Please try again in a moment."

    # Simple keyword-based responses for common queries
    if any(w in msg_lower for w in ["hello", "hi", "hey", "greetings"]):
        return "Hello! I'm NOVA, your AI assistant. I'm currently initializing my brain — I'll be fully operational shortly. For now, the server is running but the AI model isn't loaded yet."
    if any(w in msg_lower for w in ["what can you do", "help", "capabilities"]):
        return "Once fully initialized, I can: search the web, open/close apps, manage files, control system settings, remember facts, plan tasks, and more. I'm currently loading my AI brain — please try again in a moment."
    if any(w in msg_lower for w in ["status", "how are you", "are you working"]):
        return "NOVA's server is running and healthy. The AI brain is initializing in the background. Full functionality will be available once the model is loaded."
    if any(w in msg_lower for w in ["time", "date", "today"]):
        import datetime
        now = datetime.datetime.now()
        return f"It's {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d, %Y')}."
    if any(w in msg_lower for w in ["weather"]):
        return "I can check the weather once the AI backend is fully loaded. Please try again in a moment."
    if any(w in msg_lower for w in ["thank", "thanks"]):
        return "You're welcome! I'll be fully operational soon."
    if any(w in msg_lower for w in ["bye", "goodbye", "see you"]):
        return "Goodbye! I'll be here when you need me."

    return f"I received your message, but the AI brain is still initializing. Your query: \"{user_msg[:80]}\". Please try again in a moment."


def _finish_round(messages, stop_event, streaming):
    """Provider-agnostic follow-up round after tool execution."""
    try:
        import nova
        router = getattr(nova, "_nova_router", None)
        if router:
            text, _tc, tokens = _router_round(router, messages, use_tools=False, stop_event=stop_event, streaming=streaming)
            return text, tokens
    except Exception:
        pass

    # Fallback: Gemini-specific path
    if streaming:
        try:
            text, _tc, tokens = _stream_round(messages, use_tools=False, stop_event=stop_event)
            return text, tokens
        except Exception as e:
            log.error("streaming finish failed; falling back: %s", e)
    text, _tc = _full_round(messages, use_tools=False)
    return text, []


def _router_round(router, messages, use_tools, stop_event=None, streaming=True):
    """Route a turn through the IntelligenceRouter.
    
    When tools are needed, always use `complete` (Ollama/Gemini tool calls
    come in the full response, not as stream chunks). Stream only for
    text-only follow-up rounds.
    
    Returns (text, tool_calls, token_events).
    """
    system = _system_text(messages)
    router_msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]

    # Round with tools: non-streaming (need full response for tool calls)
    if use_tools:
        try:
            result = router.complete(
                messages=router_msgs,
                system=system,
                tools=TOOL_DECLARATIONS,
                require_tools=True,
            )
            # Emit any text as tokens so the UI shows it
            tokens = []
            if result.text:
                tokens.append(_ev("token", text=result.text))
            if result.error:
                # Surface the real error instead of a misleading offline message.
                log.error("router.complete returned error: %s", result.error)
                err_text = _model_error_message(result.error)
                return err_text, [], tokens
            if not result.text and not result.tool_calls:
                log.warning("router.complete returned empty result (no text, no tools)")
                return "I received your message but couldn't produce a response. Please try again.", [], tokens
            return result.text or "", result.tool_calls or [], tokens
        except Exception as e:
            log.error("router complete failed: %s", e, exc_info=True)
            return _model_error_message(str(e)), [], []

    # Text-only follow-up: streaming is fine
    if streaming:
        try:
            text_parts = []
            tokens = []
            for chunk in router.stream(
                messages=router_msgs,
                system=system,
            ):
                if stop_event is not None and stop_event.is_set():
                    break
                text_parts.append(chunk)
                tokens.append(_ev("token", text=chunk))
            return "".join(text_parts), [], tokens
        except Exception as e:
            log.error("router streaming failed; falling back to complete: %s", e, exc_info=True)

    try:
        result = router.complete(
            messages=router_msgs,
            system=system,
        )
    except Exception as e:
        log.error("router complete failed (follow-up): %s", e, exc_info=True)
        return _model_error_message(str(e)), [], []
    if result.error:
        log.error("router.complete returned error (follow-up): %s", result.error)
        return _model_error_message(result.error), [], []
    return result.text or "", [], []


def _full_round(messages, use_tools: bool) -> Tuple[str, List[Dict]]:
    if not callable(_call_gemini_chat):
        return "", []
    result = _call_gemini_chat(messages, use_tools=use_tools)
    if not result:
        return "", []
    return result.get("text", ""), result.get("tool_calls", [])


def _stream_round(messages, use_tools: bool, stop_event=None):
    """Stream a Gemini turn directly via generate_content_stream."""
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=gtypes.HttpOptions(timeout=8000),
    )
    contents = _build_contents(messages)
    config = _make_config(use_tools, messages)
    stream = client.models.generate_content_stream(
        model=VISION_MODEL, contents=contents, config=config)
    text_parts: List[str] = []
    tool_calls: List[Dict] = []
    tokens: List[dict] = []
    for chunk in stream:
        if stop_event is not None and stop_event.is_set():
            break
        parts = []
        try:
            if chunk.candidates:
                parts = getattr(chunk.candidates[0].content, "parts", None) or []
        except Exception:
            parts = []
        text = "".join(getattr(p, "text", "") or "" for p in parts if p.text)
        if text:
            text_parts.append(text)
            ev = _ev("token", text=text)
            tokens.append(ev)
        for p in parts:
            fc = getattr(p, "function_call", None)
            if fc:
                tool_calls.append({
                    "id": str(int(time.time() * 1000)) + str(len(tool_calls)),
                    "name": fc.name,
                    "args": dict(fc.args or {}),
                })
    return "".join(text_parts), tool_calls, tokens


def _offline_reply(messages, meta) -> str:
    # Try the intelligence router first (unified online/offline)
    try:
        import nova
        router = getattr(nova, "_nova_router", None)
        if router:
            result = router.complete(
                messages=messages,
                system=_system_text(messages),
                tools=_with_tools(),
            )
            if result.ok:
                return result.text
    except Exception as e:
        log.debug("router reply failed, falling back: %s", e)

    # Fallback to existing offline cascade
    try:
        text = _think_offline(messages[-1].get("content", "") if messages else "", meta)
        return str(text or "")
    except Exception as e:
        log.error("offline reply failed: %s", e)
        return ""


def _fallback_reply(messages, meta) -> str:
    user_text = messages[-1].get("content", "") if messages else ""
    agent = _resolve("agent_process")
    try:
        if callable(agent):
            r = agent(user_text, meta)
            if r:
                return str(r)
    except Exception as e:
        log.error("agent_process fallback failed: %s", e)
    try:
        r = _think_offline(user_text, meta)
        if r:
            return str(r)
    except Exception as e:
        log.error("think_offline fallback failed: %s", e)
    # Final fallback: use offline response
    return _get_offline_response(user_text)