"""
actions/_audio_win.py
══════════════════════
Windows system-volume control via pycaw (COM-based, no external executable).

Replaces the previous nircmd.exe dependency in computer_settings.py — nircmd
is a third-party NirSoft utility that was never bundled, never installed, and
never documented anywhere in this project. subprocess.run(..., check=False)
silently swallowed the resulting "file not found" failure, so volume_up/down
returned a success message despite doing nothing on any machine that didn't
happen to have nircmd.exe on PATH.

Requires: pip install pycaw comtypes  (Windows only — this module is only
imported from computer_settings.py inside an `if system == "Windows":` guard,
so it's never touched on Linux/Mac).
"""

from __future__ import annotations

from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume


def _get_volume_interface() -> IAudioEndpointVolume:
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def step_volume(delta: float) -> float:
    """delta is a fraction, e.g. +0.05 = +5%. Returns the new level (0.0–1.0)."""
    vol = _get_volume_interface()
    current = vol.GetMasterVolumeLevelScalar()
    new_level = max(0.0, min(1.0, current + delta))
    vol.SetMasterVolumeLevelScalar(new_level, None)
    return new_level


def set_mute(muted: bool | None = None) -> bool:
    """Explicit mute/unmute, or toggle if muted is None. Returns resulting state."""
    vol = _get_volume_interface()
    if muted is None:
        muted = not bool(vol.GetMute())
    vol.SetMute(1 if muted else 0, None)
    return muted


def get_volume() -> float:
    return _get_volume_interface().GetMasterVolumeLevelScalar()
