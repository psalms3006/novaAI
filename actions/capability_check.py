"""actions.capability_check — what can NOVA do right now, and why not?

Before this, "why can't you do that?" was answered from the model's
imagination. The facts existed but were scattered: connectivity in
nova._connectivity, the credential mode in desk.creds, tool availability in
nova._TOOL_AVAILABILITY, permission choices in the desk settings, the local
model in the router. Nothing put them in one place the model could read.

This reports the live state, read-only, and -- given a task -- sorts what it
needs into the three answers the person deserves:

  can't      not possible on this machine right now (no network, no GPU)
  shouldn't  possible, but a permission is off or the action needs a yes
  not yet    possible once something is installed, connected or granted

Nothing here is inferred from tool *names*; every line comes from a probe.
"""
from __future__ import annotations

import os
import re
import shutil
import sys

#: Tools that cannot work without the internet. Kept explicit (not derived
#: from nova_intelligence.provider.TOOL_NETWORK_REQUIREMENTS, whose names
#: predate the current tools and mark web_search as optional).
NEEDS_NETWORK = {
    "web_search": "searching the web",
    "fetch_url": "reading web pages and links",
    "research_report": "researching and writing a sourced report",
    "email_search": "searching your email",
    "send_message": "sending messages",
}

#: Words in a request that imply a capability, mapped to what it needs.
_TASK_NEEDS = [
    (r"\b(search|google|look up|latest|news|website|web|online|link|url|http)\b", "network"),
    (r"\b(research|sources?|report)\b", "network"),
    (r"\b(email|gmail|inbox|mail)\b", "email"),
    (r"\b(train|fine[- ]?tune|lora|finetune|training)\b", "gpu"),
    (r"\b(gpu|cuda|vram)\b", "gpu"),
    (r"\b(screen|screenshot|what'?s on my screen|look at this)\b", "screen"),
    (r"\b(open|launch|click|type|close|volume|brightness|shutdown|restart)\b", "computer_control"),
    (r"\b(delete|move|rename|write|save|create (a )?file|edit)\b", "file_write"),
    (r"\b(file|folder|document|read)\b", "file_read"),
    (r"\b(run|execute|install|pip|npm|script|terminal|shell|command)\b", "exec"),
    (r"\b(voice|talk|speak|listen|microphone|mic)\b", "voice"),
]


def _nova():
    return sys.modules.get("nova")


def _connectivity() -> str:
    n = _nova()
    conn = getattr(n, "_connectivity", None) if n else None
    if conn is None:
        return "unknown"
    try:
        st = conn.state
        st = st() if callable(st) else st
        return str(getattr(st, "value", st))
    except Exception:
        return "unknown"


def _cloud() -> dict:
    try:
        from desk import creds
        st = dict(getattr(creds, "_last_status", {}) or {})
        return {"mode": st.get("mode", "unknown"), "cloud_error": st.get("cloud_error", "")}
    except Exception:
        return {"mode": "unknown", "cloud_error": ""}


def _models() -> list[str]:
    n = _nova()
    router = getattr(n, "_nova_router", None) if n else None
    out = []
    if router is None:
        return out
    try:
        for name in ("gemini", "ollama", "ollama-fallback"):
            p = router.get_provider(name)
            if p is None:
                continue
            model = getattr(p, "model", "") or ""
            healthy = True
            try:
                h = p.health
                h = h() if callable(h) else h
                healthy = getattr(h, "consecutive_fails", 0) < 3
            except Exception:
                pass
            tools = "no tools" if str(model).startswith("tinyllama") else "tools"
            out.append(f"{name}: {model or '?'} ({tools}{'' if healthy else ', failing recently'})")
    except Exception:
        pass
    return out


def _tools() -> tuple[list[str], list[str]]:
    n = _nova()
    avail = {}
    if n is not None:
        try:
            n._tool_available("open_app")          # fills the map if empty
            avail = dict(getattr(n, "_TOOL_AVAILABILITY", {}) or {})
        except Exception:
            avail = {}
    have = sorted(t for t, ok in avail.items() if ok)
    missing = sorted(t for t, ok in avail.items() if not ok)
    return have, missing


def _permissions() -> dict:
    try:
        from desk.settings import get as _get
        return dict(_get("permissions", {}) or {})
    except Exception:
        return {}


