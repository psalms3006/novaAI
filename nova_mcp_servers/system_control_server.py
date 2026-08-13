"""
nova_mcp_servers/system_control_server.py
═══════════════════════════════════════════
Local MCP server exposing OS-level control (volume, brightness, apps,
browser, keyboard, mouse, screenshots) as individually-typed MCP tools.

Why this is a separate process reached via stdio, and not just more Python
functions called in-process: these are genuinely stateless, one-shot system
calls — there's no shared in-process state to lose by crossing a process
boundary (unlike memory/planner/embeddings, which stay in-process — see the
architecture note in nova.py where this server is registered). Standardizing
them as MCP tools also means any other MCP-speaking client (not just NOVA)
can drive the same machine the same way, which plain Python functions can't
offer.

Run standalone for testing:
    python nova_mcp_servers/system_control_server.py

NOVA launches this automatically via nova_mcp — see the ServerConfig entry
named "system_control" in nova.py's _mcp_configs list.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Make the project root importable (this file lives one level down) so we can
# reuse actions/*.py instead of re-implementing volume/brightness/app logic.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

import actions.computer_settings as _settings
import actions.open_app as _open_app
import actions.close_app as _close_app

try:
    import actions.browser_control as _browser
    _HAS_BROWSER = True
except ImportError:
    _HAS_BROWSER = False

# Helper to lazy-load pyautogui
def _get_pyautogui():
    try:
        import pyautogui
        return pyautogui
    except ImportError:
        return None

mcp = FastMCP("nova-system-control")


# ── Volume ──────────────────────────────────────────────────────────────────

@mcp.tool()
def volume_up() -> str:
    """Increase system volume by a fixed step."""
    return _settings.execute({"action": "volume_up"})


@mcp.tool()
def volume_down() -> str:
    """Decrease system volume by a fixed step."""
    return _settings.execute({"action": "volume_down"})


@mcp.tool()
def volume_mute() -> str:
    """Toggle system audio mute on or off."""
    return _settings.execute({"action": "volume_mute"})


# ── Brightness ───────────────────────────────────────────────────────────────

@mcp.tool()
def brightness_up() -> str:
    """Increase screen brightness by 10%."""
    return _settings.execute({"action": "brightness_up"})


@mcp.tool()
def brightness_down() -> str:
    """Decrease screen brightness by 10%."""
    return _settings.execute({"action": "brightness_down"})


# ── Applications ─────────────────────────────────────────────────────────────

@mcp.tool()
def open_app(app_name: str) -> str:
    """Open an application by name, e.g. 'Chrome', 'Notepad', 'Spotify'."""
    return _open_app.execute({"app_name": app_name})


@mcp.tool()
def close_app(app_name: str) -> str:
    """Close/terminate a running application by name, e.g. 'Chrome'."""
    return _close_app.execute({"app_name": app_name})


# ── System power ─────────────────────────────────────────────────────────────

@mcp.tool()
def lock_pc() -> str:
    """Lock the computer immediately."""
    return _settings.execute({"action": "lock"})


@mcp.tool()
def sleep_pc() -> str:
    """Put the computer to sleep."""
    return _settings.execute({"action": "sleep"})


@mcp.tool()
def restart_pc() -> str:
    """Restart the computer immediately. Destructive — confirm with the user first."""
    return _settings.execute({"action": "restart"})


@mcp.tool()
def shutdown_pc(delay_seconds: int = 0) -> str:
    """Shut down the computer. Destructive — confirm with the user first."""
    return _settings.execute({"action": "shutdown", "value": str(delay_seconds)})


@mcp.tool()
def cancel_shutdown() -> str:
    """Cancel a previously scheduled shutdown."""
    return _settings.execute({"action": "cancel_shutdown"})


@mcp.tool()
def open_settings() -> str:
    """Open the Windows Settings app."""
    return _open_app.execute({"app_name": "settings"})


# ── Browser ──────────────────────────────────────────────────────────────────

@mcp.tool()
def browser_control(action: str, url: str = "") -> str:
    """Control the default web browser. action: 'open_url', 'new_tab', 'close_tab',
    'back', 'forward', 'refresh' (exact supported actions depend on browser_control.py)."""
    if not _HAS_BROWSER:
        return "browser_control module not available."
    return _browser.execute({"action": action, "url": url})


# ── Keyboard / mouse / screenshot ────────────────────────────────────────────

@mcp.tool()
def type_text(text: str) -> str:
    """Type the given text at the current cursor/focus position."""
    pyautogui = _get_pyautogui()
    if pyautogui is None:
        return "pyautogui not installed. Run: pip install pyautogui"
    pyautogui.write(text, interval=0.04)
    return f"Typed: {text[:60]}"


@mcp.tool()
def press_hotkey(keys: str) -> str:
    """Press a keyboard shortcut, e.g. 'ctrl+c' or 'alt+tab' (plus-separated)."""
    pyautogui = _get_pyautogui()
    if pyautogui is None:
        return "pyautogui not installed. Run: pip install pyautogui"
    parts = [k.strip() for k in keys.split("+") if k.strip()]
    pyautogui.hotkey(*parts)
    return f"Hotkey: {keys}"


@mcp.tool()
def press_key(key: str) -> str:
    """Press a single key, e.g. 'enter', 'escape', 'tab'."""
    pyautogui = _get_pyautogui()
    if pyautogui is None:
        return "pyautogui not installed. Run: pip install pyautogui"
    pyautogui.press(key)
    return f"Pressed: {key}"


@mcp.tool()
def mouse_click(x: int = -1, y: int = -1, button: str = "left") -> str:
    """Click the mouse. Pass x=-1, y=-1 (default) to click at the current cursor position."""
    pyautogui = _get_pyautogui()
    if pyautogui is None:
        return "pyautogui not installed. Run: pip install pyautogui"
    kwargs = {"button": button} if button in ("left", "right", "middle") else {}
    if x >= 0 and y >= 0:
        pyautogui.click(int(x), int(y), **kwargs)
        return f"Clicked ({button}) at ({x}, {y})"
    pyautogui.click(**kwargs)
    return f"Clicked ({button}) at current position"


@mcp.tool()
def mouse_move(x: int, y: int) -> str:
    """Move the mouse cursor to absolute screen coordinates."""
    pyautogui = _get_pyautogui()
    if pyautogui is None:
        return "pyautogui not installed. Run: pip install pyautogui"
    pyautogui.moveTo(int(x), int(y), duration=0.3)
    return f"Moved mouse to ({x}, {y})"


@mcp.tool()
def mouse_scroll(direction: str = "down", amount: int = 3) -> str:
    """Scroll the mouse wheel. direction: 'up' or 'down'."""
    pyautogui = _get_pyautogui()
    if pyautogui is None:
        return "pyautogui not installed. Run: pip install pyautogui"
    pyautogui.scroll(-amount if direction == "down" else amount)
    return f"Scrolled {direction} by {amount}"


@mcp.tool()
def take_screenshot(path: str = "") -> str:
    """Capture a screenshot and save it to disk. Returns the saved file path."""
    pyautogui = _get_pyautogui()
    if pyautogui is None:
        return "pyautogui not installed. Run: pip install pyautogui"
    save_path = path or str(Path.home() / "Desktop" / f"nova_cap_{int(time.time())}.png")
    pyautogui.screenshot(save_path)
    return f"Screenshot saved: {save_path}"


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
