"""desk.bridge - the NOVA Desktop backend: local Flask API + SPA host.

Runs in-process with the full nova stack (started by `python nova.py --desk`),
bound to 127.0.0.1 only. Reuses the existing pipeline functions via the loaded
`nova` module (_call_gemini_chat/_execute_tool_sync/build_memory_context/
_gemini_vision) - no intelligence is duplicated here.

Endpoints (all under /api, guarded by a per-run random token):
  status, tools, health, bootstrap
  chat (SSE stream), chat/stop, regenerate
  conversations (list/create/get/patch/delete/search)
  memory (get/clear/toggle, search)
  vision (image + question)
  voice (start/stop/abort/status)  push-to-talk
  files (list/save)
  settings (get/post)
  tasks
  confirm (pending/decide)      UI-driven safety gate
"""
from __future__ import annotations

import io
import functools
import json
import logging
import queue
import re
import os
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request
from flask_sock import Sock

import agent_activity
import nova as _nova
import nova_state
import nova_safety

from . import chat as desk_chat
from . import confirm as desk_confirm
from . import projects as desk_projects
from . import settings as desk_settings
from . import store as desk_store
from . import trace as desk_trace
from . import voice as desk_voice
from . import live_session as desk_live
from . import account_api as desk_account

log = _nova.log
from nova_version import APP_VERSION

_META: dict = {}
run_token: str = secrets.token_hex(16)
_started_at = time.time()
_stop_events: dict = {}
_brain_ready: bool = False  # Set by nova_desktop_app after nova.main() completes

# Live command-centre state, maintained by the event-bus publishers below so
# /api/system reports what actually happened rather than a plausible guess.
# Which agent is working is kept by agent_activity -- one registry that the
# voice session, the chat path and the task manager all report to.
_last_voice_state: str = 'idle'
_last_turn_ms: float = 0.0

_workspace: Path | None = None

STATIC_DIR = Path(__file__).parent / "static"


# ── nova resolution helpers (same pattern as server_extra.py) ─────────────────

def _resolve(name, default=None):
    try:
        return getattr(_nova, name, default)
    except Exception:
        return default


def _resolve_call(name, *modules, default=None):
    for m in modules:
        if m is None:
            continue
        v = getattr(m, name, None)
        if v is not None:
            return v
    return default


def _import_mod(name):
    try:
        import sys
        return sys.modules.get(name) or __import__(name)
    except Exception:
        return None


_live_extra = _import_mod("live_extra")
_offline_extra = _import_mod("offline_extra")
_vision_extra = _import_mod("vision_extra")
_agents_extra = _import_mod("agents_extra")

build_memory_context = _resolve_call("build_memory_context", _live_extra, _nova)
_gemini_vision = _resolve_call("_gemini_vision", _vision_extra, _nova)
is_online = _resolve_call("is_online", _live_extra, _nova)
add_memory_fact = _resolve_call("add_memory_fact", _live_extra, _nova)
NOVA_SYSTEM_PROMPT = _resolve("NOVA_SYSTEM_PROMPT", "")
FORCE_OFFLINE = _resolve("FORCE_OFFLINE", False)
_HAS_GEMINI = _resolve("HAS_GEMINI", False)
_GEMINI_KEY = _resolve("GEMINI_API_KEY", "")
_VISION_MODEL = _resolve("VISION_MODEL", "gemini-flash-latest")
_TOOL_AVAILABILITY = _resolve("_TOOL_AVAILABILITY", {})


def _ns(name, default=None):
    try:
        return getattr(nova_state, name, default)
    except Exception:
        return default


def workspace_dir() -> Path:
    global _workspace
    if _workspace is None:
        w = (desk_settings.get("workspace_dir") or "").strip()
        if w:
            _workspace = Path(w).expanduser()
        else:
            _workspace = desk_settings.app_data_dir() / "workspace"
        _workspace.mkdir(parents=True, exist_ok=True)
    return _workspace


# ── per-conversation run state ────────────────────────────────────────────────

def stop_event(cid: str):
    ev = _stop_events.get(cid)
    if ev is None:
        ev = threading.Event()
        _stop_events[cid] = ev
    return ev


# ── Gemini message assembly ───────────────────────────────────────────────────

def _meta_dict() -> dict:
    m = dict(_META)
    # The desktop's own identity source is authoritative: default is the neutral
    # "User" (never an inherited developer name); a name set in Settings persists.
    m["user_name"] = desk_settings.get("user_name") or "User"
    m["user_name_pronunciation"] = desk_settings.get("user_name_pronunciation") or ""
    m.setdefault("user_gender", "")
    m.setdefault("channel", "desktop")
    return m


def _history_messages(cid: str, history_turns: int) -> list:
    convo = desk_store.get_conversation(cid)
    if not convo:
        return []
    turns = []
    for msg in convo["messages"]:
        if msg["role"] in ("user", "assistant"):
            content = (msg.get("content") or "").strip()
            if content:
                turns.append({"role": msg["role"], "content": content})
    if history_turns > 0 and len(turns) > history_turns * 2:
        turns = turns[-(history_turns * 2):]
    return turns


def _project_for(cid: str | None) -> dict | None:
    if not cid:
        return None
    try:
        convo = desk_store.get_conversation(cid)
    except Exception:
        return None
    if not convo or not convo.get("project_id"):
        return None
    return desk_projects.get_project(convo["project_id"])


def _system_prompt(query: str, meta: dict, cid: str | None = None) -> str:
    parts = [NOVA_SYSTEM_PROMPT]

    proj = _project_for(cid)
    if proj:
        parts.append(
            f"\n\n## Active project: {proj.get('name', 'Untitled')}\n"
            f"Project context: {proj.get('description') or 'none'}\n"
            f"Project instructions (follow them for this project):\n"
            f"{proj.get('instructions') or 'none'}"
        )

    # Who NOVA is talking to -- the same block the voice session uses. Typed
    # chat used to go without it, so NOVA knew the person's name and what
    # they had told her about themselves only when spoken to.
    try:
        identity = desk_live._identity_block()
        if identity:
            parts.append("\n\n" + identity)
    except Exception as e:
        log.debug("identity block unavailable: %s", e)

    # Layer B of nova_personality: what this person has asked of NOVA, with
    # the Response style setting folded in (it used to be its own block).
    try:
        import nova_personality
        adaptation = nova_personality.adaptation_block(
            str(desk_settings.get("response_style", "balanced") or ""))
        if adaptation:
            parts.append("\n\n" + adaptation)
    except Exception as e:
        log.debug("adaptation block unavailable: %s", e)

    custom = (desk_settings.get("user_system_prompt") or "").strip()
    if custom:
        parts.append(
            "\n\n## User custom instructions\n"
            "The user has written these instructions and asked you to follow them. "
            "They add to the directives above but never override the core safety, "
            "honesty, or user-control rules:\n" + custom
        )

    if desk_settings.get("memory_enabled", True):
        try:
            mem_ctx = build_memory_context(meta, query=query)
            if mem_ctx:
                parts.append(f"\n\nMEMORY:\n{mem_ctx}")
        except Exception as e:
            log.warning("memory context failed: %s", e)
    return "\n".join(parts)


# ── persistence of a finished turn ────────────────────────────────────────────

def _persist_turn(cid: str, user_text: str, events: list, started: float):
    """Save the assistant message + tool meta; set an automatic title."""
    # The turn emits streamed "token" events AND a final "assistant" event that
    # already carries the complete text. Concatenating both stored every reply
    # twice ("ALPHA" -> "ALPHAALPHA"), which is what the conversation history
    # then replayed back to the user. The assistant event is authoritative;
    # tokens are only a fallback for a turn that never emitted one.
    streamed_parts = []
    final_text = None
    tools = []
    for ev in events:
        if ev.get("type") == "token":
            streamed_parts.append(ev.get("text", ""))
        elif ev.get("type") == "assistant":
            final_text = ev.get("text", "")
        elif ev.get("type") == "tool_start":
            tools.append({
                "name": ev.get("name"), "label": ev.get("label"),
                "ok": True, "summary": "", "started": ev.get("ts"),
            })
        elif ev.get("type") == "tool_done":
            if tools:
                tools[-1].update({"ok": bool(ev.get("ok")), "summary": ev.get("summary", "")})
    content = (final_text if final_text is not None else "".join(streamed_parts)).strip()
    mid = desk_store.upsert_last_assistant(cid, content, {"tools": tools, "latency": round(time.time() - started, 2)})
    convo = desk_store.get_conversation(cid)
    if convo and convo["title"] in ("New chat", ""):
        title = (user_text or "New chat").strip().splitlines()[0][:48]
        if title:
            desk_store.rename_conversation(cid, title)
    return mid


# ── auth decorator ────────────────────────────────────────────────────────────

def _check_desk_token() -> bool:
    tok = request.headers.get("X-NOVA-Desk", "")
    return bool(tok) and tok == run_token


def require_token(fn):
    # functools.wraps rather than copying __name__ by hand: it also sets
    # __wrapped__, so inspect.getsource on an endpoint shows the endpoint
    # instead of this wrapper, and tracebacks name the right function.
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _check_desk_token():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


# ── app ───────────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.json.ensure_ascii = False
sock = Sock(app)

# Module-level event bus stubs — replaced by real implementations in run_desk_server()
def _noop_event(event): pass
def _noop_str(text, role="nova"): pass
def _noop_task(task_id, label=""): pass
def _noop_task_done(task_id, ok=True, summary=""): pass
def _noop_agent(agent_id, task_id, name, action="", tool=""): pass
def _noop_agent_progress(agent_id, action="", tool=""): pass
def _noop_agent_done(agent_id, ok=True, summary=""): pass
def _noop_orb(state): pass
publish_event = _noop_event
publish_voice_state = _noop_str
publish_transcript = _noop_str
publish_task_start = _noop_task
publish_task_done = _noop_task_done
publish_agent_start = _noop_agent
publish_agent_progress = _noop_agent_progress
publish_agent_done = _noop_agent_done
publish_orb_state = _noop_orb


UI_INDEX = STATIC_DIR / "ui" / "index.html"


@app.get("/")
def index():
    """The desktop interface: desk/ui's build, with this run's token in it.

    Serves the same page for the main window and the ambient strip
    (`/?mode=ambient`); the page picks its layout from the query string.
    """
    if not UI_INDEX.is_file():
        # A source checkout that was never built, or a bundle missing its
        # interface: say exactly that instead of a traceback or a blank window.
        return Response(
            "<!doctype html><title>NOVA</title><body style='font:14px sans-serif;"
            "background:#0a0b10;color:#cbd5e1;padding:40px'><h2>NOVA's interface is not built</h2>"
            "<p>Run <code>npm install</code> and <code>npm run build</code> in <code>desk/ui</code>, "
            "then restart NOVA.</p></body>", status=503, mimetype="text/html")
    html = UI_INDEX.read_text(encoding="utf-8")
    html = html.replace("__DESK_TOKEN__", run_token)
    html = html.replace("__DESK_VERSION__", APP_VERSION)
    resp = Response(html, mimetype="text/html")
    # The page carries a per-run token: never let the webview reuse a copy
    # from an earlier run.
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── status / meta ─────────────────────────────────────────────────────────────

@app.get("/api/bootstrap")
@require_token
def api_bootstrap():
    return jsonify({
        "version": APP_VERSION,
        "token": run_token,
        "company": _resolve("NOVA_COMPANY", "Omniel") or "Omniel",
        "assistant_name": _resolve("NOVA_ASSISTANT_NAME", "NOVA") or "NOVA",
    })


