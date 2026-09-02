"""win_overlay.py — Windows-specific ambient orb overlay.

Handles always-on-top, borderless, click-through window for ambient mode.
Uses Win32 API via ctypes for WS_EX_LAYERED | WS_EX_TRANSPARENT hit-testing.

Platform: Windows only. macOS/Linux stubs degrade gracefully (non-click-through).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

# Win32 constants
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
LWA_ALPHA = 0x00000002
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202

_is_windows = sys.platform == "win32"

# Position persistence file
_pos_file = Path.home() / ".nova" / "ambient_pos.json"

# Log file
_log_file = Path.home() / ".nova" / "overlay.log"


def _log(msg: str):
    """Append a timestamped line to the overlay log file."""
    try:
        _log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _load_position() -> tuple[int, int]:
    """Load persisted ambient window position."""
    try:
        if _pos_file.exists():
            data = json.loads(_pos_file.read_text())
            x, y = data.get("x", -1), data.get("y", -1)
            _log(f"Loaded cached position: ({x}, {y})")
            return x, y
    except Exception as e:
        _log(f"Failed to load cached position: {e}")
    # Default: top-right corner with 20px margin
    import ctypes
    user32 = ctypes.windll.user32
    screen_w = user32.GetSystemMetrics(0)
    default_x = screen_w - 140
    default_y = 20
    _log(f"Using default position: ({default_x}, {default_y})")
    return default_x, default_y


def _save_position(x: int, y: int):
    """Persist ambient window position."""
    try:
        _pos_file.parent.mkdir(parents=True, exist_ok=True)
        _pos_file.write_text(json.dumps({"x": x, "y": y}))
    except Exception:
        pass


def _clamp_to_screen(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    """Clamp position so the window is fully visible on at least one monitor."""
    import ctypes
    user32 = ctypes.windll.user32
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    _log(f"Screen metrics: {screen_w}x{screen_h}")

    orig_x, orig_y = x, y

    # Clamp so window is within primary screen bounds
    if x + w > screen_w:
        x = screen_w - w
    if y + h > screen_h:
        y = screen_h - h
    if x < 0:
        x = 0
    if y < 0:
        y = 0

    if (x, y) != (orig_x, orig_y):
        _log(f"Clamped position from ({orig_x}, {orig_y}) to ({x}, {y})")
    else:
        _log(f"Position ({x}, {y}) is within screen bounds — no clamp needed")

    return x, y


class AmbientOverlay:
    """Manages a small always-on-top click-through window for the ambient orb.

    On Windows: true click-through with WS_EX_LAYERED | WS_EX_TRANSPARENT.
    Clicks land on the orb hit region; all other input passes through.
    On non-Windows: normal always-on-top small window (no click-through).
    """

    def __init__(self, width: int = 120, height: int = 120):
        self.width = width
        self.height = height
        self._hwnd = None
        self._click_through = True
        self._visible = False
        self._orb_bounds = (0, 0, width, height)  # relative to window
        self._on_click = None  # callback for orb clicks
        self._on_expand = None  # callback for expand request
        self._monitor_thread = None
        self._stop_event = threading.Event()
        _log(f"AmbientOverlay created: {width}x{height}, platform={sys.platform}")

    def set_click_callback(self, callback):
        """Set callback for orb click events."""
        self._on_click = callback

    def set_expand_callback(self, callback):
        """Set callback for expand-to-full-mode requests."""
        self._on_expand = callback

    def show(self):
        """Show the ambient overlay window."""
        _log("AmbientOverlay.show() called")
        if not _is_windows:
            _log("Not Windows — using fallback")
            self._show_fallback()
            return

        try:
            import ctypes
            import ctypes.wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # Get screen size
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            _log(f"Screen size: {screen_w}x{screen_h}")

            # Load position
            x, y = _load_position()

            # Validate against screen bounds BEFORE using
            x, y = _clamp_to_screen(x, y, self.width, self.height)

            # Delete cached position if it was bad (so next launch starts fresh)
            try:
                if _pos_file.exists():
                    cached = json.loads(_pos_file.read_text())
                    if (cached.get("x", -1), cached.get("y", -1)) != (x, y):
                        _log("Cached position was off-screen — clearing cache")
                        _pos_file.unlink(missing_ok=True)
            except Exception:
                pass

            # Create borderless topmost window
            WNDCLASS = "NovaAmbient"
            hinstance = kernel32.GetModuleHandleW(None)

            # Register window class
            wc = ctypes.wintypes.WNDCLASSEX()
            wc.cbSize = ctypes.sizeof(wc)
            wc.lpfnWndProc = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
            )(self._wnd_proc)
            wc.hInstance = hinstance
            wc.lpszClassName = WNDCLASS
            reg_result = user32.RegisterClassExW(ctypes.byref(wc))
            _log(f"RegisterClassExW result: {reg_result}")

            # Create window
            ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
            _log(f"Creating window: ex_style=0x{ex_style:08x}, pos=({x},{y}), size=({self.width},{self.height})")
            hwnd = user32.CreateWindowExW(
                ex_style, WNDCLASS, "NOVA",
                0x80000000,  # WS_POPUP
                x, y, self.width, self.height,
                None, None, hinstance, None
            )

            if not hwnd:
                _log("ERROR: CreateWindowExW returned NULL — window creation failed")
                return

            _log(f"Window created: hwnd=0x{hwnd:08x}")

            # Set layered window alpha: 255 = fully opaque content
            alpha = 255
            result = user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
            _log(f"SetLayeredWindowAttributes: alpha={alpha}, result={result}")

            # Show window
            show_result = user32.ShowWindow(hwnd, SWP_SHOWWINDOW)
            _log(f"ShowWindow result: {show_result}")

            # Ensure topmost
            pos_result = user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            _log(f"SetWindowPos result: {pos_result}")

            self._hwnd = hwnd
            self._visible = True
            self._x = x
            self._y = y

            # Verify visibility: check window rect
            try:
                rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                _log(f"GetWindowRect: left={rect.left}, top={rect.top}, right={rect.right}, bottom={rect.bottom}, "
                     f"actual_size={rect.right - rect.left}x{rect.bottom - rect.top}")
                if rect.right - rect.left == 0 or rect.bottom - rect.top == 0:
                    _log("WARNING: Window has zero size!")
            except Exception as e:
                _log(f"GetWindowRect failed: {e}")

            # Verify it's actually visible
            is_visible = user32.IsWindowVisible(hwnd)
            _log(f"IsWindowVisible: {is_visible}")

            # Start monitor thread for hit-testing
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()

            _log("AmbientOverlay.show() completed successfully")

        except Exception as e:
            _log(f"ERROR in AmbientOverlay.show(): {e}")
            import traceback
            _log(traceback.format_exc())
            self._show_fallback()

    def hide(self):
        """Hide the ambient overlay window."""
        if not _is_windows or not self._hwnd:
            return

        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.ShowWindow(self._hwnd, 0)  # SW_HIDE
            self._visible = False
            self._stop_event.set()
            _log("AmbientOverlay hidden")
        except Exception:
            pass

    def destroy(self):
        """Destroy the ambient overlay window."""
        self._stop_event.set()
        if _is_windows and self._hwnd:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
                self._visible = False
                _log("AmbientOverlay destroyed")
            except Exception:
                pass

    def move(self, x: int, y: int):
        """Move the overlay window."""
        if _is_windows and self._hwnd:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                x, y = _clamp_to_screen(x, y, self.width, self.height)
                user32.SetWindowPos(self._hwnd, HWND_TOPMOST, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOACTIVATE)
                self._x = x
                self._y = y
                _save_position(x, y)
                _log(f"Moved to ({x}, {y})")
            except Exception:
                pass

    def set_click_through(self, enabled: bool):
        """Toggle click-through mode."""
        if not _is_windows or not self._hwnd:
            return

        try:
            import ctypes
            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
            if enabled:
                ex_style |= WS_EX_TRANSPARENT
            else:
                ex_style &= ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex_style)
            self._click_through = enabled
            _log(f"Click-through: {enabled}")
        except Exception:
            pass

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        """Window procedure for handling mouse events."""
        if msg == WM_LBUTTONDOWN:
            _log("Orb clicked!")
            if self._on_click:
                self._on_click()
            return 0
        elif msg == WM_MOUSEMOVE:
            pass

        try:
            import ctypes
            user32 = ctypes.windll.user32
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    def _monitor_loop(self):
        """Monitor thread: saves position periodically and handles cleanup."""
        while not self._stop_event.is_set():
            try:
                if hasattr(self, '_x') and hasattr(self, '_y'):
                    _save_position(self._x, self._y)
            except Exception:
                pass
            self._stop_event.wait(5.0)

    def _show_fallback(self):
        """Fallback for non-Windows: just log that ambient mode isn't fully supported."""
        _log("Ambient mode: non-click-through window (Windows-only feature)")

    def is_available(self) -> bool:
        """Check if ambient overlay is available on this platform."""
        return _is_windows


class FullWindowManager:
    """Manages the full-mode window (normal pywebview window).

    Handles transitions between ambient and full mode.
    """

    def __init__(self):
        self._window = None
        self._mode = "full"  # "ambient" or "full"
        _log("FullWindowManager created")

    def set_window(self, window):
        """Set the pywebview window reference."""
        self._window = window
        _log(f"FullWindowManager.set_window: {window}")

    @property
    def mode(self) -> str:
        return self._mode

    def transition_to_full(self):
        """Transition from ambient to full mode."""
        self._mode = "full"
        _log("FullWindowManager: transitioning to full mode")
        if self._window:
            try:
                self._window.resize(1280, 840)
                self._window.evaluate_js("document.body.dataset.mode = 'full'")
                _log("FullWindowManager: transition to full complete")
            except Exception as e:
                _log(f"FullWindowManager: transition to full failed: {e}")

    def transition_to_ambient(self):
        """Transition from full to ambient mode."""
        self._mode = "ambient"
        _log("FullWindowManager: transitioning to ambient mode")
        if self._window:
            try:
                self._window.evaluate_js("document.body.dataset.mode = 'ambient'")
            except Exception as e:
                _log(f"FullWindowManager: transition to ambient failed: {e}")
