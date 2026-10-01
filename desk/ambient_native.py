"""desk.ambient_native â€” the ambient orb as a real see-through window.

The ambient orb is thinking-orbs (desk.orb_engine) drawn into a Win32
layered window with per-pixel alpha (UpdateLayeredWindow). Between the dots
the window is genuinely absent: the desktop shows through and the compositor
blends every dot's edge against whatever is behind it.

Why not the web page it used to be: WebView2 cannot be made transparent on
this machine. Measured 2026-10-01 with pywebview 6.2.1, transparent=True,
frameless, a canvas of red dots on a transparent body: the window painted an
opaque (240,240,240) square. A colour key does not reach WebView2's
DirectComposition surface either (nova_desktop_app notes, measured earlier).

What it shows comes from NOVA's own runtime, in process: the voice session's
events (state, her voice level, tool calls) and the bridge's event bus (chat
tools). OrbStateTracker turns those into one of the nine orb states.

Clicking it opens the full window; dragging moves it. Pixels between dots
carry alpha 1/255 inside the orb's circle -- invisible, but they keep the
whole orb one target for the mouse instead of a scatter of dot-sized ones.
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

import numpy as np

from desk import orb_engine

log = logging.getLogger(__name__)

# â”€â”€ what NOVA is doing -> which orb â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

#: Tool name -> orb state while it runs.
TOOL_STATES = {
    "web_search": "searching", "research_report": "searching", "browser_control": "searching",
    "vision": "searching", "screen_processor": "searching",
    "nova_learning": "weaving", "learn_resource": "weaving", "nova_capability": "weaving",
    "remember_fact": "connecting", "nova_memory": "connecting",
    "generate_document": "composing", "file_processor": "solving",
}
#: Voice session state -> orb state.
VOICE_STATES = {
    "connecting": "connecting", "disconnecting": "connecting",
    "connected": "listening", "ready": "listening", "listening": "listening", "streaming": "listening",
    "speaking": "composing", "error": "solving",
}
IDLE = "breathing"
TOOL_HOLD_S = 45.0


class OrbStateTracker:
    """NOVA's events in, the orb state and her voice level out. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.voice = IDLE
        self.tools: dict = {}         # key -> (orb state, started)
        self.chat_busy = False
        self.level = 0.0
        self._level_at = 0.0

    def on_live(self, ev: dict) -> None:
        kind = ev.get("type")
        with self._lock:
            if kind == "state":
                self.voice = VOICE_STATES.get(str(ev.get("state", "")), IDLE)
            elif kind == "audio_level":
                self.level = float(ev.get("level") or 0.0)
                self._level_at = time.time()
            elif kind == "tool_call":
                for name in ev.get("tools") or []:
                    self.tools[name] = (TOOL_STATES.get(name, "working"), time.time())
            elif kind == "tool_result":
                self.tools.pop(ev.get("tool"), None)
            elif kind == "vision_capture":
                self.tools["vision"] = ("searching", time.time())
            elif kind in ("vision_captured", "vision_sent", "vision_failed", "vision_refused"):
                self.tools.pop("vision", None)
            elif kind in ("interrupted", "turn_complete"):
                self.tools = {k: v for k, v in self.tools.items() if k.startswith("chat:")}

    def on_bus(self, ev: dict) -> None:
        """The bridge's bus: typed chat runs tools as agents."""
        kind = ev.get("type")
        with self._lock:
            if kind == "orb_state":
                self.chat_busy = ev.get("state") == "executing"
                if ev.get("state") == "idle":
                    self.tools = {k: v for k, v in self.tools.items() if not k.startswith("chat:")}
            elif kind == "agent_start" and ev.get("tool"):
                self.tools["chat:" + str(ev.get("agent_id"))] = (
                    TOOL_STATES.get(str(ev.get("tool")), "working"), time.time())
            elif kind == "agent_done":
                self.tools.pop("chat:" + str(ev.get("agent_id")), None)

    def current(self, now: Optional[float] = None) -> tuple:
        """(orb state, NOVA's voice level 0..1)."""
        now = now or time.time()
        with self._lock:
            for k in [k for k, (_, at) in self.tools.items() if now - at > TOOL_HOLD_S]:
                self.tools.pop(k, None)
            if self.tools:
                state = max(self.tools.values(), key=lambda v: v[1])[0]
            elif self.voice != IDLE:
                state = self.voice
            elif self.chat_busy:
                state = "working"
            else:
                state = IDLE
            level = self.level if now - self._level_at < 0.4 else 0.0
            return state, max(0.0, min(1.0, level))