@app.get("/api/status")
@require_token
def api_status():
    online = False
    try:
        if callable(is_online):
            # Run is_online with a quick timeout to avoid blocking
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(is_online)
                try:
                    online = bool(future.result(timeout=1.0))
                except (concurrent.futures.TimeoutError, Exception):
                    online = False
        elif isinstance(is_online, bool):
            online = is_online
    except Exception:
        pass

    tools = {}
    try:
        for name, avail in (dict(_TOOL_AVAILABILITY or {})).items():
            tools[name] = bool(avail)
    except Exception:
        pass
    tools["remember_fact"] = True

    local_intel = {"available": False, "ollama_running": False, "model": "", "installed": [], "runtime_state": "UNKNOWN"}
    try:
        import nova
        runtime = getattr(nova, "_local_runtime", None)
        if runtime:
            local_intel["runtime_state"] = runtime.state.name
            local_intel["ollama_running"] = runtime.is_running
        # Ask the registered provider what is actually installed. Reading only
        # the runtime state reported "available: false / installed: []" while a
        # model was present and being routed to, which made the offline story
        # look broken in the UI when it was not.
        _r = getattr(nova, "_nova_router", None)
        if _r is not None:
            for _pname, _prov in getattr(_r, "_providers", {}).items():
                if "ollama" not in _pname.lower():
                    continue
                local_intel["model"] = getattr(_prov, "model", "") or ""
                local_intel["available"] = bool(_prov.is_available())
                try:
                    local_intel["installed"] = [m.get("name", "") for m in _prov.list_models()]
                except Exception:
                    pass
                break
    except Exception:
        pass

    router_state = "unknown"
    try:
        import nova
        _r = getattr(nova, "_nova_router", None)
        router_state = type(_r).__name__ if _r else "None"
    except Exception:
        pass

    connectivity = "online"
    try:
        import nova
        conn = getattr(nova, "_connectivity", None)
        if conn:
            connectivity = conn.state.value
    except Exception:
        pass

    voice_status = {
        "available": False,
        "transcription": {"any": False},
        "enabled": bool(desk_settings.get("voice_enabled", True)),
        "responses": bool(desk_settings.get("voice_responses", True)),
        "provider": "unknown",
    }
    try:
        voice_status["available"] = desk_voice.audio_available()
    except Exception:
        pass

    facts_count = 0
    try:
        mt = _ns("_memory_texts", None)
        if mt is not None:
            facts_count = len(list(mt) if not isinstance(mt, (list, tuple)) else mt)
    except Exception:
        pass

    auth = {}
    try:
        auth = _auth_status()
    except Exception:
        pass

    return jsonify({
        "online": online,
        "connectivity": connectivity,
        "degraded": bool(_resolve("_rest_backoff_until") or False),
        # Name the model that will actually answer, not the one we would
        # prefer. Without a key NOVA falls back to the local model, and
        # reporting the cloud model anyway turned "why is this taking
        # eighteen seconds?" into an unanswerable question: the window said
        # ONLINE and named a Gemini model while a 1.5B model on the user's
        # own CPU was doing the work.
        "model": (_VISION_MODEL if _GEMINI_KEY
                  else (local_intel.get("model") or "local")),
        "preferred_model": _VISION_MODEL,
        "serving": "cloud" if _GEMINI_KEY else "local",
        "has_key": bool(_GEMINI_KEY),
        "gemini": bool(_HAS_GEMINI),
        "tools": tools,
        "memory": {
            "enabled": desk_settings.get("memory_enabled", True),
            "facts": facts_count,
        },
        "voice": voice_status,
        "local_intelligence": local_intel,
        "brain_ready": _brain_ready,
        "router_state": router_state,
        "tls": _tls_status(),
        "user": _meta_dict().get("user_name", "User"),
        "auth": auth,
        "uptime": round(time.time() - _started_at, 1),
        "version": APP_VERSION,
    })


def _tls_status() -> dict:
    """Outbound TLS trust posture.

    Surfaced in /api/status because a broken trust store is invisible to every
    other health signal: DNS resolves, TCP connects, connectivity reads
    "online" — and every model call still fails.
    """
    try:
        import nova_tls
        return nova_tls.status()
    except Exception as e:
        return {"applied": False, "method": "unknown", "error": str(e)}


def _auth_status() -> dict:
    """Credential posture for the UI — never contains secrets."""
    try:
        from desk import creds as desk_creds
        return desk_creds.resolve()
    except Exception as e:
        # Deliberately no "onboarded" or "has_credential" key.
        #
        # This used to report both as False, which is an assertion that the
        # user has never set NOVA up -- and the interface believed it and
        # reopened the first-run screen. A failure to read the credential
        # posture is not evidence about the user; it is the absence of
        # evidence, and saying so lets the interface leave things alone.
        return {"mode": "unknown", "cloud_configured": False,
                "byok_present": False, "byok_masked": "",
                "cloud_error": str(e)[:120]}


@app.get("/api/capabilities")
@require_token
def api_capabilities():
    """Return a runtime-derived capability inventory."""
    caps = {}

    # Voice
    voice_avail = desk_voice.audio_available()
    voice_stt = desk_voice.transcription_available()
    caps["voice"] = {
        "transcription": {"available": bool(voice_stt), "status": "AVAILABLE" if voice_stt else "UNAVAILABLE"},
        "audio_output": {"available": bool(voice_avail), "status": "AVAILABLE" if voice_avail else "UNAVAILABLE"},
        "continuous_mode": {"available": bool(desk_settings.get("continuous_conversation", False)), "status": "AVAILABLE" if desk_settings.get("continuous_conversation", False) else "DEGRADED"},
    }

    # Tools
    tools = {}
    for name, avail in (dict(_TOOL_AVAILABILITY or {})).items():
        tools[name] = {"available": bool(avail), "status": "AVAILABLE" if avail else "UNAVAILABLE"}
    tools["remember_fact"] = {"available": True, "status": "AVAILABLE"}
    tools["nova_memory"] = {"available": _ns("_living_memory") is not None, "status": "AVAILABLE" if _ns("_living_memory") else "UNAVAILABLE"}
    tools["nova_task"] = {"available": _ns("_task_manager") is not None, "status": "AVAILABLE" if _ns("_task_manager") else "UNAVAILABLE"}
    tools["close_app"] = {"available": True, "status": "AVAILABLE"}
    caps["tools"] = tools

    # Intelligence
    caps["intelligence"] = {
        "online_model": {"available": bool(_HAS_GEMINI and _GEMINI_KEY), "model": _VISION_MODEL if _HAS_GEMINI else "none", "status": "AVAILABLE" if _HAS_GEMINI else "UNAVAILABLE"},
        "offline_model": {"available": bool(_ns("_nova_router")), "status": "AVAILABLE" if _ns("_nova_router") else "UNAVAILABLE"},
        "memory": {"available": bool(_ns("_living_memory")), "status": "AVAILABLE" if _ns("_living_memory") else "UNAVAILABLE"},
        "planner": {"available": True, "status": "AVAILABLE"},
    }

    # System
    caps["system"] = {
        "computer_control": {"available": bool(_TOOL_AVAILABILITY.get("computer_control")), "status": "AVAILABLE" if _TOOL_AVAILABILITY.get("computer_control") else "UNAVAILABLE"},
        "vision": {"available": bool(_TOOL_AVAILABILITY.get("vision")), "status": "AVAILABLE" if _TOOL_AVAILABILITY.get("vision") else "UNAVAILABLE"},
        "file_operations": {"available": bool(_TOOL_AVAILABILITY.get("file_controller")), "status": "AVAILABLE" if _TOOL_AVAILABILITY.get("file_controller") else "UNAVAILABLE"},
        "web_search": {"available": bool(_TOOL_AVAILABILITY.get("web_search")), "status": "AVAILABLE" if _TOOL_AVAILABILITY.get("web_search") else "UNAVAILABLE"},
        "browser": {"available": bool(_TOOL_AVAILABILITY.get("browser_control")), "status": "AVAILABLE" if _TOOL_AVAILABILITY.get("browser_control") else "UNAVAILABLE"},
        "app_launcher": {"available": bool(_TOOL_AVAILABILITY.get("open_app")), "status": "AVAILABLE" if _TOOL_AVAILABILITY.get("open_app") else "UNAVAILABLE"},
    }

    return jsonify({"capabilities": caps})


@app.get("/api/health")
def api_health():
    try:
        import nova
        ready = getattr(nova, "_nova_router", None) is not None
    except Exception:
        ready = False
    # Waiting for sign-in is a ready state for the window: it has something to
    # show (the sign-in screen), and the brain will not start until then.
    auth_required = (os.environ.get("NOVA_AUTH_GATE") == "1"
                     and not (ready or _brain_ready))
    try:
        import nova_runtime
        brain = nova_runtime.brain_state()
    except Exception:
        brain = {}
    return jsonify({"ok": True, "ready": ready, "brain_ready": _brain_ready,
                    "auth_required": auth_required,
                    "brain_starting": bool(brain.get("started")) and not brain.get("ready"),
                    "brain_error": brain.get("error", "")})


# ── tools ─────────────────────────────────────────────────────────────────────

@app.get("/api/tools")
@require_token
def api_tools():
    tools = {}
    for name, avail in (dict(_TOOL_AVAILABILITY or {})).items():
        tools[name] = {
            "available": bool(avail),
            "label": desk_chat.tool_label(name),
            "source": "actions" if avail else "unavailable",
            "category": desk_confirm.TOOL_CATEGORY.get(name, ""),
            "permission": desk_confirm._permission_for(name),  # noqa: SLF001
        }
    tools["remember_fact"] = {"available": True, "label": "Remember fact", "source": "nova",
                              "category": "memory", "permission": desk_confirm._permission_for("remember_fact")}  # noqa: SLF001
    tools["nova_memory"] = {"available": _ns("_living_memory") is not None, "label": "Memory store",
                            "source": "living_memory", "category": "memory",
                            "permission": desk_confirm._permission_for("nova_memory")}  # noqa: SLF001
    tools["nova_task"] = {"available": _ns("_task_manager") is not None, "label": "Task manager",
                          "source": "task_manager", "category": "computer",
                          "permission": desk_confirm._permission_for("nova_task")}  # noqa: SLF001
    return jsonify({"tools": tools, "mcp": _mcp_status()})


# ── conversations ─────────────────────────────────────────────────────────────

@app.get("/api/conversations")
@require_token
def api_conversations():
    return jsonify({"conversations": desk_store.list_conversations()})


@app.get("/api/conversations/search")
@require_token
def api_conversations_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"conversations": []})
    return jsonify({"conversations": desk_store.search_conversations(q)})


@app.post("/api/conversations")
@require_token
def api_new_conversation():
    data = request.get_json(silent=True) or {}
    project_id = (data.get("project_id") or "").strip()
    if project_id and desk_projects.get_project(project_id) is None:
        return jsonify({"error": "Unknown project"}), 400
    cid = desk_store.new_conversation(project_id=project_id)
    return jsonify({"id": cid, "project_id": project_id})


@app.get("/api/conversations/<cid>")
@require_token
def api_get_conversation(cid):
    convo = desk_store.get_conversation(cid)
    if convo is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(convo)


@app.patch("/api/conversations/<cid>")
@require_token
def api_patch_conversation(cid):
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if title:
        desk_store.rename_conversation(cid, title)
    return jsonify({"ok": True})


