"""nova_runtime — start the brain once, for one account; restart to switch.

The brain (nova.py) fixes its data paths when it is imported, so it may only
start after nova_lifecycle has bound this process to an account. On a build
with a NOVA account server that means: after sign-in. Starting it is
therefore a callable -- invoked at launch when a session already exists, or
by the sign-in endpoint -- and it runs at most once per process.

The desk imports nova before anyone signs in, so "bound before import" is
not true of the first sign-in in a running process. `rebind_data_dir()`
closes that gap: it re-resolves the paths for the account just bound and
clears anything loaded under the previous folder, while the brain is still
stopped. Once the brain has started it refuses, and the caller restarts the
process instead of unloading one person's memory and tasks and hoping
nothing is left behind.
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


def assistant_allowed() -> tuple[bool, str]:
    """May this process run the assistant (brain, voice, chat) yet?

    The one gate that decides it, so every entry point asks the same
    question: launch, sign-in, onboarding-complete, voice. A person who has
    signed in but not finished verification and onboarding gets the setup
    screens and *nothing else* -- no listener, no tools, no background
    assistant -- no matter which UI route they try.

    Deliberately cheap (no Flask, no network): reads the cached session.
    The gate fails closed: when the account system says nothing usable,
    only the genuinely unconfigured (local-only) build is allowed through.
    """
    try:
        import nova_account
        acct = nova_account.account()
    except Exception:
        return False, "account_error"
    if not acct.configured:
        return True, ""                       # local-only build, no gate
    if os.environ.get("NOVA_AUTH_GATE") != "1":
        # The launcher only arms the gate on a build with an account server
        # and nobody signed in. A local-only run (or one where sign-in is
        # not being enforced) must keep working exactly as before.
        return True, ""
    try:
        if not acct.signed_in:
            return False, "signin"
        st = acct.status()
        if st.get("verification_required") and not st.get("email_verified"):
            return False, "verify_email"
        inst = acct.cached_instance()
        # Only block on positive knowledge: an empty cache must not lock an
        # existing user out of their own assistant.
        if inst and not inst.get("onboarding_completed"):
            return False, "onboarding"
    except Exception:
        return False, "account_error"
    return True, ""


def silence_voice() -> bool:
    """Stop the live voice session, if one is running.

    Used wherever the setup gate closes: a session already talking when the
    person signs out (or a half-onboarded account is bound) must go quiet
    immediately, not finish its sentence behind the setup screens. Returns
    whether a session was actually stopped, for logging and tests.
    """
    try:
        from desk import live_session as live
        mgr = live.get_live_manager()
        state = str(mgr.status().get("state") or "")
        mgr.stop()
        return state not in ("", "idle", "closed", "not running")
    except Exception:
        return False


def enforce_setup_gate_silence() -> str:
    """Close the microphone and cancel speech when setup is unfinished.

    Returns the blocking stage, or "" when the assistant is allowed. Every
    transition into sign-in, email verification or onboarding calls this, so
    the answer does not depend on which UI route the person took.
    """
    allowed, stage = assistant_allowed()
    if not allowed:
        silence_voice()
    return "" if allowed else stage


def rebind_data_dir() -> bool:
    """Point an already-loaded nova at the account active right now.

    Returns True when the process may continue under the new folder, False
    when it may not -- the brain has already started, so its memory and
    tasks are live and the caller must restart instead of swapping state
    under a running assistant.

    Why this exists: the desk starts its Flask server before anyone signs
    in, and nova fixes its data paths when it is imported. Binding a
    first account in that process used to leave the brain reading the
    machine-wide folder -- the previous person's living memory -- so a
    brand-new account was greeted by the old user's name. This runs after
    activate_account()/deactivate() and before start_brain(), when
    nothing holds the old path.
    """
    import sys
    from pathlib import Path
    nova = sys.modules.get("nova")
    if nova is None:
        # Not imported yet: it resolves from the env when it is, which is
        # exactly what we want.
        return True
    try:
        target = nova.data_dir_for_active_account()
    except Exception as e:
        _log(f"Data-dir rebind failed: {e}")
        return False
    try:
        same = Path(target).resolve() == Path(nova._DATA_DIR).resolve()
    except Exception:
        same = str(target) == str(nova._DATA_DIR)
    if same:
        # Same person, same folder: a re-bind after onboarding, not a swap.
        return True
    if _state.get("started"):
        # A live brain holds memory and tasks in memory; only a restart is
        # safe now.
        return False
    try:
        path = nova.refresh_data_paths()
    except Exception as e:
        _log(f"Data-dir rebind failed: {e}")
        return False

    # Modules that copied nova's paths at import time. Repointed in place --
    # a reload would re-run their import side effects.
    mem = sys.modules.get("memory_extra")
    if mem is not None:
        mem.MEMORY_META_FILE = nova.MEMORY_META_FILE
        mem.MEMORY_TEXTS_FILE = nova.MEMORY_TEXTS_FILE
        mem.MEMORY_INDEX_FILE = nova.MEMORY_INDEX_FILE
        mem.MEMORY_PENDING_FILE = nova.MEMORY_META_FILE.with_name(
            "memory_pending.jsonl")
        mem._faiss_index = None
        mem._index_encoder = None
    planner = sys.modules.get("planner_extra")
    if planner is not None:
        planner.PLANNER_FILE = nova.PLANNER_FILE
    vision = sys.modules.get("vision_extra")
    if vision is not None:
        vision.REFERENCE_FACE_PATH = nova.REFERENCE_FACE_PATH
    safety = sys.modules.get("nova_safety")
    if safety is not None:
        safety.AUDIT_FILE = path / "nova_audit.log"
        safety.COST_FILE = path / "nova_cost.json"

    # Nothing loaded from the previous folder travels with this process.
    try:
        import nova_state
        nova_state._memory_texts = []
        nova_state._living_memory = None
        nova_state._planner = None
    except Exception:
        pass
    _log(f"Data paths rebound to {path}")
    return True


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


__all__ = ["start_brain", "brain_state", "relaunch", "relaunch_command",
           "rebind_data_dir", "set_logger"]
