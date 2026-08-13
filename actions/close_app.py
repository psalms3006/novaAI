"""actions/close_app.py — terminates a running application by name.

Mirrors actions/open_app.py's app_map so 'open Chrome' / 'close Chrome' use
the same vocabulary. Uses taskkill on Windows (built-in, no external deps)
and pkill elsewhere.
"""

import platform
import subprocess

# Maps user-facing app names to their actual process/executable names.
# Kept in sync with open_app.py's app_map keys where the two overlap.
_PROCESS_MAP = {
    "chrome": "chrome.exe",
    "notepad": "notepad.exe",
    "calculator": "CalculatorApp.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "vscode": "Code.exe",
    "visual studio code": "Code.exe",
    "spotify": "Spotify.exe",
    "whatsapp": "WhatsApp.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "task manager": "Taskmgr.exe",
}


def execute(parameters: dict) -> str:
    app_name = parameters.get("app_name", "").strip()
    if not app_name:
        return "No app name provided."

    lookup = app_name.lower()
    process_name = _PROCESS_MAP.get(lookup, app_name if app_name.endswith(".exe") else f"{app_name}.exe")

    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["taskkill", "/IM", process_name, "/F"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                return f"Closed {app_name}."
            # taskkill exit code 128 = process not found — report accurately
            # instead of claiming success (the bug we just fixed in volume control).
            return f"Could not close {app_name}: {result.stderr.strip() or 'process not found'}"
        else:
            proc_stub = process_name.removesuffix(".exe")
            result = subprocess.run(["pkill", "-f", proc_stub], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                return f"Closed {app_name}."
            return f"Could not close {app_name}: process not found"
    except Exception as e:
        return f"Error closing {app_name}: {e}"