@app.delete("/api/conversations/<cid>")
@require_token
def api_delete_conversation(cid):
    desk_store.delete_conversation(cid)
    _stop_events.pop(cid, None)
    return jsonify({"ok": True})


# ── chat ──────────────────────────────────────────────────────────────────────

def _persona_filter(events):
    """Strip filler openers ("Great question!", "Absolutely!") from replies.

    The start of each stretch of streamed text is held back briefly -- until
    a sentence ends or enough has arrived to judge -- cleaned once, and then
    everything after it streams untouched. The final assistant text is
    cleaned the same way, so what is stored matches what was shown.
    """
    import nova_personality
    buf, holding = [], True

    def flush():
        text = "".join(buf)
        buf.clear()
        return {"type": "token", "text": nova_personality.clean_reply(text)} if text else None

    for ev in events:
        typ = ev.get("type")
        if typ == "token" and holding:
            buf.append(ev.get("text") or "")
            joined = "".join(buf)
            if len(joined) >= 60 or (len(joined) > 12 and any(c in joined for c in ".!?\n")):
                holding = False
                out = flush()
                if out:
                    yield out
            continue
        if typ != "token":
            out = flush() if buf else None
            if out:
                yield out
            holding = True          # the next stretch of text starts a new reply
            if typ == "assistant" and ev.get("text"):
                ev = dict(ev, text=nova_personality.clean_reply(ev["text"]))
        yield ev
    out = flush() if buf else None
    if out:
        yield out


def _run_chat(cid, message, image_path, streaming):
    try:
        import nova_personality
        changed = nova_personality.learn_from_message(message or "")
        if changed:
            log.info("[CHAT] adapted to the user: %s", ", ".join(changed))
    except Exception:
        pass
    return _persona_filter(_run_chat_raw(cid, message, image_path, streaming))


def _run_chat_raw(cid, message, image_path, streaming):
    meta = _meta_dict()
    msgs = [{"role": "system", "content": _system_prompt(message, meta, cid)}]
    msgs += _history_messages(cid, int(desk_settings.get("history_turns", 10)))
    # api_chat() already stored this user message before calling us, so
    # _history_messages() above has just returned it. Appending it again
    # sent every prompt to the model TWICE — which inflated context on
    # every turn and made the model echo itself ("ALPHA" -> "ALPHAALPHA").
    if not (msgs and msgs[-1].get("role") == "user"
            and (msgs[-1].get("content") or "").strip() == (message or "").strip()):
        msgs.append({"role": "user", "content": message})
    ev = stop_event(cid)
    ev.clear()
    return desk_chat.run_turn(msgs, meta, image_path=image_path or "",
                              stop_event=ev, streaming=streaming)


@app.post("/api/chat")
@require_token
def api_chat():
    data = request.get_json(silent=True) or {}
    cid = (data.get("conversation_id") or "").strip()
    if not cid:
        cid = desk_store.new_conversation()
    message = (data.get("message") or "").strip()
    image_path = (data.get("image_path") or data.get("image") or "").strip()
    if not message and not image_path:
        return jsonify({"error": "No message"}), 400
    if image_path and not Path(image_path).exists():
        return jsonify({"error": "Image file was not found on disk"}), 400
    if message:
        desk_store.add_message(cid, "user", message, {})
    elif image_path:
        desk_store.add_message(cid, "user", f"[image attached: {Path(image_path).name}]", {"image": Path(image_path).name})

    streaming_on = bool(desk_settings.get("streaming", True))
    events_accum: list = []
    started = time.time()

    rid = desk_trace.start(f"chat cid={cid[:8]} streaming={streaming_on} chars={len(message)}")
    desk_trace.mark("http_receive")

    # Publish orb state: thinking when chat starts
    publish_event({"type": "orb_state", "state": "thinking", "ts": time.time()})

    def event_gen():
        first_emit = True
        try:
            # Hand the request id to the browser so a UI trace can be
            # correlated with server-side stage timings for the same turn.
            yield 'data: ' + json.dumps({'type': 'meta', 'rid': rid}) + '\n\n'

            for ev in _run_chat(cid, message, image_path, streaming_on):
                events_accum.append(ev)
                if first_emit:
                    desk_trace.mark("first_event_emitted", type=ev.get("type"))
                    first_emit = False
                # Step aside for desktop work so the user can watch it happen,
                # and only for tools where seeing the screen is the point.
                if ev.get("type") == "tool_start" and ev.get("name") in _DESKTOP_TOOLS:
                    set_ambient_mode("ambient")
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                # Update orb state based on events
                if ev.get("type") == "tool_start":
                    publish_event({"type": "orb_state", "state": "executing", "ts": time.time()})
                elif ev.get("type") in ("assistant", "done"):
                    pass  # handled in finally
        except GeneratorExit:
            pass
        except Exception as e:
            log.error("chat turn failed: %s", e, exc_info=True)
            err = _ev_error(e)
            events_accum.append(err)
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            # Return orb to idle
            publish_event({"type": "orb_state", "state": "idle", "ts": time.time()})
            desk_trace.mark("stream_end", events=len(events_accum))
            if _ambient_mode == "ambient":
                set_ambient_mode("full")
            if message or image_path:
                _persist_turn(cid, message, events_accum, started)
            global _last_turn_ms
            _last_turn_ms = round((time.time() - started) * 1000, 1)
            desk_trace.mark("persisted")
            log.info("[TRACE %s] SUMMARY %s", rid, desk_trace.summary())

    resp = Response(event_gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


def _ev_error(e) -> dict:
    friendly = (
        "NOVA couldn't complete that request. This usually means the model "
        "call failed or timed out. Please retry, or check the connection."
    )
    return {"type": "error", "message": friendly, "detail": str(e)[:300]}


@app.post("/api/chat/stop")
@require_token
def api_chat_stop():
    data = request.get_json(silent=True) or {}
    cid = data.get("conversation_id", "")
    ev = _stop_events.get(cid)
    if ev:
        ev.set()
    return jsonify({"ok": True})


@app.post("/api/regenerate")
@require_token
def api_regenerate():
    data = request.get_json(silent=True) or {}
    cid = data.get("conversation_id", "")
    convo = desk_store.get_conversation(cid)
    if not convo:
        return jsonify({"error": "not found"}), 404
    msgs = convo["messages"]
    last_user = None
    ridx = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i]["role"] == "assistant":
            continue
        if msgs[i]["role"] == "user":
            last_user = msgs[i]
            ridx = i
            break
    if last_user is None:
        return jsonify({"error": "No message to regenerate"}), 400
    # drop the assistant message that followed the last user turn
    removed = 0
    with_none = []
    for i, m in enumerate(msgs):
        if i > ridx and m["role"] == "assistant" and removed == 0:
            removed += 1
            continue
        with_none.append(m)
    # persist removal
    for m in msgs[ridx + 1:]:
        desk_store.delete_message(m["id"])
    events_accum: list = []
    started = time.time()
    message = last_user.get("content", "")
    image_path = ""
    ev = stop_event(cid)
    ev.clear()

    def event_gen():
        try:
            for ev_ in desk_chat.run_turn(
                [{"role": "system", "content": _system_prompt(message, _meta_dict(), cid)}]
                + [{"role": "user", "content": message}],
                _meta_dict(), image_path="", stop_event=ev,
                streaming=bool(desk_settings.get("streaming", True))):
                events_accum.append(ev_)
                yield f"data: {json.dumps(ev_, ensure_ascii=False)}\n\n"
        except Exception as e:
            log.error("regenerate failed: %s", e, exc_info=True)
            err = _ev_error(e)
            events_accum.append(err)
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            _persist_turn(cid, message, events_accum, started)

    resp = Response(event_gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ── memory ────────────────────────────────────────────────────────────────────

@app.get("/api/memory")
@require_token
def api_memory():
    q = (request.args.get("q") or "").strip()
    facts = list(_ns("_memory_texts", []) or [])[-200:]
    records = []
    lm = _ns("_living_memory")
    metrics = {}
    if lm is not None:
        try:
            records = [
                {
                    "text": r.get("text", ""),
                    "type": r.get("type", ""),
                    "importance": r.get("importance", 0.0),
                    "project": r.get("project", ""),
                    "confirmed": bool(r.get("confirmed", True)),
                    "updated": r.get("updated", 0.0),
                    "superseded": bool(r.get("superseded_by")),
                    "decayed": bool(r.get("decay", {}).get("active")),
                }
                for r in lm.all_records()
            ]
        except Exception as e:
            log.warning("living memory listing failed: %s", e)
        try:
            metrics = dict(lm._metrics or {})  # noqa: SLF001
        except Exception:
            metrics = {}
    living = []
    ctx = ""
    if lm is not None and q:
        try:
            recs = lm.search(q)
            living = [{"text": r.get("text", ""), "confidence": r.get("confidence", "")}
                      for r in (recs or [])][:20]
        except Exception:
            living = []
        try:
            ctx = build_memory_context(_meta_dict(), query=q)
        except Exception:
            ctx = ""
    return jsonify({"facts": facts, "records": records, "metrics": metrics,
                    "living": living, "context": ctx,
                    "enabled": desk_settings.get("memory_enabled", True)})


@app.delete("/api/memory/records")
@require_token
def api_memory_record_delete():
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    updated = data.get("updated") or 0
    lm = _ns("_living_memory")
    if lm is None:
        return jsonify({"ok": False, "error": "Living memory is not initialized."}), 400
    if not text:
        return jsonify({"ok": False, "error": "Missing record text"}), 400
    try:
        ok = lm.delete_record(text, float(updated))
    except Exception:
        ok = False
    return jsonify({"ok": ok})


@app.post("/api/memory/clear")
@require_token
def api_memory_clear():
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"error": "confirmation required"}), 400
    lm = _ns("_living_memory")
    cleared = 0
    try:
        if lm is not None and hasattr(lm, "clear_all"):
            cleared = lm.clear_all()
    except Exception:
        pass
    try:
        facts = _ns("_memory_texts", [])
        facts.clear()
    except Exception:
        pass
    return jsonify({"ok": True, "cleared": cleared})


@app.post("/api/memory/toggle")
@require_token
def api_memory_toggle():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    desk_settings.set_many({"memory_enabled": enabled})
    return jsonify({"ok": True, "enabled": enabled})


# ── vision ────────────────────────────────────────────────────────────────────

