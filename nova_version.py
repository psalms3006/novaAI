"""The one place NOVA's version and its backend address are defined.

Everything that reports or compares the version reads it from here: the
window, the account client's X-NOVA-Client header, telemetry, the updater and
the installer build. There used to be two constants that disagreed -- "1.0.0"
in the window and "0.1.0" sent to the backend.

DEFAULT_CLOUD_URL is compiled into every build. Empty means "no NOVA account
server": NOVA then runs locally with no sign-in, which is how development
works. A production build sets it to the deployed backend
(docs/NOVA_OWNER_SETUP_GUIDE.md), and sign-in becomes required. NOVA_CLOUD_URL
in the environment overrides it for development and testing.
"""
from __future__ import annotations

import os

APP_VERSION = "1.0.0"

DEFAULT_CLOUD_URL = ""


def cloud_base_url() -> str:
    return (os.getenv("NOVA_CLOUD_URL", "").strip() or DEFAULT_CLOUD_URL).rstrip("/")


__all__ = ["APP_VERSION", "DEFAULT_CLOUD_URL", "cloud_base_url"]