# â”€â”€ drawing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _composite_dots(dots: list, px: int, c: float, swell: float, dark: bool) -> tuple:
    """All dots, painted far to near with the "over" operator, in a fixed
    number of numpy steps.

    Painting them one at a time cost up to 61 ms a frame (566 dots) -- 18 fps
    and most of a core. Per pixel, "over" in order is
        colour = sum_i v_i a_i prod_{j>i} (1 - a_j)
    so every (pixel, dot) contribution is gathered, sorted by pixel then by
    paint order, and the product over later dots is a reverse running sum of
    log(1 - a) within each pixel's group. Same image, no per-dot Python.
    """
    n = len(dots)
    X = np.fromiter((c + (d["x"] - c) * swell for d in dots), np.float32, n)
    Y = np.fromiter((c + (d["y"] - c) * swell for d in dots), np.float32, n)
    R = np.fromiter((d["r"] * swell for d in dots), np.float32, n)
    A = np.fromiter((d["a"] for d in dots), np.float32, n)
    W = np.clip(np.fromiter((d["white"] for d in dots), np.float32, n), 0, 1)
    V = (1 - W) if dark else W
    k = int(np.ceil(2 * float(R.max()) + 3))
    off = np.arange(k, dtype=np.int32) - k // 2
    gx = np.floor(X).astype(np.int32)[:, None, None] + off[None, None, :]
    gy = np.floor(Y).astype(np.int32)[:, None, None] + off[None, :, None]
    dist = np.hypot(gx + 0.5 - X[:, None, None], gy + 0.5 - Y[:, None, None])
    cov = np.clip(R[:, None, None] - dist + 0.5, 0, 1) * A[:, None, None]
    keep = (cov > 1e-4) & (gx >= 0) & (gx < px) & (gy >= 0) & (gy < px)
    order = np.broadcast_to(np.arange(n)[:, None, None], cov.shape)[keep]
    pix = (gy * px + gx)[keep]
    a = np.minimum(cov[keep], 0.9999)
    v = np.broadcast_to(V[:, None, None], cov.shape)[keep]
    srt = np.lexsort((order, pix))
    pix, a, v = pix[srt], a[srt], v[srt]
    s = np.log1p(-a)
    csum = np.cumsum(s)
    starts = np.r_[0, np.flatnonzero(np.diff(pix)) + 1]
    seg = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(pix)]))
    before = np.r_[0.0, csum][starts][seg]               # sum before this pixel's group
    total = np.r_[csum[starts[1:] - 1], csum[-1]][seg] - before
    later = total - (csum - before)                      # sum over later dots, same pixel
    t = np.exp(later)
    size = px * px
    color = np.bincount(pix, weights=v * a * t, minlength=size).reshape(px, px).astype(np.float32)
    group_total = np.r_[csum[starts[1:] - 1], csum[-1]] - np.r_[0.0, csum][starts]
    alpha = np.zeros(size, np.float32)
    alpha[pix[starts]] = 1 - np.exp(group_total)
    return color, alpha.reshape(px, px)