@app.post("/api/vision")
@require_token
def api_vision():
    f = request.files.get("image")
    if f is None:
        return jsonify({"error": "No image uploaded"}), 400
    question = (request.form.get("question") or "").strip() or "What do you see in this image?"
    tmp = tempfile.NamedTemporaryFile(suffix=Path(f.filename or "img.png").suffix or ".png", delete=False)
    f.save(tmp.name)
    if not callable(_gemini_vision):
        return jsonify({"error": "Vision is unavailable in this build."})
    try:
        text = _gemini_vision(tmp.name, question)
        return jsonify({"text": str(text)})
    except Exception as e:
        log.error("vision failed: %s", e, exc_info=True)
        return jsonify({"error": "Vision analysis failed. Please check the Gemini API key."}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


# ── voice (push-to-talk) ──────────────────────────────────────────────────────

@app.post("/api/voice/start")
@require_token
def api_voice_start():
    # Push-to-talk is the fallback for when the live session cannot run. It is
    # not a second way to listen *while* it is running: that would be two
    # capture streams on one device, which on Windows generally succeeds and
    # then splits the input between them, so NOVA mishears intermittently with
    # nothing anywhere reporting a fault.
    if desk_live.get_live_manager().owns_microphone:
        return jsonify({
            "ok": False,
            "message": ("NOVA is already listening through the live voice "
                        "session, so push-to-talk is not needed. Stop the "
                        "voice session first if you want to record."),
            "reason": "live_session_owns_microphone",
            **desk_voice.status(),
        })
    ok, msg = desk_voice.start_capture()
    return jsonify({"ok": ok, "message": msg, **desk_voice.status()})


@app.post("/api/voice/stop")
@require_token
def api_voice_stop():
    path, msg = desk_voice.stop_capture()
    if not path:
        return jsonify({"ok": False, "message": msg})
    transcript, err = desk_voice.transcribe(path)
    try:
        os.unlink(path)
    except Exception:
        pass
    if transcript:
        return jsonify({"ok": True, "transcript": transcript, "message": msg})
    return jsonify({"ok": False, "message": err or "No speech was recognised."})


@app.post("/api/voice/abort")
@require_token
def api_voice_abort():
    return jsonify({"ok": True, "message": desk_voice.abort_capture()})


@app.get("/api/voice/status")
@require_token
def api_voice_status():
    return jsonify(desk_voice.status())


# ── live (Gemini Live native audio) ───────────────────────────────────────────

@app.post("/api/live/start")
@require_token
def api_live_start():
    # The microphone permission governs voice itself, not only the wake word.
    if (desk_settings.get("permissions", {}) or {}).get("microphone") == "deny":
        return jsonify({"ok": False, "error": "microphone_denied",
                        "message": "The microphone is turned off in NOVA's permissions. "
                                   "Turn it on in Settings > Permissions to talk to NOVA."}), 403
    mgr = desk_live.get_live_manager()
    r = mgr.start()
    return jsonify(r)


@app.post("/api/live/stop")
@require_token
def api_live_stop():
    mgr = desk_live.get_live_manager()
    r = mgr.stop()
    return jsonify(r)


@app.post("/api/live/mute")
@require_token
def api_live_mute():
    """Mute/unmute the microphone.

    A secondary control, not the way NOVA is operated: the mic is hot from the
    moment initialisation finishes and stays that way. Default is unmuted and
    mute is not persisted across restarts.
    """
    data = request.get_json(silent=True) or {}
    mgr = desk_live.get_live_manager()
    if "muted" in data:
        return jsonify(mgr.set_muted(bool(data.get("muted"))))
    return jsonify(mgr.set_muted(not mgr.muted))


@app.post("/api/live/interrupt")
@require_token
def api_live_interrupt():
    """Stop NOVA talking, because the user said so.

    Being unable to cut an assistant off mid-sentence is the difference
    between a conversation and a recital, and the microphone cannot do it
    here: the shipped voice policy is half-duplex, so NOVA is deaf for
    exactly as long as she is the one speaking. That leaves the surfaces to
    provide the interruption, and this is the one path they all use — the
    same one the automatic detector uses, so a deliberate interruption and a
    detected one leave NOVA in identical states.

    Harmless when she is already silent; the caller does not have to know.
    """
    mgr = desk_live.get_live_manager()
    data = request.get_json(silent=True) or {}
    # The caller that is about to send the user's words asks us not to tell
    # the model, because those words will. See LiveManager.barge_in.
    notify = bool(data.get("notify_model", True))
    return jsonify(mgr.barge_in(notify_model=notify))


@app.post("/api/live/text")
@require_token
def api_live_text():
    """Type into the live conversation instead of speaking into it.

    Typing and talking were two separate conversations: text went to the REST
    model and came back as text on screen, voice went to the live session and
    came back as sound, and neither knew the other had happened. So a question
    typed mid-conversation got a silent answer, and NOVA had no memory of it
    the moment you spoke again.

    Sent this way the typed words join the same session the voice is in. The
    answer is spoken *and* arrives as a transcript, which is what "reply in
    both" means, and the turn is part of one conversation either way.

    Returns not-connected when no session is live; the caller falls back to
    the REST path, which is the right behaviour when voice is simply off.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "reason": "empty"}), 400
    mgr = desk_live.get_live_manager()
    result = mgr.send_text(text)
    if result.get("ok"):
        # Echo the user's own line to the surfaces straight away. The model
        # transcribes what it *hears*, so a typed turn would otherwise leave
        # NOVA's reply on screen with nothing above it.
        publish_transcript(text, role="user")
    return jsonify(result)


@app.post("/api/live/screen")
@require_token
def api_live_screen():
    """Turn NOVA's screen awareness on or off.

    Off unless explicitly switched on, and switched off again whenever the
    voice session ends. Screen contents are the most sensitive thing NOVA can
    be given, so this is never implicit: nothing here is enabled by opening
    ambient mode, only by asking for it.
    """
    data = request.get_json(silent=True) or {}
    mgr = desk_live.get_live_manager()
    if "watching" in data:
        return jsonify(mgr.set_screen_share(bool(data.get("watching"))))
    return jsonify(mgr.set_screen_share(not mgr.screen_status()["watching"]))


@app.get("/api/live/status")
@require_token
def api_live_status():
    mgr = desk_live.get_live_manager()
    return jsonify(mgr.status())


@app.get("/api/audio/devices")
@require_token
def api_audio_devices():
    """The microphones this machine has, for the `mic_device` setting.

    PortAudio lists each device once per Windows audio API, so names are
    de-duplicated, keeping the entry on the host API the voice session itself
    prefers. The setting stores the name, which is what live_session matches.
    """
    try:
        import sounddevice as sd
    except Exception as e:
        return jsonify({"ok": False, "inputs": [], "error": f"audio unavailable: {e}"})
    try:
        preferred = desk_live._preferred_host_api()
    except Exception:
        preferred = None
    try:
        default_in = sd.default.device[0] if sd.default.device else None
        seen: dict[str, dict] = {}
        for i, d in enumerate(sd.query_devices()):
            if int(d.get("max_input_channels", 0) or 0) <= 0:
                continue
            name = str(d.get("name", "")).strip()
            if not name:
                continue
            entry = {"index": i, "name": name, "default": i == default_in}
            prior = seen.get(name)
            if prior is None or (preferred is not None and d.get("hostapi") == preferred):
                if prior is not None:
                    entry["default"] = entry["default"] or prior["default"]
                seen[name] = entry
        return jsonify({"ok": True, "inputs": list(seen.values())})
    except Exception as e:
        return jsonify({"ok": False, "inputs": [], "error": str(e)})


def _block_on_accept(server) -> None:
    """Accepted connections block on reads, whatever the process default.

    Python gives an accepted socket the process-wide default timeout. The
    browser sends nothing on the voice and event WebSockets, so a default
    (nova.is_online() used to set 4 s) made their reader thread treat four
    quiet seconds as a close; the handler ended mid-stream and the window
    silently stopped receiving the voice session. This has to happen at
    accept: by the time a route runs, the reader is already waiting with
    the old timeout.
    """
    accept = server.get_request

    def get_request():
        conn, addr = accept()
        conn.settimeout(None)
        return conn, addr

    server.get_request = get_request


@sock.route("/api/live/ws")
def api_live_ws(ws):
    """WebSocket for Live audio events.

    Authenticates via ?token= query param (browser WS can't set custom headers).
    Server pushes JSON text frames: {type, ts, ...data}. Client sends nothing
    (REST endpoints control lifecycle); server detects close via exception.
    Also forwards voice state events to the unified /ws/events bus.
    """
    token = request.args.get("token", "")
    if token != run_token:
        ws.close(401)
        return
    mgr = desk_live.get_live_manager()
    q = mgr.subscribe()
    idle = 0
    try:
        while True:
            try:
                idle = 0
                try:
                    ev = q.get(timeout=0.5)
                except queue.Empty:
                    # A quiet half-second is the normal state of a voice
                    # session, not a reason to hang up.
                    #
                    # This used to fall through to the bare `except
                    # Exception: break` below, so the socket closed after the
                    # first 500 ms in which NOVA happened to say nothing —
                    # which is immediately. The conversation panel therefore
                    # never received a transcript, and the greeting sat
                    # waiting eight seconds for an interface that had already
                    # been and gone.
                    #
                    # The ping is not decoration: without traffic there is no
                    # way to notice a peer that has gone away, and a
                    # subscriber queue nobody drains fills up and starts
                    # dropping the events other surfaces still want.
                    idle += 1
                    if idle >= 20:              # ~10 s
                        idle = 0
                        try:
                            ws.send('{"type":"ping"}')
                        except Exception:
                            break
                    continue
                ev_dict = ev.to_dict()
                ws.send(json.dumps(ev_dict))
                # Forward voice state events to unified event bus
                if ev_dict.get("type") == "state":
                    state_val = ev_dict.get("state", "")
                    # Every state the session can publish, named here.
                    #
                    # Anything missing fell through to "idle", and three of
                    # the states NOVA spends nearly all her time in were
                    # missing: a conversation showed an idle orb while she
                    # was listening, thinking about it, and talking back.
                    # The surfaces that open the voice socket directly were
                    # fine; the ambient bar and the telemetry panel, which
                    # deliberately read this bus instead, were not.
                    orb_map = {
                        "connecting": "thinking",
                        "connected": "listening",
                        "ready": "listening",
                        "listening": "listening",
                        "streaming": "listening",
                        "speaking": "speaking",
                        "muted": "idle",
                        "offline": "offline",
                        "disconnecting": "thinking",
                        "error": "error",
                        "closed": "offline",
                    }
                    orb_state = orb_map.get(state_val, "idle")
                    # publish_voice_state, not a bare voice_state event: it
                    # also records the state /api/system reports, and the
                    # HUD's VOICE pill showed "idle" for whole conversations.
                    publish_voice_state(state_val)
                    publish_event({"type": "orb_state", "state": orb_state, "ts": time.time()})
                elif ev_dict.get("type") == "user_transcript":
                    publish_event({"type": "transcript", "text": ev_dict.get("text", ""), "role": "user", "ts": time.time()})
                elif ev_dict.get("type") == "nova_transcript":
                    publish_event({"type": "transcript", "text": ev_dict.get("text", ""), "role": "nova", "ts": time.time()})
                elif ev_dict.get("type") in ("turn_complete", "interrupted",
                                             "tool_call", "tool_result",
                                             "screen_share", "screen_frame",
                                             "playback_complete",
                                             "vision_capture", "vision_captured",
                                             "vision_sent", "vision_failed",
                                             "vision_refused"):
                    # Forwarded so the ambient bar and the telemetry panel can
                    # follow the conversation without opening their own voice
                    # socket. One runtime, one event stream.
                    publish_event({**ev_dict, "ts": time.time()})
            except Exception:
                break
    finally:
        mgr.unsubscribe(q)


# ── local intelligence (Ollama model management) ─────────────────────────────

@app.get("/api/local-intelligence/status")
@require_token
def api_local_intel_status():
    try:
        from nova_intelligence.local_model_manager import get_model_manager
        mgr = get_model_manager()
        return jsonify(mgr.get_status())
    except Exception as e:
        return jsonify({"error": str(e), "runtime_available": False}), 500


@app.get("/api/local-intelligence/models")
@require_token
def api_local_intel_models():
    try:
        from nova_intelligence.local_model_manager import get_model_manager
        mgr = get_model_manager()
        return jsonify({"models": mgr.list_available()})
    except Exception as e:
        return jsonify({"error": str(e), "models": []}), 500


@app.post("/api/local-intelligence/set-model")
@require_token
def api_local_intel_set_model():
    data = request.get_json(silent=True) or {}
    model_id = data.get("model", "").strip()
    if not model_id:
        return jsonify({"ok": False, "error": "No model specified"}), 400
    try:
        from nova_intelligence.local_model_manager import get_model_manager
        mgr = get_model_manager()
        ok = mgr.set_current_model(model_id)
        # Update router's Ollama provider model
        import nova
        router = getattr(nova, "_nova_router", None)
        if router:
            ollama = router.get_provider("ollama")
            if ollama:
                ollama.model = model_id
        return jsonify({"ok": ok, "model": model_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/local-intelligence/download")
@require_token
def api_local_intel_download():
    data = request.get_json(silent=True) or {}
    model_id = data.get("model", "").strip()
    if not model_id:
        return jsonify({"ok": False, "error": "No model specified"}), 400
    try:
        from nova_intelligence.local_model_manager import get_model_manager
        mgr = get_model_manager()
        ok = mgr.download_model(model_id)
        return jsonify({"ok": ok, "model": model_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/local-intelligence/remove")
@require_token
def api_local_intel_remove():
    data = request.get_json(silent=True) or {}
    model_id = data.get("model", "").strip()
    if not model_id:
        return jsonify({"ok": False, "error": "No model specified"}), 400
    try:
        from nova_intelligence.local_model_manager import get_model_manager
        mgr = get_model_manager()
        ok = mgr.remove_model(model_id)
        return jsonify({"ok": ok, "model": model_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/local-intelligence/test")
@require_token
def api_local_intel_test():
    data = request.get_json(silent=True) or {}
    model_id = data.get("model", "")
    try:
        from nova_intelligence.local_model_manager import get_model_manager
        mgr = get_model_manager()
        result = mgr.test_model(model_id or None)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/local-intelligence/diagnostics")
@require_token
def api_local_intel_diagnostics():
    try:
        import nova
        router = getattr(nova, "_nova_router", None)
        conn = getattr(nova, "_connectivity", None)
        diag = {
            "connectivity": conn.snapshot() if conn else {"state": "unknown"},
            "router": router.snapshot() if router else {"providers": {}},
        }
        # Ollama status
        try:
            from nova_intelligence.local_model_manager import get_model_manager
            diag["ollama"] = get_model_manager().get_status()
        except Exception:
            diag["ollama"] = {"runtime_available": False}
        # Voice status
        diag["voice"] = {
            "stt": desk_voice.transcription_available(),
            "tts": desk_voice.audio_available(),
        }
        return jsonify(diag)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/local-intelligence/runtime")
@require_token
def api_local_intel_runtime():
    """Get Ollama runtime status and start/stop controls."""
    try:
        import nova
        runtime = getattr(nova, "_local_runtime", None)
        if not runtime:
            return jsonify({"error": "Runtime not initialized", "state": "UNKNOWN"}), 503
        return jsonify(runtime.health_check())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/local-intelligence/runtime/start")
@require_token
def api_local_intel_runtime_start():
    """Start Ollama runtime."""
    try:
        import nova
        runtime = getattr(nova, "_local_runtime", None)
        if not runtime:
            return jsonify({"ok": False, "error": "Runtime not initialized"}), 503
        ok = runtime.start()
        return jsonify({"ok": ok, "state": runtime.state.name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/local-intelligence/runtime/stop")
@require_token
def api_local_intel_runtime_stop():
    """Stop Ollama runtime (only if NOVA started it)."""
    try:
        import nova
        runtime = getattr(nova, "_local_runtime", None)
        if not runtime:
            return jsonify({"ok": False, "error": "Runtime not initialized"}), 503
        ok = runtime.stop()
        return jsonify({"ok": ok, "state": runtime.state.name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Offline Knowledge (ZIM) ─────────────────────────────────────────────────

@app.get("/api/offline-knowledge/status")
@require_token
def api_offline_knowledge_status():
    """Get status of offline knowledge base (ZIM files)."""
    try:
        from nova_intelligence.offline_knowledge import OfflineKnowledgeManager
        mgr = OfflineKnowledgeManager()
        return jsonify(mgr.status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/offline-knowledge/packages")
@require_token
def api_offline_knowledge_packages():
    """List available and installed packages."""
    try:
        from nova_intelligence.offline_knowledge import OfflineKnowledgeManager
        mgr = OfflineKnowledgeManager()
        return jsonify({
            "installed": mgr.installed_packages(),
            "available": mgr.available_packages(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/offline-knowledge/install")
@require_token
def api_offline_knowledge_install():
    """Install a ZIM package."""
    data = request.get_json(silent=True) or {}
    package_id = data.get("package_id", "")
    if not package_id:
        return jsonify({"ok": False, "error": "package_id required"}), 400
    try:
        from nova_intelligence.offline_knowledge import OfflineKnowledgeManager
        mgr = OfflineKnowledgeManager()
        ok = mgr.install(package_id)
        return jsonify({"ok": ok, "package_id": package_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/offline-knowledge/remove")
@require_token
def api_offline_knowledge_remove():
    """Remove an installed ZIM package."""
    data = request.get_json(silent=True) or {}
    package_id = data.get("package_id", "")
    if not package_id:
        return jsonify({"ok": False, "error": "package_id required"}), 400
    try:
        from nova_intelligence.offline_knowledge import OfflineKnowledgeManager
        mgr = OfflineKnowledgeManager()
        ok = mgr.remove(package_id)
        return jsonify({"ok": ok, "package_id": package_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── projects ───────────────────────────────────────────────────────────────────

@app.get("/api/projects")
@require_token
def api_projects_list():
    projects = desk_projects.list_projects()
    projects.sort(key=lambda p: p.get("updated", 0), reverse=True)
    for p in projects:
        try:
            p["conversations"] = desk_store.count_by_project(p["id"])
        except Exception:
            p["conversations"] = 0
    return jsonify({"projects": projects})


@app.post("/api/projects")
@require_token
def api_projects_create():
    data = request.get_json(silent=True) or {}
    p = desk_projects.create_project(
        name=data.get("name", ""),
        description=data.get("description", ""),
        instructions=data.get("instructions", ""),
        color=data.get("color", "violet"),
    )
    return jsonify({"ok": True, "project": p})


@app.get("/api/projects/<pid>")
@require_token
def api_projects_get(pid):
    p = desk_projects.get_project(pid)
    if p is None:
        return jsonify({"error": "not found"}), 404
    p["conversations"] = desk_store.count_by_project(pid)
    p["recent"] = desk_store.list_conversations(limit=50, project_id=pid)
    return jsonify({"project": p})


@app.patch("/api/projects/<pid>")
@require_token
def api_projects_patch(pid):
    data = request.get_json(silent=True) or {}
    p = desk_projects.update_project(
        pid,
        name=data.get("name"),
        description=data.get("description"),
        instructions=data.get("instructions"),
        color=data.get("color"),
    )
    if p is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "project": p})


@app.delete("/api/projects/<pid>")
@require_token
def api_projects_delete(pid):
    ok = desk_projects.delete_project(pid)
    return jsonify({"ok": ok})


# ── images / media ─────────────────────────────────────────────────────────────

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


@app.get("/api/images")
@require_token
def api_images():
    base = workspace_dir()
    images = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS:
            rel = str(p.relative_to(base))
            images.append({
                "name": rel,
                "size": p.stat().st_size,
                "modified": p.stat().st_mtime,
                "url": "/api/files/" + rel.replace("\\", "/"),
            })
    return jsonify({"images": images, "workspace": str(base)})


@app.get("/api/files/<path:path>")
@require_token
def api_file_content(path):
    """Serve a workspace file (inline preview or download)."""
    try:
        target = _safe_join(path)
    except (ValueError, OSError):
        return jsonify({"error": "Invalid path"}), 400
    if not target.is_file():
        return jsonify({"error": "Not found"}), 404
    mimetypes = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
        ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
        ".json": "application/json", ".csv": "text/csv; charset=utf-8",
        ".pdf": "application/pdf", ".py": "text/x-python; charset=utf-8",
        ".zip": "application/zip",
    }
    mime = mimetypes.get(target.suffix.lower(), "application/octet-stream")
    dl = request.args.get("dl") == "1"
    data = target.read_bytes()
    resp = Response(data, mimetype=mime)
    if dl or mime == "application/octet-stream":
        resp.headers["Content-Disposition"] = (
            "attachment; filename=\"" + target.name.replace("\"", "") + "\"")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── MCP status (real introspection, never fabricated) ─────────────────────────

def _mcp_status() -> dict:
    try:
        has_mcp = bool(getattr(_nova, "HAS_MCP", False))
    except Exception:
        has_mcp = False
    bridge = _ns("_mcp_bridge")
    servers, tools = [], []
    if has_mcp and bridge is not None:
        try:
            servers = list(getattr(bridge, "servers", lambda: [])() or []) or []
            # One shape for every server: a bare name and an object with
            # .name used to come back as a string and a dict respectively.
            servers = [{"name": s if isinstance(s, str) else (getattr(s, "name", None) or str(s))}
                       for s in servers]
        except Exception:
            servers = []
        try:
            decls = list(getattr(bridge, "gemini_declarations", lambda: [])() or []) or []
            # One entry per declaration. (This was a dict comprehension inside
            # a list, which collapsed every tool into a single {"name": last}.)
            tools = [{"name": d.get("name") if isinstance(d, dict) else str(d)} for d in decls]
        except Exception:
            tools = []
    return {
        "enabled": bool(has_mcp and bridge is not None),
        "package": bool(has_mcp),
        "connected_servers": len(servers) if has_mcp else 0,
        "servers": servers,
        "tools": tools,
        "message": ("MCP is disabled — run with the `mcp` package installed and a "
                    "server configured (e.g. NOVA_MCP_FILESYSTEM_ENABLED=1) to enable it."
                    if not has_mcp else
                    ("No MCP servers connected." if not servers else "")),
    }


@app.get("/api/mcp")
@require_token
def api_mcp():
    return jsonify(_mcp_status())


# ── tasks ──────────────────────────────────────────────────────────────────────

# ── ambient mode ──────────────────────────────────────────────────────────────
#
# NOVA collapses to a small always-on-top presence while she is doing work on
# the desktop, so the user can watch the actual application being driven. The
# desktop shell owns the windows, so it registers hooks here and the web layer
# only ever asks for a mode.

_ambient_hooks: dict = {"enter": None, "exit": None}
_ambient_mode: str = "full"

# Tools whose value to the user is *seeing the desktop*. Purely conversational
# turns must not yank the window away, so this list is deliberately narrow.
_DESKTOP_TOOLS = {
    "open_app", "close_app", "browser_control", "computer_control",
    "computer_settings", "file_controller", "vision",
}


def register_ambient_hooks(enter=None, exit=None) -> None:
    """Called by the desktop shell to expose real window control."""
    _ambient_hooks["enter"] = enter
    _ambient_hooks["exit"] = exit
    log.info("[AMBIENT] hooks registered (enter=%s exit=%s)", bool(enter), bool(exit))


def set_ambient_mode(mode: str) -> dict:
    """Switch NOVA between the full command centre and the ambient presence."""
    global _ambient_mode
    mode = "ambient" if mode == "ambient" else "full"
    if mode == _ambient_mode:
        return {"ok": True, "mode": mode, "changed": False}
    hook = _ambient_hooks["enter"] if mode == "ambient" else _ambient_hooks["exit"]
    if hook is None:
        # Headless/browser runs have no window to move; say so rather than
        # reporting a transition that did not happen.
        return {"ok": False, "mode": _ambient_mode, "reason": "no desktop window"}
    try:
        hook()
    except Exception as e:
        log.warning("[AMBIENT] %s hook failed: %s", mode, e)
        return {"ok": False, "mode": _ambient_mode, "reason": str(e)}
    _ambient_mode = mode
    publish_event({"type": "ambient", "mode": mode, "ts": time.time()})
    log.info("[AMBIENT] mode -> %s", mode)
    return {"ok": True, "mode": mode, "changed": True}


@app.post("/api/ambient")
@require_token
def api_ambient():
    data = request.get_json(silent=True) or {}
    return jsonify(set_ambient_mode((data.get("mode") or "full").strip()))


@app.get("/api/ambient")
@require_token
def api_ambient_get():
    return jsonify({
        "ok": True,
        "mode": _ambient_mode,
        "available": bool(_ambient_hooks["enter"]),
    })


def _agent_key(agent_id: str, tool: str = "", name: str = "") -> str:
    """The roster agent that owns a runtime agent/tool id.

    The chat path names agents after the tool they run ("agent-web_search-3f9c")
    and its own thinking "thinking-<turn>", which is NOVA herself.
    """
    if tool:
        return agent_activity.agent_for_tool(tool)
    m = re.match(r"agent-(.+)-[^-]+$", agent_id or "")
    return agent_activity.agent_for_tool(m.group(1) if m else (name or ""))


# ── command-centre telemetry ──────────────────────────────────────────────────
#
# Everything the HUD renders comes from here, and every field is measured. The
# reference design is dense with readouts; a dense UI full of invented numbers
# would be worse than no UI, so nothing below is synthesised — if a value is
# unavailable the field is omitted and the panel renders it as "--".

_AGENT_ROSTER = [tuple(a) for a in agent_activity.AGENTS]

_net_last = {"t": 0.0, "sent": 0, "recv": 0}

try:  # prime the CPU sampler — the first call always returns 0.0
    import psutil as _psutil_prime
    _psutil_prime.cpu_percent(interval=None)
except Exception:
    pass


def _vitals() -> dict:
    """Real host metrics. Fields are omitted when the platform cannot supply them."""
    out: dict = {}
    try:
        import psutil
    except Exception:
        return out
    try:
        out["cpu_pct"] = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        out["mem_pct"] = vm.percent
        out["mem_used_gb"] = round(vm.used / 1e9, 2)
        out["mem_total_gb"] = round(vm.total / 1e9, 2)
    except Exception:
        pass
    try:
        now = time.time()
        n = psutil.net_io_counters()
        if _net_last["t"] and now > _net_last["t"]:
            dt = now - _net_last["t"]
            out["up_bps"] = int((n.bytes_sent - _net_last["sent"]) / dt)
            out["down_bps"] = int((n.bytes_recv - _net_last["recv"]) / dt)
        _net_last.update(t=now, sent=n.bytes_sent, recv=n.bytes_recv)
    except Exception:
        pass
    try:
        temps = psutil.sensors_temperatures() or {}
        for readings in temps.values():
            if readings and readings[0].current:
                out["thermal_c"] = round(readings[0].current, 1)
                break
    except Exception:
        pass
    return out


@app.get("/api/system")
@require_token
def api_system():
    """Live telemetry for the command-centre HUD."""
    import nova

    # ── agents: every executor reports to one registry ──────────────────────
    agents = [{k: a[k] for k in ("id", "label", "role", "state", "action", "task_id")}
              | {"since": a.get("since"), "busy": len(a.get("work") or [])}
              for a in agent_activity.snapshot()]

    # ── tasks: real task manager state, active first ────────────────────────
    tasks = []
    try:
        tm = _ns("_task_manager")
        if tm is not None:
            listing = tm.recent(12) if hasattr(tm, "recent") else tm.list()[-12:][::-1]
            for t in listing:
                d = t.to_dict() if hasattr(t, "to_dict") else {}
                tasks.append(_task_brief(d))
    except Exception as e:
        log.debug("task listing failed: %s", e)

    # ── intelligence ────────────────────────────────────────────────────────
    provider = ""
    model = ""
    try:
        r = getattr(nova, "_nova_router", None)
        if r is not None:
            provider = getattr(r, "_last_provider_used", "") or ""
            prov = r.get_provider(provider) if provider else None
            if prov is not None:
                model = str(getattr(prov, "model", "") or getattr(prov, "_model", "") or "")
    except Exception:
        pass

    # A voice conversation does not go through the router, so while one is
    # running the engine and latency are the live session's own -- otherwise
    # the HUD showed ENGINE ---- and LATENCY -- throughout a conversation.
    turn_ms = _last_turn_ms
    try:
        live = desk_live.get_live_manager().status()
        if live.get("state") not in (None, "idle", "closed", "error", "offline"):
            model = str(live.get("model") or model).replace("models/", "")
            provider = live.get("engine") or provider
            turn_ms = live.get("last_turn_ms") or turn_ms
    except Exception:
        pass

    conn = "unknown"
    try:
        r = getattr(nova, "_nova_router", None)
        if r is not None:
            conn = r.connectivity.state.value
    except Exception:
        pass

    return jsonify({
        "ts": time.time(),
        "uptime_s": round(time.time() - _started_at, 1),
        "vitals": _vitals(),
        "agents": agents,
        "tasks": tasks,
        "intelligence": {
            "provider": provider,
            "model": model,
            "connectivity": conn,
            "brain_ready": _brain_ready,
            "last_turn_ms": turn_ms,
        },
        "voice": {
            "state": _last_voice_state,
        },
        "tls": _tls_status(),
    })


def _task_brief(d: dict) -> dict:
    """What the task list shows for one task. Every figure is counted."""
    msgs = d.get("agent_messages") or []
    review = (d.get("reviews") or [None])[-1]
    return {
        "id": d.get("id", ""),
        "title": d.get("title", ""),
        "status": d.get("status", ""),
        "phase": d.get("phase", ""),
        "steps": len(d.get("steps", []) or []),
        "steps_done": d.get("steps_done", 0),
        "progress": d.get("progress", 0),
        "seconds_remaining": d.get("seconds_remaining"),
        "estimated_duration_s": d.get("estimated_duration_s", 0),
        "elapsed_s": d.get("elapsed_s", 0),
        "current": d.get("current", ""),
        "next": d.get("next", ""),
        "agents": d.get("agents", []),
        "reason": d.get("reason_for_stop", ""),
        "artifacts": [a.get("path", "") for a in d.get("artifacts") or []][:5],
        "messages": msgs[-4:],
        "review": ({"round": review.get("round"), "passed": review.get("passed"),
                    "issues": [i.get("problem", "") for i in review.get("issues") or []]}
                   if review else None),
        "updated": d.get("updated", 0),
    }


@app.get("/api/tasks/<task_id>")
@require_token
def api_task(task_id: str):
    """One task in full: steps, agent messages, reviews, history."""
    tm = _ns("_task_manager")
    t = tm.get(task_id) if tm is not None else None
    if t is None:
        return jsonify({"ok": False, "error": "no such task"}), 404
    return jsonify({"ok": True, "task": t.to_dict()})


@app.post("/api/tasks/<task_id>/cancel")
@require_token
def api_task_cancel(task_id: str):
    tm = _ns("_task_manager")
    ok = bool(tm is not None and tm.cancel(task_id, force=True))
    return jsonify({"ok": ok, **({} if ok else {"error": "that task is not running"})})


@app.post("/api/tasks/<task_id>/retry")
@require_token
def api_task_retry(task_id: str):
    """Run a finished task again, as a new task that remembers its parent."""
    tm = _ns("_task_manager")
    t = tm.retry(task_id) if tm is not None and hasattr(tm, "retry") else None
    if t is None:
        return jsonify({"ok": False, "error": "only a task that has stopped can be retried"}), 400
    return jsonify({"ok": True, "task_id": t.id})


@app.get("/api/tasks")
@require_token
def api_tasks():
    tm = _ns("_task_manager")
    if tm is None:
        return jsonify({"tasks": [], "status": "Task manager not initialized."})
    tasks = []
    try:
        raw = getattr(tm, "list", None)
        if callable(raw):
            # to_dict(), not the Task objects themselves: jsonify would
            # serialise the raw dataclass fields and skip the computed
            # progress and countdown.
            tasks = [t.to_dict() if hasattr(t, "to_dict") else t for t in raw()]
    except Exception:
        tasks = []
    if not tasks and callable(getattr(tm, "exec_command", None)):
        try:
            out = tm.exec_command("status", task_id="", title="", steps=[], meta={})
            return jsonify({"tasks": [], "status": str(out)})
        except Exception as e:
            return jsonify({"tasks": [], "status": f"Task manager error: {e}"})
    return jsonify({"tasks": tasks, "status": "ok"})


# ── permissions ────────────────────────────────────────────────────────────────

@app.get("/api/permissions")
@require_token
def api_permissions():
    return jsonify({
        "categories": list(desk_settings._PERMISSION_CATEGORIES),  # noqa: SLF001
        "permissions": desk_settings.get("permissions", {}),
        "tool_map": desk_confirm.tool_scopes(),
        "policy": desk_settings.get("confirm_policy", "prompt"),
    })


# ── files ─────────────────────────────────────────────────────────────────────

def _safe_join(name: str) -> Path:
    name = name.replace("\\", "/")
    if ".." in name.split("/"):
        raise ValueError("Invalid path")
    base = workspace_dir()
    p = (base / name).resolve()
    if not str(p).startswith(str(base.resolve())):
        raise ValueError("Invalid path")
    return p


@app.get("/api/files")
@require_token
def api_files_list():
    base = workspace_dir()
    files = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            files.append({
                "name": str(p.relative_to(base)),
                "size": p.stat().st_size,
                "modified": p.stat().st_mtime,
            })
    return jsonify({"files": files, "workspace": str(base)})


@app.post("/api/files/upload")
@require_token
def api_files_upload():
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "No file uploaded"}), 400
    name = (Path(f.filename or "file.bin").name or "file.bin")
    try:
        target = _safe_join("attachments/" + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        f.save(target)
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "name": name, "path": str(target)})


@app.post("/api/files/save")
@require_token
def api_files_save():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    content = data.get("content") or ""
    if not name:
        return jsonify({"error": "No filename"}), 400
    try:
        target = _safe_join(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "path": str(target), "workspace": str(workspace_dir())})


# ── settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
@require_token
def api_settings_get():
    return jsonify({"settings": desk_settings.all(), "toggles": desk_settings.toggles()})


@app.post("/api/settings")
@require_token
def api_settings_post():
    data = request.get_json(silent=True) or {}
    # permissions are applied as a merge; everything else goes through set_many
    if isinstance(data.get("permissions"), dict):
        desk_settings.set_many({"permissions": data["permissions"]})
        data.pop("permissions", None)
    desk_settings.set_many(data)
    # The setting has already been applied locally. Syncing it to the account
    # happens afterwards, on a background thread, so changing a preference is
    # never gated on the network.
    desk_account.push_preferences_async(data)
    return jsonify({"ok": True, "settings": desk_settings.all()})


@app.post("/api/settings/profile")
@require_token
def api_settings_profile():
    data = request.get_json(silent=True) or {}
    name = (data.get("user_name") or "").strip()
    if name:
        desk_settings.set_many({"user_name": name})
        _META["user_name"] = name
    # How the name is *said*, which the spelling often does not tell you.
    # Stored whenever it is offered so the voice layer can be told once
    # rather than mispronouncing it every session.
    if "user_name_pronunciation" in data:
        say = (data.get("user_name_pronunciation") or "").strip()
        desk_settings.set_many({"user_name_pronunciation": say})
        _META["user_name_pronunciation"] = say
    return jsonify({"ok": True, "user_name": desk_settings.get("user_name") or "User"})


# ── onboarding / credentials ──────────────────────────────────────────────────
# The Gemini key NEVER travels through these endpoints in the server→client
# direction. BYOK keys are accepted once, sealed with DPAPI, and only ever
# reported back masked.

def _creds():
    from desk import creds as desk_creds
    return desk_creds


@app.get("/api/onboarding")
@require_token
def api_onboarding_status():
    return jsonify({"ok": True, "auth": _auth_status()})


@app.get("/api/accounts")
@require_token
def api_accounts():
    """Which external services NOVA is connected to, and what she may do.

    Never contains a token: the account layer keeps credentials in the
    credential store and returns only metadata, because everything here
    reaches the interface and can reach the model.
    """
    try:
        from integrations.accounts import PROVIDERS, get_account_store
        store = get_account_store()
        connected = {c["provider"]: c for c in store.connected()}
        out = []
        for name, spec in sorted(PROVIDERS.items()):
            if name.endswith("_placeholder"):
                continue
            entry = connected.get(name) or {"provider": name,
                                            "status": "disconnected",
                                            "grants": []}
            entry["description"] = spec.description
            entry["supports"] = sorted(g.value for g in spec.supports)
            out.append(entry)
        return jsonify({"ok": True, "accounts": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.post("/api/accounts/gmail/connect")
@require_token
def api_accounts_gmail_connect():
    """Start Google's consent flow. Opens a browser on this machine.

    Read-only: the scope requested cannot send, delete or archive anything.
    Run on a worker thread because the flow blocks until the person either
    consents or closes the window, and the request must not hold the server.
    """
    import threading

    def _run():
        try:
            from integrations.gmail import authorise
            result = authorise()
            log.info("[GMAIL] connected as %s", result.get("account", "?"))
            publish_event({"type": "account_changed", "provider": "gmail",
                           "ok": True, "ts": time.time()})
        except Exception as e:
            log.warning("[GMAIL] consent failed: %s", e)
            publish_event({"type": "account_changed", "provider": "gmail",
                           "ok": False, "error": str(e)[:200],
                           "ts": time.time()})

    threading.Thread(target=_run, daemon=True,
                     name="GmailConsent").start()
    return jsonify({"ok": True, "started": True})


@app.post("/api/accounts/<provider>/disconnect")
@require_token
def api_accounts_disconnect(provider):
    """Forget the account and destroy the credential."""
    try:
        from integrations.accounts import get_account_store
        get_account_store().disconnect(provider)
        publish_event({"type": "account_changed", "provider": provider,
                       "ok": True, "ts": time.time()})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.post("/api/onboarding/byok")
@require_token
def api_onboarding_byok():
    data = request.get_json(silent=True) or {}
    key = (data.get("api_key") or "").strip()
    try:
        st = _creds().set_byok(key)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not store key securely: {e}"}), 500
    log.info("BYOK credential stored (%s)", st.get("byok_masked", ""))
    return jsonify({"ok": True, "auth": st})


@app.delete("/api/onboarding/byok")
@require_token
def api_onboarding_byok_clear():
    st = _creds().clear_byok_and_apply()
    log.info("BYOK credential removed; effective mode now %s", st.get("mode"))
    return jsonify({"ok": True, "auth": st})


@app.post("/api/onboarding/offline")
@require_token
def api_onboarding_offline():
    st = _creds().choose_offline()
    return jsonify({"ok": True, "auth": st})


@app.post("/api/onboarding/cloud")
@require_token
def api_onboarding_cloud():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    try:
        st = _creds().choose_cloud(url)
    except Exception as e:
        # honest failure: no fabricated provider, no fake success
        return jsonify({"ok": False, "error": str(e)[:200],
                        "auth": _auth_status()}), 502
    return jsonify({"ok": True, "auth": st})


@app.post("/api/onboarding/complete")
@require_token
def api_onboarding_complete():
    _creds().mark_onboarded()
    return jsonify({"ok": True, "auth": _auth_status()})


@app.get("/api/update")
@require_token
def api_update_status():
    """Automatic updates: what is installed, what is staged, what happened."""
    from desk import updater
    s = updater.status()
    s["staged"] = updater.staged()
    try:
        import json as _json
        s["history"] = _json.loads((updater.update_dir() / "history.json").read_text("utf-8"))[-5:]
    except Exception:
        s["history"] = []
    return jsonify({"ok": True, **s})


@app.get("/api/live/token")
@require_token
def api_live_token():
    """Ephemeral Gemini Live credential via the NOVA backend.

    Production flow: Desktop → NOVA backend mints short-lived Live token →
    Desktop opens the Live session directly. Requires a deployed NOVA Cloud
    backend; without one this returns an honest 503 (the long-lived Gemini key
    is never shipped to the client).
    """
    c = _creds()
    cloud = c.current_cloud_client()
    if not cloud.active:
        return jsonify({"ok": False, "error":
                        "Live tokens require NOVA Cloud (set cloud_url). "
                        "Voice currently uses the local on-device stack."}), 503
    try:
        tok = cloud.mint_live_token()
    except Exception as e:
        return jsonify({"ok": False, "error": f"NOVA Cloud unreachable: {type(e).__name__}"}), 502
    return jsonify({"ok": True, **tok})


# ── confirmations ─────────────────────────────────────────────────────────────

@app.get("/api/confirm/pending")
@require_token
def api_confirm_pending():
    return jsonify({"pending": desk_confirm.store.pending()})


@app.post("/api/confirm")
@require_token
def api_confirm_decision():
    data = request.get_json(silent=True) or {}
    req_id = data.get("id", "")
    decision = str(data.get("decision", "")).strip().lower()
    if not req_id:
        return jsonify({"error": "Missing id"}), 400
    if decision in ("yes", "y", "true", "1"):
        ok = desk_confirm.store.decide(req_id, True)
    elif decision in ("no", "n", "false", "0"):
        ok = desk_confirm.store.decide(req_id, False)
    else:
        return jsonify({"error": "Decision must be yes or no"}), 400
    return jsonify({"ok": ok})


# ── runner ────────────────────────────────────────────────────────────────────

_server = None


def shutdown_desk_server() -> None:
    """Stop the Werkzeug server (called by the desktop shell on window close)."""
    global _server
    srv = _server
    _server = None
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
    for ev in _stop_events.values():
        ev.set()


def _serve(srv):
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ── 3D Mind Map data ──────────────────────────────────────────────────────────

import math

REGION_COLORS = {
    "core":      "#2dd4a8",
    "memory":    "#a78bfa",
    "working":   "#38bdf8",
    "agents":    "#fb7185",
    "knowledge": "#fbbf24",
    "rim":       "#64748b",
}

def _collect_mind_map_nodes():
    """Assemble real nodes from all NOVA data sources."""
    nodes = []
    nid = 0

    # Core — the orchestrator itself
    nodes.append({
        "id": f"n{nid}", "region": "core", "type": "core",
        "label": "NOVA", "detail": "Master orchestrator",
        "size": 1.0, "accent": REGION_COLORS["core"],
    })
    nid += 1

    # Memory nodes -- from the running memory system, the same source as
    # /api/memory. (These used to be read from files beside the source code,
    # which in the packaged app is inside the bundle: the map showed no
    # memories, or stale ones, while the Memory page showed the real ones.)
    # `updated` travels with each node so the page can forget exactly that
    # record through DELETE /api/memory/records.
    lm = _ns("_living_memory")
    if lm is not None:
        try:
            live = [r for r in lm.all_records() if not r.get("superseded_by")]
            live.sort(key=lambda r: r.get("importance", 0.0), reverse=True)
            for rec in live[:40]:
                text = str(rec.get("text", ""))
                nodes.append({
                    "id": f"n{nid}", "region": "memory", "type": "memory",
                    "label": text[:60], "detail": text,
                    "size": 0.4 + 0.3 * min(1.0, float(rec.get("importance", 0.0) or 0.0)),
                    "accent": REGION_COLORS["memory"],
                    "source_type": rec.get("type", "unknown"),
                    "source_id": rec.get("id", ""),
                    "updated": rec.get("updated", 0.0),
                    "confirmed": bool(rec.get("confirmed", True)),
                })
                nid += 1
        except Exception as e:
            log.warning("mind map: living memory listing failed: %s", e)

    try:
        for text in list(_ns("_memory_texts", []) or [])[-30:]:
            text = text if isinstance(text, str) else str(text)
            nodes.append({
                "id": f"n{nid}", "region": "memory", "type": "fact",
                "label": text[:60], "detail": text,
                "size": 0.4, "accent": REGION_COLORS["memory"],
            })
            nid += 1
    except Exception:
        pass

    # Agent nodes
    try:
        import nova_agents
        for agent in nova_agents.AgentType:
            nodes.append({
                "id": f"n{nid}", "region": "agents", "type": "agent",
                "label": agent.name, "detail": f"Agent: {agent.name}",
                "size": 0.6, "accent": REGION_COLORS["agents"],
            })
            nid += 1
    except Exception:
        pass

    # Tool nodes
    try:
        tool_decl = getattr(_nova, "TOOL_DECLARATIONS", [])
        categories = {}
        for t in tool_decl:
            cat = t.get("category", "general")
            categories.setdefault(cat, []).append(t.get("name", "unknown"))
        for cat, tools in categories.items():
            for tool_name in tools:
                nodes.append({
                    "id": f"n{nid}", "region": "knowledge", "type": "tool",
                    "label": tool_name, "detail": f"Tool: {tool_name} ({cat})",
                    "size": 0.35, "accent": REGION_COLORS["knowledge"],
                    "category": cat,
                })
                nid += 1
    except Exception:
        pass

    # Working memory nodes (current session)
    try:
        cid = desk_store.current_conversation_id() if hasattr(desk_store, "current_conversation_id") else None
        if cid:
            msgs = desk_store.get_messages(cid, limit=20)
            for m in msgs:
                if m.get("role") == "user":
                    nodes.append({
                        "id": f"n{nid}", "region": "working", "type": "message",
                        "label": m.get("content", "")[:60],
                        "detail": m.get("content", ""),
                        "size": 0.3, "accent": REGION_COLORS["working"],
                    })
                    nid += 1
    except Exception:
        pass

    return nodes


_mind_map_edge_cache: dict = {"key": None, "edges": None}
_mind_map_edge_lock = threading.Lock()


def _collect_mind_map_edges(nodes):
    """Edges between nodes: semantic when NOVA's embedder is loaded, else regional.

    Uses the embedder NOVA already holds (nova_state._embedder). This used to
    load a fresh SentenceTransformer from a folder beside the source on every
    request -- seconds and hundreds of MB per map view, and again for every
    node clicked -- and that folder does not exist in the packaged app. The
    result is cached against the node texts, so a click reuses it.
    """
    if len(nodes) < 2:
        return []
    key = tuple((n["id"], n.get("detail", n.get("label", ""))) for n in nodes)
    with _mind_map_edge_lock:
        if _mind_map_edge_cache["key"] == key:
            return list(_mind_map_edge_cache["edges"])
    edges = _compute_mind_map_edges(nodes)
    with _mind_map_edge_lock:
        _mind_map_edge_cache["key"] = key
        _mind_map_edge_cache["edges"] = list(edges)
    return edges


def _compute_mind_map_edges(nodes):
    model = _ns("_embedder")
    if model is not None:
        try:
            return _semantic_edges(model, nodes)
        except Exception as e:
            log.warning("mind map: semantic edges failed, using regional: %s", e)
    return _regional_edges(nodes)


def _semantic_edges(model, nodes):
    import faiss
    import numpy as np
    edges, seen = [], set()
    texts = [n.get("detail", n.get("label", "")) for n in nodes]
    embeddings = np.array(model.encode(texts, show_progress_bar=False), dtype="float32")
    faiss.normalize_L2(embeddings)
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    D, I = index.search(embeddings, min(4, len(nodes)))
    for i, (dists, neighbors) in enumerate(zip(D, I)):
        for j, (dist, neighbor) in enumerate(zip(dists, neighbors)):
            if i != neighbor and dist > 0.35 and j < 3:
                pair = tuple(sorted([nodes[i]["id"], nodes[neighbor]["id"]]))
                if pair not in seen:
                    seen.add(pair)
                    edges.append({"source": pair[0], "target": pair[1],
                                  "weight": float(dist), "type": "semantic"})
    return edges


def _regional_edges(nodes):
    edges = []
    region_groups = {}
    for n in nodes:
        region_groups.setdefault(n["region"], []).append(n["id"])
    for region, nids in region_groups.items():
        for i in range(len(nids)):
            for j in range(i + 1, min(i + 4, len(nids))):
                edges.append({
                    "source": nids[i], "target": nids[j],
                    "weight": 0.5, "type": "regional",
                })
    return edges


@app.get("/api/mind-map")
@require_token
def api_mind_map():
    """Return the skeleton mind map: all nodes, edges, and stats."""
    try:
        nodes = _collect_mind_map_nodes()
        edges = _collect_mind_map_edges(nodes)

        region_counts = {}
        for n in nodes:
            r = n["region"]
            region_counts[r] = region_counts.get(r, 0) + 1

        stats = {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "regions": region_counts,
            "has_faiss": False,
        }
        try:
            import faiss
            stats["has_faiss"] = True
        except ImportError:
            pass

        return jsonify({"nodes": nodes, "edges": edges, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/mind-map/node/<node_id>")
@require_token
def api_mind_map_node(node_id):
    """Return detail for a single node (lazy-loaded on click)."""
    try:
        nodes = _collect_mind_map_nodes()
        node = next((n for n in nodes if n["id"] == node_id), None)
        if not node:
            return jsonify({"error": "Node not found"}), 404

        detail = {
            "node": node,
            "connections": [],
        }

        # Find connected edges
        edges = _collect_mind_map_edges(nodes)
        connected_ids = set()
        for e in edges:
            if e["source"] == node_id:
                connected_ids.add(e["target"])
            elif e["target"] == node_id:
                connected_ids.add(e["source"])

        detail["connections"] = [n for n in nodes if n["id"] in connected_ids]
        return jsonify(detail)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Mind Map live observer WebSocket ──────────────────────────────────────────

_observer_clients: list = []
_observer_lock = threading.Lock()

def _notify_observers(event_type: str = "update"):
    """Push a lightweight event to all connected observer clients."""
    msg = json.dumps({"type": event_type, "ts": time.time()})
    with _observer_lock:
        dead = []
        for ws in _observer_clients:
            try:
                ws.send(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _observer_clients.remove(ws)

@sock.route("/ws/observe")
def ws_observe(ws):
    """Read-only WebSocket for mind map live updates. Never touches voice/chat socket."""
    token = request.args.get("token", "")
    if token != run_token:
        return
    with _observer_lock:
        _observer_clients.append(ws)
    try:
        # Keep alive — receive only pings, never send data except via _notify_observers
        while True:
            data = ws.receive(timeout=30)
            if data is None:
                break
    except Exception:
        pass
    finally:
        with _observer_lock:
            if ws in _observer_clients:
                _observer_clients.remove(ws)


def _start_account_session() -> None:
    """Restore the signed-in account, adopt its name, and start telemetry.

    Everything here is either local (reading the cached session from the OS
    keystore) or dispatched to a background thread. Startup never waits on the
    network: a machine with no connection reaches the same voice-ready state,
    just without a fresh preference pull.
    """
    import nova_account
    acct = nova_account.account()
    if not acct.configured:
        log.info("[DESK] no NOVA Cloud configured; running local-only")
        return
    if not acct.signed_in:
        log.info("[DESK] NOVA Cloud configured; no account signed in")
        return

    name = acct.display_name
    if name:
        # NOVA already knows who this is; the user should not be asked again.
        desk_settings.set_many({"user_name": name})
        _META["user_name"] = name

    acct.start_background()
    acct.emit("NOVA_STARTED", surface="desktop",
              app_version=nova_account.APP_VERSION)
    desk_account.pull_preferences_async()
    log.info("[DESK] signed in as %s (%s)", name or "?",
             "online" if acct.online else "cached")


def run_desk_server(meta, port: int | None = None) -> None:
    """Entrypoint called by nova.py --desk. Blocks (serves until stopped)."""
    global _META, run_token, _started_at, _server
    global publish_event, publish_voice_state, publish_transcript
    global publish_task_start, publish_task_done
    global publish_agent_start, publish_agent_progress, publish_agent_done
    global publish_orb_state
    _META = dict(meta or {})
    run_token = secrets.token_hex(16)
    _started_at = time.time()

    ok, msg = desk_confirm.install(ui_mode=True)
    log.info("[DESK] %s", msg)

    # Account surface. Registered before the server starts so the SPA can ask
    # who is signed in on its very first request. If no NOVA Cloud backend is
    # configured this still answers -- with configured:false -- and NOVA runs
    # entirely locally.
    try:
        desk_account.register(app, require_token, _META)
        _start_account_session()
    except Exception as e:                       # never block startup on this
        log.warning("[DESK] account surface unavailable: %s", e)

    # Offline model preparation (first-run and Settings), with real progress.
    try:
        from desk import offline_model
        offline_model.register(app, require_token)
    except Exception as e:
        log.warning("[DESK] offline model surface unavailable: %s", e)

    # Document library. Registered here so the SPA can list and add documents;
    # the tool path reaches the same library through nova_core.rag.api.
    try:
        from nova_core.rag import api as rag_api
        rag_api.register(app, require_token)
        log.info("[DESK] document library ready")
    except Exception as e:
        log.warning("[DESK] document library unavailable: %s", e)

    if port is None:
        port = int(os.getenv("NOVA_DESK_PORT", "") or 8765)
    try:
        port = int(_resolve("DESK_PORT", port) or port)
    except Exception:
        pass

    from werkzeug.serving import make_server

    # Stop logging a line per HTTP request. Two surfaces poll this server
    # several times a second, and every one of those was formatted and
    # written to nova.log through the root handler -- 8 MB of
    # "GET /api/confirm/pending 200" in a single session, constant disk I/O
    # on the audio path, and any real error buried a thousand lines deep.
    # Warnings and errors from the server still come through.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    host = os.getenv("NOVA_DESK_HOST", "127.0.0.1")
    _server = make_server(host, port, app, threaded=True)
    _block_on_accept(_server)
    print(f"[NOVA] 🖥  NOVA Desktop backend ready at http://{host}:{port}")

    # ── Unified event bus (voice state + agent lifecycle + transcripts) ────────
    _event_clients: list = []
    _event_lock = threading.Lock()

    def publish_event(event: dict):
        msg = json.dumps(event, ensure_ascii=False)
        with _event_lock:
            dead = []
            for cws in _event_clients:
                try:
                    cws.send(msg)
                except Exception:
                    dead.append(cws)
            for cws in dead:
                _event_clients.remove(cws)

    def publish_voice_state(state: str, **extra):
        global _last_voice_state
        _last_voice_state = state
        publish_event({"type": "voice_state", "state": state, "ts": time.time(), **extra})

    def publish_transcript(text: str, role: str = "nova"):
        publish_event({"type": "transcript", "text": text, "role": role, "ts": time.time()})

    def publish_task_start(task_id: str, label: str = ""):
        publish_event({"type": "task_start", "task_id": task_id, "label": label or "Processing", "ts": time.time()})

    def publish_task_done(task_id: str, ok: bool = True, summary: str = ""):
        publish_event({"type": "task_done", "task_id": task_id, "ok": ok, "summary": summary, "ts": time.time()})

    def publish_agent_start(agent_id: str, task_id: str, name: str, action: str = "", tool: str = ""):
        agent_activity.begin(_agent_key(agent_id, tool, name), action or name,
                             source="chat", task_id=task_id, key=f"chat:{agent_id}")
        publish_event({"type": "agent_start", "agent_id": agent_id, "task_id": task_id, "name": name, "action": action, "tool": tool, "ts": time.time()})

    def publish_agent_progress(agent_id: str, action: str = "", tool: str = ""):
        # The registry's row says what the agent started on; progress is
        # the event below, not a rewrite of that row.
        publish_event({"type": "agent_progress", "agent_id": agent_id, "action": action, "tool": tool, "ts": time.time()})

    def publish_agent_done(agent_id: str, ok: bool = True, summary: str = ""):
        # By the exact id it started under. Matching by substring left
        # "research" lit forever: it is not a substring of
        # "agent-web_search-3f9c".
        agent_activity.end(f"chat:{agent_id}")
        publish_event({"type": "agent_done", "agent_id": agent_id, "ok": ok, "summary": summary, "ts": time.time()})

    def publish_orb_state(state: str):
        publish_event({"type": "orb_state", "state": state, "ts": time.time()})

    # Make publish functions accessible from chat module
    import desk.chat as _chat_mod
    _chat_mod._publish_event = publish_event
    _chat_mod._publish_task_start = publish_task_start
    _chat_mod._publish_task_done = publish_task_done
    _chat_mod._publish_agent_start = publish_agent_start
    _chat_mod._publish_agent_progress = publish_agent_progress
    _chat_mod._publish_agent_done = publish_agent_done
    _chat_mod._publish_orb_state = publish_orb_state

    @sock.route("/ws/events")
    def ws_events(cws):
        """Unified event WebSocket for the NOVA UI."""
        token = request.args.get("token", "")
        if token != run_token:
            cws.close(401)
            return
        with _event_lock:
            _event_clients.append(cws)
        try:
            while True:
                # receive() returning None is an *idle* timeout, not a close.
                # Breaking on it tore down every event client every few seconds
                # and reconnected in a loop, so agent/task/voice events were
                # regularly lost. Keep the socket open and ping to hold it.
                data = cws.receive(timeout=20)
                if data is None:
                    try:
                        cws.send('{"type":"ping"}')
                    except Exception:
                        break          # peer really is gone
                    continue
        except Exception:
            pass
        finally:
            with _event_lock:
                if cws in _event_clients:
                    _event_clients.remove(cws)

    # Voice is started by the interface, not by a timer here.
    #
    # This used to spawn a thread that slept two seconds and then opened the
    # Live session regardless of what else was happening. Two seconds after
    # the HTTP server binds is not "the application is ready" — it is usually
    # before the window has painted. The user's report was NOVA speaking over
    # her own startup, a fragment of a greeting delivered to an interface that
    # did not exist yet, and this timer is where that came from.
    #
    # The SPA calls POST /api/live/start once it has loaded and attached to
    # the event stream, which is the only moment that actually means ready.
    print(f"[NOVA] Press Ctrl+C to stop.")
    _serve(_server)