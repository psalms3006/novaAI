"""Generators for context/self/nova.md's AUTO blocks.

Each generator is a pure function: given a snapshot of the relevant
registry, it returns markdown -- a summary, not a wall of source. Every
generator reads from the same site the runtime reads from:

- capabilities  <- nova.TOOL_DECLARATIONS (parsed via AST, not imported --
                   see _tool_declarations_via_ast for why)
- subagents     <- nova_agents.AgentType + nova_agents.get_all_agents()
- integrations  <- the actual config sites (env vars / MCP server list),
                   not a second hand-maintained list
- voice_loop    <- desk.live_session module docstrings/constants actually
                   read from the module, not restated from memory
- recent_activity <- `git log`, fixed 14-day window

A generator that cannot find its source (import error, git not
available, etc.) returns a clearly-marked "unavailable" placeholder
rather than raising -- a failing generator must never fail a commit.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def _unavailable(reason: str) -> str:
    return f"_unavailable, regenerate manually: {reason}_"


# ── capabilities ────────────────────────────────────────────────────────────

def _tool_declarations_via_ast(nova_py_path: Path) -> list[dict]:
    """Parse TOOL_DECLARATIONS out of nova.py without importing it.

    Importing nova.py runs its entire startup-adjacent module body
    (heavy optional imports, path resolution, logging setup) just to
    read a list literal. AST parsing reads the same literal the runtime
    actually assigns to TOOL_DECLARATIONS at that line, with none of that
    cost and no side effects -- safe to run from a pre-commit hook on
    every commit.
    """
    tree = ast.parse(nova_py_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "TOOL_DECLARATIONS"
                       for t in node.targets)):
            continue
        if not isinstance(node.value, ast.List):
            continue
        tools = []
        for elt in node.value.elts:
            if not isinstance(elt, ast.Dict):
                continue
            entry: dict[str, Any] = {}
            for k, v in zip(elt.keys, elt.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    entry[k.value] = v.value
            if "name" in entry:
                tools.append(entry)
        return tools
    return []


def generate_capabilities(nova_py_path: Path | None = None) -> str:
    path = nova_py_path or (REPO_ROOT / "nova.py")
    try:
        tools = _tool_declarations_via_ast(path)
    except Exception as e:
        return _unavailable(f"could not parse TOOL_DECLARATIONS from {path}: {e}")
    if not tools:
        return _unavailable("TOOL_DECLARATIONS was not found or is empty")

    lines = ["| Tool | Description |", "| --- | --- |"]
    for t in tools:
        name = t.get("name", "?")
        desc = (t.get("description") or "").split(".")[0].strip()
        if len(desc) > 100:
            desc = desc[:97] + "..."
        lines.append(f"| `{name}` | {desc} |")
    lines.append("")
    lines.append(f"{len(tools)} tools declared in `nova.py`'s `TOOL_DECLARATIONS`, "
                 "plus whatever MCP servers extend it with at runtime "
                 "(`nova_state._mcp_bridge.gemini_declarations()`).")
    return "\n".join(lines)


# ── sub-agents ───────────────────────────────────────────────────────────────

def generate_subagents() -> str:
    try:
        import nova_agents
    except Exception as e:
        return _unavailable(f"could not import nova_agents: {e}")

    try:
        agents = nova_agents.get_all_agents()
    except Exception as e:
        return _unavailable(f"nova_agents.get_all_agents() raised: {e}")

    # agents_extra.py registers a second wave (orchestrator + specialists)
    # under the same AgentType enum but is not in nova_agents.get_all_agents().
    # Read it too, best-effort, so this block matches what init_agents()
    # actually wires up rather than only the base module's subset.
    #
    # Read via AST, not `import agents_extra`: that module does `import nova
    # as _nova` at its own top level, and nova.py does `from agents_extra
    # import agent_process` at ITS top level -- importing agents_extra
    # standalone, before nova.py's own import has already put it in
    # sys.modules, fails with "cannot import name 'agent_process' from
    # partially initialized module 'agents_extra' (most likely due to a
    # circular import)". A generator that only needs class names has no
    # reason to run either module's import-time side effects at all.
    extra_names: dict[str, str] = {}
    try:
        tree = ast.parse((REPO_ROOT / "agents_extra.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(b, ast.Name) and b.id == "BaseAgent" for b in node.bases
            ):
                extra_names[node.name] = ""
    except Exception:
        pass

    if not agents and not extra_names:
        return _unavailable("no sub-agents found via nova_agents.get_all_agents()")

    lines = ["| Agent | Type |", "| --- | --- |"]
    for agent_type, agent in sorted(agents.items(), key=lambda kv: kv[0].value):
        lines.append(f"| {type(agent).__name__} | `{agent_type.value}` |")
    for class_name in sorted(extra_names):
        lines.append(f"| {class_name} | (agents_extra.py) |")
    return "\n".join(lines)


# ── integrations ─────────────────────────────────────────────────────────────

def generate_integrations() -> str:
    """What NOVA can actually reach, read from real config/env sites --
    not a hand-maintained list that drifts from what's actually wired."""
    import os

    # nova.py reads GEMINI_API_KEY via plain os.getenv(), but only after
    # its own load_dotenv() call has populated os.environ from .env --
    # checking os.getenv() here without doing the same made a real,
    # working key read back as "no API key set".
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env", override=False)
    except Exception:
        pass

    rows: list[tuple[str, str, str]] = []

    rows.append(("Gemini Live / Gemini API", "cloud LLM + realtime voice",
                "configured" if os.getenv("GEMINI_API_KEY") else "no API key set"))
    rows.append(("Ollama (local models)", "offline LLM fallback (Qwen, TinyLlama)",
                "see `nova_intelligence.local_model_manager`"))
    rows.append(("faster-whisper", "offline speech-to-text",
                "configured" if _module_importable("faster_whisper") else "not installed"))
    rows.append(("pyttsx3 / Piper", "offline text-to-speech",
                "configured" if _module_importable("pyttsx3") else "not installed"))
    rows.append(("libzim (offline Wikipedia)", "ZIM archive search",
                "configured" if _module_importable("libzim") else "not installed"))
    rows.append(("Gmail", "read-only mail awareness",
                "see `desk/creds.py` and `integrations/`"))

    try:
        tree = ast.parse((REPO_ROOT / "nova.py").read_text(encoding="utf-8"))
        mcp_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_MCPServerConfig":
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        mcp_names.append(str(kw.value.value))
        for name in mcp_names:
            rows.append((f"MCP: {name}", "Model Context Protocol server", "configured in nova.py"))
    except Exception:
        pass

    lines = ["| Integration | Purpose | Status |", "| --- | --- | --- |"]
    for name, purpose, status in rows:
        lines.append(f"| {name} | {purpose} | {status} |")
    return "\n".join(lines)


