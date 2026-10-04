"""desk.startup — start NOVA with Windows, or not.

Three choices, stored as the `startup_mode` setting and applied to
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (per user; no admin):

    manual      nothing registered -- the person launches NOVA themselves
    open        NOVA starts with Windows and opens its window
    background  NOVA starts with Windows as the ambient orb only

The registry value is re-applied on every launch, so after an update moves
the executable the entry still points at the program that is actually there.
`launch_on_startup` / `start_minimized` used to be stored and read by nothing.
"""
from __future__ import annotations

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "NOVA"
MODES = ("manual", "open", "background")


def launch_command(mode: str) -> str:
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                              "nova_desktop_app.py"))
        cmd = f'"{sys.executable}" "{script}"'
    cmd += " --autostart"
    if mode == "background":
        cmd += " --background"
    return cmd


def _winreg():
    if os.name != "nt":
        return None
    import winreg
    return winreg


def registered_command() -> str:
    wr = _winreg()
    if wr is None:
        return ""
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY) as k:
            return str(wr.QueryValueEx(k, VALUE)[0])
    except OSError:
        return ""


def apply(mode: str) -> dict:
    """Make Windows match `mode`. Returns what is now registered."""
    if mode not in MODES:
        raise ValueError(f"startup mode must be one of {MODES}")
    wr = _winreg()
    if wr is None:
        return {"mode": mode, "registered": "", "supported": False}
    with wr.CreateKey(wr.HKEY_CURRENT_USER, RUN_KEY) as k:
        if mode == "manual":
            try:
                wr.DeleteValue(k, VALUE)
            except OSError:
                pass
        else:
            wr.SetValueEx(k, VALUE, 0, wr.REG_SZ, launch_command(mode))
    return {"mode": mode, "registered": registered_command(), "supported": True}


def set_mode(mode: str) -> dict:
    from . import settings
    result = apply(mode)
    settings.set_many({"startup_mode": mode,
                       "launch_on_startup": mode != "manual",
                       "start_minimized": mode == "background"})
    return result


def reapply_saved() -> None:
    """At launch: keep the registry pointing at this copy of NOVA."""
    from . import settings
    mode = settings.get("startup_mode", "")
    if mode in ("open", "background"):
        try:
            apply(mode)
        except Exception:
            pass


__all__ = ["MODES", "apply", "set_mode", "reapply_saved", "registered_command",
           "launch_command"]
