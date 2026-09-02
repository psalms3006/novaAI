#!/usr/bin/env python3
"""
NOVA Desktop App — native window around the FULL NOVA assistant.

Boots the complete NOVA stack exactly like `python nova.py --ui` — Gemini
chat + tools + living memory + task manager + recollection — then shows the
whole UI in a native desktop window (pywebview + Windows WebView2) instead of
a browser tab, so it looks and feels like ChatGPT / Claude.

Run:  python nova_desktop.py
Headless check (no window): NOVA_DESKTOP_HEADLESS=1 python nova_desktop.py
"""

import os
import sys
import time
import threading
import webbrowser

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# nova.py decides its mode from argv at import time
if "--ui" not in sys.argv:
    sys.argv.append("--ui")

# never pop a browser tab — the desktop window IS the UI
webbrowser.open = lambda *a, **k: False

import nova  # noqa: E402

UI_URL = f"http://127.0.0.1:{nova.UI_PORT}"
SERVER_READY_TIMEOUT = 120


def _wait_for_server() -> bool:
    import requests
    deadline = time.time() + SERVER_READY_TIMEOUT
    while time.time() < deadline:
        try:
            if requests.get(UI_URL, timeout=2).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    boot = threading.Thread(target=nova.main, name="NOVABoot", daemon=True)
    boot.start()

    print(f"NOVA desktop: waiting for the assistant server on {UI_URL} ...")
    if not _wait_for_server():
        print("NOVA desktop: the server did not come up. Check the boot log above.")
        return 1
    print("NOVA desktop: assistant server ready.")

    if os.environ.get("NOVA_DESKTOP_HEADLESS") == "1":
        print("HEADLESS_READY")
        return 0

    import webview
    webview.create_window(
        "NOVA",
        UI_URL,
        width=1280,
        height=820,
        min_size=(900, 600),
    )
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
