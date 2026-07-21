import subprocess

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
    }

    lookup = app_name.lower()
    if lookup in app_map:
        cmd = app_map[lookup]
    else:
        cmd = f'start "" "{app_name}"'

    try:
        # NON-BLOCKING: Popen doesn't wait for the app to close
        subprocess.Popen(cmd, shell=True)
        return f"Opened {app_name}."
    except Exception as e:
        return f"Error opening {app_name}: {e}"