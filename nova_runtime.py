"""nova_runtime — start the brain once, for one account; restart to switch.

The brain (nova.py) fixes its data paths when it is imported, so it may only
start after nova_lifecycle has bound this process to an account. On a build
with a NOVA account server that means: after sign-in. Starting it is
therefore a callable -- invoked at launch when a session already exists, or
by the sign-in endpoint -- and it runs at most once per process. Signing out
restarts the process instead of unloading one person's memory and tasks and
hoping nothing is left behind.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
from typing import Callable

_lock = threading.Lock()
_state = {"started": False, "ready": False, "error": "", "started_at": 0.0}
_log: Callable[[str], None] = lambda m: None


def set_logger(fn: Callable[[str], None]) -> None:
    global _log
    _log = fn


def brain_state() -> dict:
    return dict(_state)


def _run() -> None:
    try:
        _log("Brain: importing nova module...")
        import nova as nova_mod
        orig = getattr(nova_mod, "run_desk_server", None)
        if callable(orig):
            # nova.main() would start a second desk server; the desktop
            # already runs one.
            nova_mod.run_desk_server = lambda *a, **kw: None
        _log("Brain: calling nova.main()...")
        try:
            nova_mod.main()
        finally:
            if callable(orig):
                nova_mod.run_desk_server = orig
        _log(f"Brain: ready (router={type(getattr(nova_mod, '_nova_router', None)).__name__})")
        try:
            import desk.bridge as br
            br._brain_ready = True
        except Exception as e:
            _log(f"Brain: could not set bridge flag: {e}")
        _state["ready"] = True
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {e}"
        _log(f"Brain init FAILED: {e}\n{traceback.format_exc()}")


def start_brain() -> bool:
    """Start the brain in the background. False if it had already started."""
    with _lock:
        if _state["started"]:
            return False
        _state["started"] = True
        _state["started_at"] = time.time()
    threading.Thread(target=_run, name="NOVABrain", daemon=True).start()
    _log("Brain init thread started")
    return True


def relaunch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--after-restart"]
    return [sys.executable, os.path.abspath(sys.argv[0]), "--after-restart"]


def relaunch(delay_s: float = 0.6) -> None:
    """Start a fresh NOVA and end this one (after the HTTP reply has gone).

    The new process waits for this one's single-instance lock to be released
    (`--after-restart`), so the two never run side by side.
    """
    def _go():
        time.sleep(delay_s)
        try:
            flags = 0
            if os.name == "nt":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(relaunch_command(), close_fds=True, creationflags=flags)
            _log("Relaunch: new process started; exiting")
        except Exception as e:
            _log(f"Relaunch failed: {e}")
        finally:
            os._exit(0)
    threading.Thread(target=_go, name="NOVARelaunch", daemon=True).start()


__all__ = ["start_brain", "brain_state", "relaunch", "relaunch_command", "set_logger"]
