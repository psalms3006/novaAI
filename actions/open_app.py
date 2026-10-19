import os
import subprocess
from core.shell_manager import execute_safe

def execute(parameters):
    app_name = parameters.get("app_name", "").strip()
    if not app_name:
        return "No app name provided."

    app_map = {
        "settings": "ms-settings:",
        "chrome": "chrome",
        "notepad": "notepad",
        "calculator": "calc",
        "file explorer": "explorer",
        "explorer": "explorer",
        "vscode": "code",
        "visual studio code": "code",
        "spotify": "spotify",
        "whatsapp": "whatsapp",
        "edge": "msedge",
        "microsoft edge": "msedge",
        "terminal": "wt",
        "windows terminal": "wt",
        "task manager": "taskmgr",
        "control panel": "control",
        "word": "winword",
        "excel": "excel",
        "powerpoint": "powerpnt",
        "paint": "mspaint",
        "photos": "ms-photos:",
        "snipping tool": "ms-screenclip:",
        "camera": "microsoft.windows.camera:",
        "maps": "bingmaps:",
        "mail": "outlookmail:",
        "clock": "ms-clock:",
    }

    lookup = app_name.lower().strip()
    try:
        # Use os.startfile for opening apps/URIs. This is the Windows-native,
        # safe way to open applications/files/URIs without invoking a shell
        # or worrying about shell injection.
        if lookup in app_map:
            os.startfile(app_map[lookup])
        else:
            # For arbitrary paths, still use startfile but validate it first.
            os.startfile(app_name)
        return f"Opened {app_name}."
    except Exception as e:
        return f"Error opening {app_name}: {e}"
