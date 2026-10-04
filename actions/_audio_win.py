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

import gc
from contextlib import contextmanager
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume


@contextmanager
def _volume_interface():
    """The endpoint volume interface, released before we hand control back.

    GetSpeakers() used to return the COM device directly. In current pycaw
    (20251023) it returns an AudioDevice wrapper with no .Activate, and the
    real device is on ._dev — so the old call raised "'AudioDevice' object has
    no attribute 'Activate'" and every volume request came back as "Volume
    control failed... install pycaw", on a machine where pycaw was installed
    and working perfectly.

    The interface must not outlive this block. These pointers used to be
    returned to the caller and dropped on the floor, so comtypes ran
    IUnknown::Release from the finaliser at whatever later allocation
    triggered a collection — an ordinary `import shutil` was enough — and if
    the apartment had gone by then, the vtable dereference took the whole
    interpreter down with an access violation. No exception, no traceback,
    nothing in NOVA's log. Collecting here runs those finalisers while COM is
    still up, which is the difference between a freed pointer and a crash.
    """
    speakers = AudioUtilities.GetSpeakers()
    device = getattr(speakers, "_dev", speakers)
    interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    try:
        yield volume
    finally:
        del volume, interface, device, speakers
        gc.collect()


def get_volume() -> float:
    """Current master volume, 0.0-1.0."""
    with _volume_interface() as vol:
        return float(vol.GetMasterVolumeLevelScalar())


def set_volume(level: float) -> float:
    """Set master volume to an absolute 0.0-1.0. Returns what it became."""
    level = max(0.0, min(1.0, float(level)))
    with _volume_interface() as vol:
        vol.SetMasterVolumeLevelScalar(level, None)
        return float(vol.GetMasterVolumeLevelScalar())


def step_volume(delta: float) -> float:
    """delta is a fraction, e.g. +0.05 = +5%. Returns the new level (0.0–1.0)."""
    with _volume_interface() as vol:
        current = vol.GetMasterVolumeLevelScalar()
        new_level = max(0.0, min(1.0, current + delta))
        vol.SetMasterVolumeLevelScalar(new_level, None)
        return new_level


def set_mute(muted: bool | None = None) -> bool:
    """Explicit mute/unmute, or toggle if muted is None. Returns resulting state."""
    with _volume_interface() as vol:
        if muted is None:
            muted = not bool(vol.GetMute())
        vol.SetMute(1 if muted else 0, None)
        return muted
