import subprocess
import sys

def execute(parameters):
    app_name = parameters.get("app_name", "").strip()
    if not app_name:
        return "No app name provided."

    app_map = {
        "settings": "start ms-settings:",
        "chrome": "start chrome",
        "notepad": "notepad",
        "calculator": "calc",
        "file explorer": "explorer",
        "explorer": "explorer",
        "vscode": "code",
        "visual studio code": "code",
        "spotify": "start spotify",
        "whatsapp": "start whatsapp",
        "edge": "start msedge",
        "microsoft edge": "start msedge",
        "terminal": "wt",
        "windows terminal": "wt",
        "task manager": "taskmgr",
        "control panel": "control",
        "word": "start winword",
        "excel": "start excel",
        "powerpoint": "start powerpnt",
        "paint": "mspaint",
        "photos": "start ms-photos:",
        "snipping tool": "start ms-screenclip:",
        "camera": "start microsoft.windows.camera:",
        "maps": "start bingmaps:",
        "mail": "start outlookmail:",
        "clock": "start ms-clock:",
    }

    lookup = app_name.lower().strip()
    if lookup in app_map:
        cmd = app_map[lookup]
    else:
        cmd = f'start "" "{app_name}"'

    try:
        kwargs = {"shell": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        stdout, stderr = proc.communicate(timeout=10)
        if proc.returncode != 0 and stderr:
            err = stderr.decode("utf-8", errors="replace").strip()
            if err:
                return f"Error opening {app_name}: {err}"
        return f"Opened {app_name}."
    except subprocess.TimeoutExpired:
        proc.kill()
        return f"Opened {app_name} (process timed out but was launched)."
    except Exception as e:
        return f"Error opening {app_name}: {e}"