def render_orb(state: str, t: float, px: int, dark: bool = True, level: float = 0.0) -> np.ndarray:
    """One frame as premultiplied BGRA (px, px, 4) uint8, top row first.

    The 64 px preset (dot counts, radii, speed) drawn at *px*: the engine's
    radii scale sub-linearly with size by design, which is what keeps small
    orbs legible. Her voice swells the orb a little. Dots are antialiased
    analytically (coverage = r - distance + 0.5) and composited far to near.
    """
    mode, _speed, opts = orb_engine.resolve_preset(state, 64)
    dots, lines = orb_engine.MODE_FRAMES[mode](px, t, opts)
    swell = 1.0 + 0.10 * level
    c = px / 2
    color = np.zeros((px, px), np.float32)      # premultiplied ink
    alpha = np.zeros((px, px), np.float32)
    yy, xx = np.mgrid[0:px, 0:px].astype(np.float32) + 0.5

    def ink(white):
        w = min(1.0, max(0.0, white))
        return (1 - w) if dark else w

    def blend(y0, y3, x0, x3, cov, value):
        color[y0:y3, x0:x3] = value * cov + color[y0:y3, x0:x3] * (1 - cov)
        alpha[y0:y3, x0:x3] = cov + alpha[y0:y3, x0:x3] * (1 - cov)

    for ln in lines:
        x1, y1 = c + (ln["x1"] - c) * swell, c + (ln["y1"] - c) * swell
        x2, y2 = c + (ln["x2"] - c) * swell, c + (ln["y2"] - c) * swell
        x0, x3 = int(max(0, min(x1, x2) - 2)), int(min(px, max(x1, x2) + 3))
        y0, y3 = int(max(0, min(y1, y2) - 2)), int(min(px, max(y1, y2) + 3))
        if x3 <= x0 or y3 <= y0:
            continue
        sx, sy = xx[y0:y3, x0:x3], yy[y0:y3, x0:x3]
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy or 1e-9
        u = np.clip(((sx - x1) * dx + (sy - y1) * dy) / l2, 0, 1)
        dist = np.hypot(sx - (x1 + u * dx), sy - (y1 + u * dy))
        blend(y0, y3, x0, x3, np.clip(ln["w"] / 2 - dist + 0.5, 0, 1) * ln["a"], ink(ln["white"]))

    if dots:
        dc, da = _composite_dots(dots, px, c, swell, dark)
        # dots go over the lines
        color = dc + color * (1 - da)
        alpha = da + alpha * (1 - da)

    out = np.zeros((px, px, 4), np.uint8)
    g = np.clip(color * 255 + 0.5, 0, 255).astype(np.uint8)
    out[..., 0] = out[..., 1] = out[..., 2] = g
    a = np.clip(alpha * 255 + 0.5, 0, 255).astype(np.uint8)
    # One target for the mouse: alpha 1 (invisible) inside the circle.
    inside = (xx - c) ** 2 + (yy - c) ** 2 <= (c - 0.5) ** 2
    out[..., 3] = np.where(inside & (a == 0), 1, a)
    return out


# â”€â”€ the window â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
gdi32 = ctypes.windll.gdi32 if hasattr(ctypes, "windll") else None

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x8, 0x80, 0x08000000
WM_DESTROY, WM_TIMER, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE = 0x2, 0x113, 0x201, 0x202, 0x200
WM_APP = 0x8000
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOACTIVATE = 0x1, 0x10
HWND_TOPMOST = -1
DRAG_PX = 5
FPS = 30

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


def _set_signatures() -> None:
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                           ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                           ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
                                           ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
    user32.LoadCursorW.restype = wintypes.HANDLE
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]


