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

        else:
            return f"Unknown action: {action}"

    except Exception as e:
        log.error(f"Computer settings failed: {e}")
        return f"Error: {str(e)}"
