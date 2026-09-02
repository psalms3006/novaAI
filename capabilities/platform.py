"""
Platform detection for capability adapters.
"""
from __future__ import annotations

import platform
import sys
from typing import Optional

# Canonical platform identifiers used throughout the capability layer.
WINDOWS = "windows"
LINUX = "linux"
MACOS = "macos"
ANDROID = "android"
IOS = "ios"
DESKTOP = "desktop"


def host_platform() -> str:
    """Detect the current host platform.

    Returns one of the canonical identifiers: windows, linux, macos,
    android, ios, or the raw sys.platform value when unmapped.
    """
    sys_platform = sys.platform.lower()
    if sys_platform in ("win32", "cygwin", "msys"):
        return WINDOWS
    if sys_platform.startswith("linux"):
        # Android runs on a Linux kernel — detect via build prop presence.
        try:
            return ANDROID if platform.system() == "Linux" and _is_android() else LINUX
        except Exception:
            return LINUX
    if sys_platform == "darwin":
        return IOS if _is_ios() else MACOS
    return sys_platform


def _is_android() -> bool:
    try:
        # android context is typically injected via os.environ on device
        return bool(os_environ_get("ANDROID_DATA") or os_environ_get("ANDROID_ROOT"))
    except Exception:
        return False


def _is_ios() -> bool:
    # iOS Python (e.g. Pythonista) sets this; safest heuristic available cross-platform.
    return bool(os_environ_get("PYTHONISTA_MODULE_PATH"))


def os_environ_get(key: str) -> Optional[str]:
    import os

    return os.environ.get(key)


def is_desktop(platform_name: Optional[str] = None) -> bool:
    return (platform_name or host_platform()) in (WINDOWS, LINUX, MACOS)


def is_mobile(platform_name: Optional[str] = None) -> bool:
    return (platform_name or host_platform()) in (ANDROID, IOS)