def hardware() -> dict:
    """RAM, disk, CPU and GPU, from the machine itself."""
    hw: dict = {"cpus": os.cpu_count() or 0}
    try:
        import psutil
        vm = psutil.virtual_memory()
        hw["ram_gb"] = round(vm.total / 2**30, 1)
        hw["ram_free_gb"] = round(vm.available / 2**30, 1)
    except Exception:
        pass
    try:
        du = shutil.disk_usage(os.path.expanduser("~"))
        hw["disk_free_gb"] = round(du.free / 2**30, 1)
    except Exception:
        pass
    hw["nvidia_gpu"] = shutil.which("nvidia-smi") is not None
    hw["gpus"] = _gpu_names()
    return hw


def _gpu_names() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def assess(task: str, state: dict) -> list[str]:
    """Sort what `task` needs into can / can't / shouldn't / not yet."""
    text = (task or "").lower()
    needs = []
    for pattern, need in _TASK_NEEDS:
        if re.search(pattern, text) and need not in needs:
            needs.append(need)
    online = state["connectivity"] != "offline"
    perms = state["permissions"]
    lines = []
    for need in needs:
        if need == "network":
            lines.append("network: available" if online else
                         "network: CAN'T right now -- this computer is offline. "
                         "Local files, memory, documents and installed skills still work.")
        elif need == "email":
            if not online:
                lines.append("email: CAN'T right now -- offline.")
            elif "email_search" in state["missing_tools"]:
                lines.append("email: NOT YET -- connect Gmail in Settings first.")
            else:
                lines.append("email: available (read-only search)")
        elif need == "gpu":
            hw = state["hardware"]
            if hw.get("nvidia_gpu"):
                lines.append("gpu: an NVIDIA GPU is present; check VRAM before training.")
            else:
                gpus = ", ".join(hw.get("gpus") or []) or "none found"
                lines.append(
                    "gpu: CAN'T train on a GPU here -- no CUDA-capable GPU "
                    f"(graphics: {gpus}; RAM {hw.get('ram_gb', '?')} GB). Small-model "
                    "inference, dataset preparation and CPU-only jobs are still possible.")
        elif need in ("screen", "computer_control", "file_read", "file_write", "exec"):
            scope = "screen_read" if need == "screen" else need
            setting = perms.get(scope, "ask")
            if setting == "deny":
                lines.append(f"{scope}: SHOULDN'T -- turned off in Settings > Permissions.")
            elif setting == "ask":
                lines.append(f"{scope}: allowed with your yes each time.")
            else:
                lines.append(f"{scope}: allowed.")
        elif need == "voice":
            lines.append("voice: needs the network for the cloud voice"
                         + ("" if online else " -- offline now, so typed chat only"))
    if not needs:
        lines.append("Nothing in the request needs a capability beyond conversation.")
    return lines


def snapshot() -> dict:
    have, missing = _tools()
    return {
        "connectivity": _connectivity(),
        "cloud": _cloud(),
        "models": _models(),
        "tools": have,
        "missing_tools": missing,
        "permissions": _permissions(),
        "hardware": hardware(),
    }


def execute(args: dict) -> str:
    state = snapshot()
    online = state["connectivity"] != "offline"
    lines = [f"Network: {state['connectivity']}"
             + (f" (NOVA Cloud: {state['cloud']['cloud_error']})" if state["cloud"]["cloud_error"] else ""),
             f"Model access: {state['cloud']['mode']}"]
    if state["models"]:
        lines.append("Models: " + "; ".join(state["models"]))
    if not online:
        lines.append("Unavailable while offline: " + ", ".join(
            f"{t} ({why})" for t, why in NEEDS_NETWORK.items()))
    if state["missing_tools"]:
        lines.append("Not installed: " + ", ".join(state["missing_tools"]))
    denied = [s for s, v in state["permissions"].items() if v == "deny"]
    if denied:
        lines.append("Turned off in Settings: " + ", ".join(denied))
    hw = state["hardware"]
    lines.append(
        f"Hardware: {hw.get('cpus')} CPUs, RAM {hw.get('ram_gb', '?')} GB "
        f"({hw.get('ram_free_gb', '?')} free), disk {hw.get('disk_free_gb', '?')} GB free, "
        f"GPU: {', '.join(hw.get('gpus') or []) or 'unknown'}"
        f"{' (CUDA)' if hw.get('nvidia_gpu') else ' (no CUDA)'}")
    task = str((args or {}).get("task") or "").strip()
    if task:
        lines.append(f"For '{task[:120]}':")
        lines.extend("  - " + l for l in assess(task, state))
    return "\n".join(lines)
