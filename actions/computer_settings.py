import shutil
import os
import platform
import subprocess
import time
import logging

log = logging.getLogger(__name__)


#: Windows Settings pages by what people call them. Opening the page directly
#: replaces a dozen blind clicks in Settings' sidebar (2026-09-30: fifteen
#: app_control calls in ninety seconds, three failures, and a silent NOVA).
SETTINGS_PAGES = {
    "accounts": "yourinfo", "your info": "yourinfo", "account": "yourinfo",
    "other users": "otherusers", "family": "family-group", "email accounts": "emailandaccounts",
    "sign-in options": "signinoptions", "sign in options": "signinoptions", "password": "signinoptions",
    "wifi": "network-wifi", "wi-fi": "network-wifi", "network": "network", "internet": "network",
    "vpn": "network-vpn", "proxy": "network-proxy", "airplane mode": "network-airplanemode",
    "mobile hotspot": "network-mobilehotspot", "bluetooth": "bluetooth", "devices": "devices",
    "printers": "printers", "mouse": "mousetouchpad", "touchpad": "devices-touchpad",
    "keyboard": "keyboard", "display": "display", "night light": "nightlight", "sound": "sound",
    "notifications": "notifications", "focus": "quiethours", "do not disturb": "quiethours",
    "power": "powersleep", "sleep": "powersleep", "battery": "batterysaver", "storage": "storagesense",
    "apps": "appsfeatures", "installed apps": "appsfeatures", "uninstall": "appsfeatures",
    "default apps": "defaultapps", "startup apps": "startupapps", "startup": "startupapps",
    "personalization": "personalization", "background": "personalization-background",
    "wallpaper": "personalization-background", "colors": "colors", "dark mode": "colors",
    "themes": "themes", "lock screen": "lockscreen", "taskbar": "taskbar", "start menu": "personalization-start",
    "privacy": "privacy", "microphone privacy": "privacy-microphone", "microphone": "privacy-microphone",
    "camera": "privacy-webcam", "location": "privacy-location", "windows update": "windowsupdate",
    "update": "windowsupdate", "date and time": "dateandtime", "time": "dateandtime",
    "language": "regionlanguage", "region": "regionlanguage", "clipboard": "clipboard",
    "multitasking": "multitasking", "about": "about", "activation": "activation",
    "recovery": "recovery", "backup": "backup", "remote desktop": "remotedesktop",
    "for developers": "developers", "gaming": "gaming-gamebar", "accessibility": "easeofaccess",
}


def _open_settings(page: str) -> str:
    key = page.strip().lower()
    uri = SETTINGS_PAGES.get(key)
    if uri is None:
        uri = next((v for k, v in SETTINGS_PAGES.items() if key and (key in k or k in key)), None)
    if uri is None:
        return ("I don't know a Settings page called " + repr(page) + ". Pages I can open: "
                + ", ".join(sorted(SETTINGS_PAGES)) + ".")
    if platform.system() != "Windows":
        return "Opening Windows Settings pages only works on Windows."
    os.startfile("ms-settings:" + uri)
    return f"Opened Settings at {page.strip() or uri} (ms-settings:{uri})."