class AmbientOrbWindow:
    """A small always-on-top see-through orb. Same verbs the pywebview window
    offered the ambient watcher: show, hide, resize, move."""

    TITLE = "NOVA_AMBIENT"

    def __init__(self, px: int, tracker: OrbStateTracker, on_click: Callable[[], None],
                 dark: Callable[[], bool] = lambda: True, pos: tuple = (64, 64)) -> None:
        self.px = int(px)
        self.tracker = tracker
        self.on_click = on_click
        self.dark = dark
        self.hwnd = 0
        self.visible = False
        self._pos = tuple(pos)
        self._ready = threading.Event()
        self._clock = 0.0
        self._last = time.perf_counter()
        self._state = None
        self._prev = None
        self._fade = 1.0
        self._press = None
        self.frames = 0
        self.last_state = None
        self._thread = threading.Thread(target=self._run, name="AmbientOrb", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    # -- public, any thread --
    def show(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP + 1, 0, 0)

    def hide(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP + 2, 0, 0)

    def resize(self, w: int, h: int) -> None:
        self.px = int(min(w, h))

    def move(self, x: int, y: int) -> None:
        self._pos = (int(x), int(y))
        if self.hwnd:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)

    def position(self) -> tuple:
        r = wintypes.RECT()
        if self.hwnd and user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            return r.left, r.top
        return self._pos

    def destroy(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP + 3, 0, 0)

    # -- window thread --
    def _run(self) -> None:
        try:
            _set_signatures()
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
            self._proc = WNDPROC(self._wndproc)        # kept alive: Windows calls it
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = "NovaAmbientOrb"
            wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32649))   # IDC_HAND
            user32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                "NovaAmbientOrb", self.TITLE, WS_POPUP, self._pos[0], self._pos[1],
                self.px, self.px, None, None, hinst, None) or 0
            if self.hwnd:
                # 25 ms: Windows rounds timers to its 15.6 ms tick, so this lands on
                # ~31 ms (32 fps); 33 ms became 47 ms (21 fps, measured).
                user32.SetTimer(self.hwnd, 1, 25, None)
        except Exception as e:
            log.error("ambient orb window failed: %s", e)
            self.hwnd = 0
        finally:
            self._ready.set()
        if not self.hwnd:
            return
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TIMER:
                if self.visible:
                    self._paint()
                return 0
            if msg == WM_APP + 1:
                self.visible = True
                self._paint()
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                return 0
            if msg == WM_APP + 2:
                self.visible = False
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if msg == WM_APP + 3:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if msg == WM_LBUTTONDOWN:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                x, y = self.position()
                self._press = (pt.x, pt.y, x, y, False)
                user32.SetCapture(hwnd)
                log.debug("Ambient orb: press at (%d,%d), window at (%d,%d)", pt.x, pt.y, x, y)
                return 0
            if msg == WM_MOUSEMOVE and self._press:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                sx, sy, wx, wy, moved = self._press
                if moved or abs(pt.x - sx) > DRAG_PX or abs(pt.y - sy) > DRAG_PX:
                    self._press = (sx, sy, wx, wy, True)
                    self.move(wx + pt.x - sx, wy + pt.y - sy)
                return 0
            if msg == WM_LBUTTONUP:
                press, self._press = self._press, None
                user32.ReleaseCapture()
                log.debug("Ambient orb: release (%s)", "drag" if press and press[4] else "click" if press else "no press")
                if press and not press[4]:
                    threading.Thread(target=self._click, daemon=True).start()
                return 0
        except Exception as e:
            log.debug("ambient orb: %s", e)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _click(self) -> None:
        try:
            self.on_click()
        except Exception as e:
            log.warning("ambient orb click failed: %s", e)

    def _paint(self) -> None:
        state, level = self.tracker.current()
        self.last_state = state
        now = time.perf_counter()
        _, speed, _ = orb_engine.resolve_preset(state, 64)
        # Each state keeps its own tempo; her voice quickens it a little.
        self._clock += (now - self._last) * speed * (1 + 0.6 * level)
        self._last = now
        if state != self._state:
            log.info("Ambient orb: %s -> %s", self._state, state)
            self._prev, self._state, self._fade = self._state, state, 0.0
        self._fade = min(1.0, self._fade + 1 / (FPS * 0.35))
        dark = bool(self.dark())
        img = render_orb(state, self._clock, self.px, dark, level)
        if self._prev and self._fade < 1.0:
            # Crossfade: the old state dissolves while the new one forms.
            old = render_orb(self._prev, self._clock, self.px, dark, level)
            img = (img.astype(np.float32) * self._fade + old.astype(np.float32) * (1 - self._fade)).astype(np.uint8)
            img[..., 3] = np.maximum(img[..., 3], 1)
        self._blit(np.ascontiguousarray(img[::-1]))   # DIB rows are bottom-up
        self.frames += 1

    def _blit(self, bgra: np.ndarray) -> None:
        px = bgra.shape[0]
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), px, px, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, bgra.ctypes.data, bgra.nbytes)
            old = gdi32.SelectObject(mem, hbmp)
            x, y = self.position()
            blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
                                       ctypes.byref(wintypes.SIZE(px, px)), mem,
                                       ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(mem, old)
        finally:
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)


def attach_live_events(tracker: OrbStateTracker, stop: Optional[threading.Event] = None) -> threading.Event:
    """Feed the tracker from the voice session until *stop* is set."""
    stop = stop or threading.Event()

    def run():
        while not stop.is_set():
            try:
                from desk import live_session
                mgr = live_session.get_live_manager()
                q = mgr.subscribe()
            except Exception:
                time.sleep(1.0)
                continue
            try:
                while not stop.is_set():
                    try:
                        ev = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    try:
                        tracker.on_live(ev.to_dict() if hasattr(ev, "to_dict") else dict(ev))
                    except Exception:
                        pass
            finally:
                try:
                    mgr.unsubscribe(q)
                except Exception:
                    pass
    threading.Thread(target=run, name="AmbientOrbEvents", daemon=True).start()
    return stop


__all__ = ["AmbientOrbWindow", "OrbStateTracker", "render_orb", "attach_live_events", "TOOL_STATES"]
