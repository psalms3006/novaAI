import platform
import subprocess
import time
import logging

log = logging.getLogger(__name__)


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

        elif action == "volume_up":
            if system == "Windows":
                subprocess.run(
                    ["nircmd.exe", "changesysvolume", "2000"], check=False)
            elif system == "Linux":
                subprocess.run(["amixer", "set", "Master", "5%+"], check=False)
            return "Volume increased."

        elif action == "volume_down":
            if system == "Windows":
                subprocess.run(
                    ["nircmd.exe", "changesysvolume", "-2000"], check=False)
            elif system == "Linux":
                subprocess.run(["amixer", "set", "Master", "5%-"], check=False)
            return "Volume decreased."

        elif action == "volume_mute":
            if system == "Windows":
                subprocess.run(
                    ["nircmd.exe", "mutesysvolume", "2"], check=False)
            elif system == "Linux":
                subprocess.run(
                    ["amixer", "set", "Master", "toggle"], check=False)
            return "Volume muted/unmuted."

        elif action == "brightness_up":
            if system == "Windows":
                # Requires brightness control utility
                return "Brightness control not implemented for Windows."
            elif system == "Linux":
                subprocess.run(["brightnessctl", "set", "+10%"], check=False)
                return "Brightness increased."
            return "Brightness control not available."

        elif action == "brightness_down":
            if system == "Linux":
                subprocess.run(["brightnessctl", "set", "10%-"], check=False)
                return "Brightness decreased."
            return "Brightness control not available."

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

        else:
            return f"Unknown action: {action}"

    except Exception as e:
        log.error(f"Computer settings failed: {e}")
        return f"Error: {str(e)}"