def _winget(*argv, timeout=120) -> tuple:
    exe = shutil.which("winget")
    if not exe:
        raise FileNotFoundError("winget is not available on this computer")
    r = subprocess.run([exe, *argv, "--accept-source-agreements", "--disable-interactivity"],
                       capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _list_apps(name: str) -> str:
    """Installed programs whose name contains `name` (all of them, when empty)."""
    try:
        code, out = _winget("list", *(["--name", name] if name else []))
    except FileNotFoundError:
        os.startfile("ms-settings:appsfeatures")
        return "winget isn't available, so I opened Settings > Installed apps instead."
    rows = [l.rstrip() for l in out.splitlines() if l.strip() and not set(l.strip()) <= set("-\|/ ")]
    rows = [l for l in rows if "Name" not in l[:6] or "Id" not in l]
    if not rows or "No installed package found" in out:
        return f"No installed program matching {name!r}."
    return "Installed programs" + (f" matching {name!r}" if name else "") + ":\n" + "\n".join(rows[:40])


def _uninstall_app(name: str) -> str:
    """Uninstall one program by name. Always confirmed first (desk.confirm.IRREVERSIBLE)."""
    if not name.strip():
        return "Tell me which program to uninstall."
    try:
        code, out = _winget("list", "--name", name)
    except FileNotFoundError:
        os.startfile("ms-settings:appsfeatures")
        return ("winget isn't available, so I can't uninstall it myself. I opened Settings > "
                "Installed apps so you can remove it there.")
    if "No installed package found" in out:
        return f"No installed program matches {name!r}, so there is nothing to uninstall."
    try:
        code, out = _winget("uninstall", "--name", name, "--silent", timeout=600)
    except subprocess.TimeoutExpired:
        return f"Uninstalling {name} is taking longer than 10 minutes; it may still be finishing."
    tail = " ".join(out.split())[-300:]
    if code == 0 or "Successfully uninstalled" in out:
        return f"Uninstalled {name}."
    return (f"I couldn't uninstall {name} (winget exit {code}): {tail} "
            f"It may need you to confirm a Windows prompt, or to be removed from Settings > Installed apps.")


def execute(args: dict) -> str:
    action = args.get("action", "")
    value = args.get("value", "")
    system = platform.system()

    try:
        if action == "screenshot":
            # Requires PIL
            try:
                from PIL import ImageGrab
                timestamp = int(time.time())
                filename = f"screenshot_{timestamp}.png"
                ImageGrab.grab().save(filename)
                return f"Screenshot saved as {filename}"
            except ImportError:
                return "Screenshot requires PIL: pip install pillow"

        elif action in ("battery", "get_battery"):
            # Reading the machine, which NOVA could not do at all: the tool
            # answered "Unknown action: battery".
            try:
                import psutil
                b = psutil.sensors_battery()
                if b is None:
                    return "No battery detected — this looks like a desktop."
                state = "charging" if b.power_plugged else "on battery"
                left = ""
                if not b.power_plugged and b.secsleft and b.secsleft > 0:
                    left = f", about {b.secsleft // 3600}h {(b.secsleft % 3600) // 60}m left"
                return f"Battery is at {b.percent:.0f}%, {state}{left}."
            except Exception as e:
                return f"Could not read the battery: {e}"

        elif action in ("get_volume", "volume"):
            if system == "Windows":
                try:
                    from actions._audio_win import get_volume
                    return f"Volume is at {get_volume() * 100:.0f}%."
                except Exception as e:
                    return f"Could not read the volume: {e}"
            return "Reading the volume is not supported on this OS."

        elif action == "set_volume":
            if system == "Windows":
                try:
                    from actions._audio_win import set_volume
                    raw = args.get("value", args.get("level"))
                    level = float(raw)
                    if level > 1:
                        level /= 100.0
                    got = set_volume(level)
                    return f"Volume set to {got * 100:.0f}%."
                except (TypeError, ValueError):
                    return "Tell me a level, for example 'set volume to 40%'."
                except Exception as e:
                    return f"Could not set the volume: {e}"
            return "Setting the volume is not supported on this OS."

        elif action in ("system_info", "info"):
            try:
                # platform is imported at module scope; importing it again
                # here would make it a local and shadow the earlier use.
                import psutil
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage(os.path.expanduser("~"))
                return (
                    f"{platform.system()} {platform.release()}. "
                    f"CPU {psutil.cpu_percent(interval=0.3):.0f}%. "
                    f"Memory {mem.percent:.0f}% of "
                    f"{mem.total / 1024**3:.1f} GB. "
                    f"Disk {disk.percent:.0f}% of {disk.total / 1024**3:.0f} GB."
                )
            except Exception as e:
                return f"Could not read system info: {e}"

        elif action == "volume_up":
            if system == "Windows":
                try:
                    from actions._audio_win import step_volume
                    step_volume(+0.05)
                    return "Volume increased."
                except Exception as e:
                    return f"Volume control failed: {e}. Install pycaw: pip install pycaw comtypes"
            elif system == "Linux":
                subprocess.run(["amixer", "set", "Master", "5%+"], check=False)
                return "Volume increased."
            return "Volume control not available on this OS."

        elif action == "volume_down":
            if system == "Windows":
                try:
                    from actions._audio_win import step_volume
                    step_volume(-0.05)
                    return "Volume decreased."
                except Exception as e:
                    return f"Volume control failed: {e}. Install pycaw: pip install pycaw comtypes"
            elif system == "Linux":
                subprocess.run(["amixer", "set", "Master", "5%-"], check=False)
                return "Volume decreased."
            return "Volume control not available on this OS."

        elif action == "volume_mute":
            if system == "Windows":
                try:
                    from actions._audio_win import set_mute
                    muted = set_mute()
                    return "Volume muted." if muted else "Volume unmuted."
                except Exception as e:
                    return f"Mute failed: {e}. Install pycaw: pip install pycaw comtypes"
            elif system == "Linux":
                subprocess.run(
                    ["amixer", "set", "Master", "toggle"], check=False)
                return "Volume muted/unmuted."
            return "Volume control not available on this OS."

        elif action == "brightness_up":
            if system == "Windows":
                try:
                    import screen_brightness_control as sbc
                    current = sbc.get_brightness(display=0)[0]
                    sbc.set_brightness(min(100, current + 10))
                    return "Brightness increased."
                except Exception as e:
                    return f"Brightness control failed: {e}. Install: pip install screen-brightness-control"
            elif system == "Linux":
                subprocess.run(["brightnessctl", "set", "+10%"], check=False)
                return "Brightness increased."
            return "Brightness control not available on this OS."

        elif action == "brightness_down":
            if system == "Windows":
                try:
                    import screen_brightness_control as sbc
                    current = sbc.get_brightness(display=0)[0]
                    sbc.set_brightness(max(0, current - 10))
                    return "Brightness decreased."
                except Exception as e:
                    return f"Brightness control failed: {e}. Install: pip install screen-brightness-control"
            elif system == "Linux":
                subprocess.run(["brightnessctl", "set", "10%-"], check=False)
                return "Brightness decreased."
            return "Brightness control not available on this OS."

        elif action == "lock":
            if system == "Windows":
                subprocess.run(
                    ["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
            elif system == "Darwin":
                subprocess.run(
                    ["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend"], check=False)
            elif system == "Linux":
                subprocess.run(
                    ["gnome-screensaver-command", "-l"], check=False)
            return "Computer locked."

        elif action == "shutdown":
            delay = int(value) if value.isdigit() else 0
            if system == "Windows":
                subprocess.run(
                    ["shutdown", "/s", "/t", str(delay)], check=False)
            elif system in ["Linux", "Darwin"]:
                subprocess.run(
                    ["shutdown", "-h", f"+{delay}" if delay else "now"], check=False)
            return f"Shutdown scheduled in {delay} seconds."

        elif action == "cancel_shutdown":
            if system == "Windows":
                subprocess.run(["shutdown", "/a"], check=False)
            elif system in ["Linux", "Darwin"]:
                subprocess.run(["shutdown", "-c"], check=False)
            return "Shutdown cancelled."

        elif action == "restart":
            if system == "Windows":
                subprocess.run(["shutdown", "/r", "/t", "0"], check=False)
            elif system in ["Linux", "Darwin"]:
                subprocess.run(["reboot"], check=False)
            return "Restarting..."

        elif action == "type":
            if not value:
                return "Error: No text to type."
            # Requires pyautogui for cross-platform typing
            try:
                import pyautogui
                pyautogui.typewrite(value, interval=0.01)
                return f"Typed: {value}"
            except ImportError:
                return "Typing requires pyautogui: pip install pyautogui"

        elif action == "hotkey":
            if not value:
                return "Error: No hotkey specified."
            try:
                import pyautogui
                keys = value.split("+")
                pyautogui.hotkey(*keys)
                return f"Pressed: {value}"
            except ImportError:
                return "Hotkeys require pyautogui: pip install pyautogui"

        elif action == "sleep":
            if system == "Windows":
                subprocess.run(
                    ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False)
            elif system == "Darwin":
                subprocess.run(["pmset", "sleepnow"], check=False)
            elif system == "Linux":
                subprocess.run(["systemctl", "suspend"], check=False)
            return "Going to sleep."

        elif action in ("open_settings", "settings_page"):
            return _open_settings(str(args.get("value") or args.get("page") or ""))

        elif action == "list_apps":
            return _list_apps(str(args.get("value") or args.get("name") or ""))

        elif action == "uninstall_app":
            return _uninstall_app(str(args.get("value") or args.get("name") or ""))

        else:
            return f"Unknown action: {action}"

    except Exception as e:
        log.error(f"Computer settings failed: {e}")
        return f"Error: {str(e)}"