def _module_importable(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# ── voice / streaming loop ──────────────────────────────────────────────────

def generate_voice_loop() -> str:
    lines = [
        "Two independent voice paths, chosen at startup and switchable "
        "mid-session on network loss:",
        "",
        "- **Cloud** (`desk/live_session.py`, class `LiveManager`): Gemini "
        "Live over a realtime WebSocket. Owns the microphone via a "
        "PortAudio callback stream, plays audio through a buffered output "
        "stream (`OUTPUT_LATENCY_S`), and reconnects with backoff on "
        "failure rather than giving up after one.",
        "- **Offline** (`offline_extra.py`, function `run_offline_loop_v2`): "
        "faster-whisper for STT, the intelligence router "
        "(`nova_intelligence.router`) for the reply -- Ollama-served Qwen, "
        "falling back to TinyLlama -- and pyttsx3/Piper for TTS, played "
        "through `_play_pcm_with_barge_in` with real voice-interrupt "
        "support via `nova_voice.VoiceGate`.",
        "",
        "Barge-in (`nova_voice.VoiceGate`/`EchoCanceller`) is shared by "
        "both paths. Full-duplex (real voice interruption, not just a "
        "button) is forced on for offline; for the cloud path it is gated "
        "behind `NOVA_VOICE_FULL_DUPLEX=1` and off by default -- see "
        "`nova_voice.simple_voice_default()`.",
    ]
    return "\n".join(lines)


# ── recent activity ──────────────────────────────────────────────────────────

def generate_recent_activity(days: int = 14) -> str:
    try:
        result = subprocess.run(
            ["git", "log", f"--since={days}.days", "--pretty=format:%h %ad %s",
            "--date=short"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return _unavailable(f"git log failed: {e}")
    if result.returncode != 0:
        return _unavailable(f"git log exited {result.returncode}: {result.stderr[:200]}")
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        return f"_No commits in the last {days} days._"
    out = [f"Commits in the last {days} days ({len(lines)}):", ""]
    for ln in lines[:40]:
        out.append(f"- {ln}")
    if len(lines) > 40:
        out.append(f"- ...and {len(lines) - 40} more")
    return "\n".join(out)


# ── registry the renderer walks ─────────────────────────────────────────────

GENERATORS = {
    "capabilities": generate_capabilities,
    "subagents": generate_subagents,
    "integrations": generate_integrations,
    "voice_loop": generate_voice_loop,
    "recent_activity": generate_recent_activity,
}
