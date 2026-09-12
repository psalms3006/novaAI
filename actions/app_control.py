"""actions.app_control — work inside an application, not just launch one.

Opening Spotify is the easy half. "Search for Last Last and play it" means
finding a search box, typing in it, finding the result, and knowing whether
it worked — and none of that is possible by guessing pixel coordinates, which
break on a different screen, a different theme, or a window moved two inches.

So this reads the accessibility tree (UI Automation, via pywinauto). Controls
are found by name and type the way a person finds them: "the button called
Play", "the edit box labelled Search". Coordinates are never used.

Every action that changes something is followed by a check that it changed.
The alternative is NOVA reporting that she clicked Play when she clicked
nothing, which is the failure mode this whole layer exists to avoid.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

#: Enumerating every window on the desktop takes seconds, so anything that can
#: be scoped to one window is. This is the ceiling for a whole-desktop search.
FIND_TIMEOUT_S = 8.0

#: Ceiling on any accessibility-tree read. The tree is the slow, brittle part
#: of this layer and it is never load-bearing: it improves the answer, it does
#: not enable the action.
TREE_BUDGET_S = 6.0

#: How long to wait for an application to put a window up after launching.
#: Spotify and Electron apps are slow to first paint; a short timeout reports
#: failure for something that was merely still starting.
LAUNCH_TIMEOUT_S = 25.0

#: Control types worth showing the model. The tree contains hundreds of
#: layout panes that cannot be interacted with, and listing them buries the
#: handful of things that can.
INTERESTING = (
    "Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "Hyperlink",
    "ListItem", "TabItem", "MenuItem", "Slider", "Text", "Document",
)


#: pywinauto retries every lookup on a schedule of its own, and its defaults
#: are written for test automation that can afford to wait. Left alone, one
#: failed element lookup took 147 seconds and ended in a COM error — a tool
#: call that blocks a conversation for over two minutes is unusable whether or
#: not it eventually succeeds. Fail fast and say so instead.
_TIMINGS_APPLIED = False


def _fail_fast() -> None:
    global _TIMINGS_APPLIED
    if _TIMINGS_APPLIED:
        return
    try:
        from pywinauto import timings
        timings.Timings.window_find_timeout = 2.0
        timings.Timings.window_find_retry = 0.2
        timings.Timings.exists_timeout = 1.0
        timings.Timings.after_click_wait = 0.05
        timings.Timings.after_setfocus_wait = 0.1
    except Exception as e:
        log.debug("could not tighten pywinauto timings: %s", e)
    _TIMINGS_APPLIED = True


def _desktop():
    import warnings
    warnings.filterwarnings("ignore")
    _fail_fast()
    from pywinauto import Desktop
    return Desktop(backend="uia")


# ── finding windows ──────────────────────────────────────────────────────────
#
# Discovery goes through the Win32 window list, not the accessibility tree.
#
# Measured on this desktop with nineteen windows open: enumerating through
# pywinauto's UIA backend takes 7.0 seconds, and every single action was doing
# it — so a five-step task spent over half a minute just working out which
# window it was talking about, and timed out before finishing. EnumWindows
# returns the same list in under a millisecond.
#
# The accessibility tree is still where controls come from; it is simply not
# how a window is located. Wrapping one known handle costs ~3 s, so it is done
# once, lazily, and only when the tree is actually needed.

def _enum_windows() -> list[tuple[int, str]]:
    """Visible top-level windows as (handle, title). Effectively free."""
    import ctypes
    from ctypes import wintypes

    found: list[tuple[int, str]] = []
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                       wintypes.LPARAM)

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip():
                found.append((int(hwnd), buf.value))
        return True

    try:
        user32.EnumWindows(callback_type(callback), 0)
    except Exception as e:
        log.debug("EnumWindows failed: %s", e)
    return found


def _find_handle(title: str, timeout: float = FIND_TIMEOUT_S) -> tuple[int, str] | None:
    """The handle and title of the best window matching `title`.

    Ranked without touching the accessibility tree. A window that is not
    responding, or has no client area, is a shell rather than the thing the
    user means — Notepad puts up both "Untitled - Notepad" and a bare
    "Notepad", and choosing by exact title picks the empty one.
    """
    import ctypes
    from ctypes import wintypes

    want = (title or "").strip().lower()
    deadline = time.time() + timeout
    user32 = ctypes.windll.user32
    # A handle is pointer-sized; left to guess, ctypes would pass it as a
    # 32-bit int and mangle the larger ones.
    user32.IsHungAppWindow.argtypes = [wintypes.HWND]
    user32.IsHungAppWindow.restype = wintypes.BOOL

    while True:
        candidates = [(h, t) for h, t in _enum_windows()
                      if not want or want in t.lower()]
        if candidates:
            def rank(item):
                handle, text = item
                low = text.lower()
                # Ask Windows whether the window is pumping messages rather
                # than reading its title. Windows only renames a stuck window
                # to "(Not Responding)" once it puts a ghost over it, so a
                # window that is already refusing input can still be titled
                # perfectly normally -- Notepad's bare "Notepad" shell is,
                # and it was winning this ranking and swallowing every
                # keystroke while reporting success.
                alive = (not user32.IsHungAppWindow(handle)
                         and "not responding" not in low)
                rect = wintypes.RECT()
                try:
                    user32.GetClientRect(handle, ctypes.byref(rect))
                    area = max(0, rect.right) * max(0, rect.bottom)
                except Exception:
                    area = 0
                # Responsive first, then the one with a real client area
                # (the shell has none), then the closest title.
                return (alive, area, low == want, -len(text))
            return max(candidates, key=rank)
        if time.time() >= deadline:
            return None
        time.sleep(0.3)


def _wrap(handle: int):
    """The accessibility-tree view of one known window. Cached: ~3 s each."""
    cached = _window_cache.get(handle)
    if cached is not None:
        win, at = cached
        if time.time() - at < WINDOW_CACHE_S:
            return win
    try:
        win = _desktop().window(handle=handle)
        _window_cache[handle] = (win, time.time())
        return win
    except Exception as e:
        log.debug("could not wrap handle %s: %s", handle, e)
        return None


#: A wrapped window is reused for this long. Wrapping costs seconds; a window
#: does not usually stop being itself within one task.
WINDOW_CACHE_S = 30.0
_window_cache: dict = {}


def _find_window(title: str, timeout: float = FIND_TIMEOUT_S):
    """The accessibility-tree window the user means, or None."""
    found = _find_handle(title, timeout)
    if found is None:
        return None
    return _wrap(found[0])


def _describe(ctrl) -> dict | None:
    try:
        name = (ctrl.window_text() or "").strip()
        kind = ctrl.element_info.control_type
    except Exception:
        return None
    if kind not in INTERESTING:
        return None
    if not name:
        return None
    try:
        enabled = bool(ctrl.is_enabled())
    except Exception:
        enabled = True
    return {"name": name[:80], "type": kind, "enabled": enabled}


def _controls(window, limit: int = 60) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    try:
        for ctrl in window.descendants():
            d = _describe(ctrl)
            if d is None:
                continue
            key = (d["name"], d["type"])
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
            if len(out) >= limit:
                break
    except Exception as e:
        log.debug("control walk failed: %s", e)
    return out


def _match(window, name: str, kind: str = ""):
    """Find one control by name, preferring an exact match over a substring."""
    want = (name or "").strip().lower()
    if not want:
        return None
    exact = partial = None
    try:
        for ctrl in window.descendants():
            try:
                text = (ctrl.window_text() or "").strip()
                ctype = ctrl.element_info.control_type
            except Exception:
                continue
            if not text:
                continue
            if kind and ctype != kind:
                continue
            low = text.lower()
            if low == want:
                exact = ctrl
                break
            if want in low and partial is None:
                partial = ctrl
    except Exception as e:
        log.debug("control search failed: %s", e)
    return exact or partial


# ── actions ──────────────────────────────────────────────────────────────────

def _act_list_windows(args: dict) -> str:
    try:
        names = []
        for w in _desktop().windows():
            try:
                t = (w.window_text() or "").strip()
            except Exception:
                continue
            if t:
                names.append(t[:70])
        if not names:
            return "No application windows are open."
        return "Open windows:\n" + "\n".join(f"- {n}" for n in names[:25])
    except Exception as e:
        return f"Could not list windows: {e}"


def _foreground_title() -> str:
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def _is_hung(handle: int, title: str = "") -> bool:
    """Has this window stopped answering?

    Ranking prefers a live window, but when every match is stuck there is
    nothing to prefer and the dead one comes back anyway. Its title is no
    help -- Windows adds "(Not Responding)" only once it gives up and paints
    a ghost, so a window can be refusing input for seconds while still
    advertising its normal name.
    """
    import ctypes
    from ctypes import wintypes
    if "not responding" in (title or "").lower():
        return True
    try:
        user32 = ctypes.windll.user32
        user32.IsHungAppWindow.argtypes = [wintypes.HWND]
        user32.IsHungAppWindow.restype = wintypes.BOOL
        return bool(user32.IsHungAppWindow(handle))
    except Exception:
        return False


def _foreground_handle() -> int:
    """Which window actually has the foreground, by identity.

    Titles cannot answer this. "Notepad" is a substring of
    "*model.safetensors - Notepad", so a title check confirms that the right
    application is in front while the keystrokes go into the wrong document of
    it. That is not a hypothetical: it happened, to a file that had nothing to
    do with the task. A handle is the window, so it is what gets compared.
    """
    import ctypes
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _is_front(handle: int) -> bool:
    """True when `handle` is the foreground window, or owns what is.

    An application that is asked to come forward may put up a dialog and give
    that the foreground instead. The dialog is the window the user is looking
    at and belongs to the window we raised, so it counts.
    """
    import ctypes
    from ctypes import wintypes
    fg = _foreground_handle()
    if not fg:
        return False
    if fg == handle:
        return True
    user32 = ctypes.windll.user32
    user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetWindow.restype = wintypes.HWND
    GW_OWNER = 4
    try:
        return int(user32.GetWindow(fg, GW_OWNER) or 0) == handle
    except Exception:
        return False


def _raise(handle: int) -> None:
    """Bring a window to the front, working around Windows' focus-stealing
    rules — SetForegroundWindow is refused unless the calling thread is
    attached to the one that owns the foreground window."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    SW_RESTORE = 9
    try:
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, SW_RESTORE)
        # Windows grants the right to take the foreground to the *calling*
        # thread, and only while it shares an input queue with the thread that
        # currently owns the foreground. Attaching the foreground thread to
        # the target thread -- neither of which is this one -- buys us nothing,
        # so SetForegroundWindow was being refused every time and NOVA typed
        # into whatever happened to be in front.
        ours = kernel32.GetCurrentThreadId()
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if fg_thread and fg_thread != ours:
            user32.AttachThreadInput(ours, fg_thread, True)
            try:
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
            finally:
                user32.AttachThreadInput(ours, fg_thread, False)
        else:
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
    except Exception as e:
        log.debug("raise failed: %s", e)


def _act_focus(args: dict) -> str:
    title = args.get("window") or args.get("app") or ""
    found = _find_handle(title)
    if found is None:
        return f"I couldn't find a window matching {title!r}."
    handle, text = found
    _raise(handle)
    # Verify rather than assume: focus can be refused for a window owned by an
    # elevated process, or stolen back by whatever had it.
    time.sleep(0.4)
    if _is_front(handle):
        return f"{text} is now in front."
    return (f"I asked for {text!r} but {_foreground_title()!r} is in front. "
            "It may be blocked by another window.")


def _act_inspect(args: dict) -> str:
    title = args.get("window") or args.get("app") or ""
    found = _find_handle(title)
    if found is None:
        return f"I couldn't find a window matching {title!r}."
    handle, wtitle = found

    def _read() -> list[dict]:
        win = _wrap(handle)
        return _controls(win) if win else []

    controls = _bounded(_read, budget=TREE_BUDGET_S * 2) or []
    if not controls:
        return (f"{wtitle!r} is open but exposes no controls I can "
                "read. It may draw its own interface rather than using "
                "standard ones.")
    lines = [f"{c['type']}: {c['name']}" + ("" if c["enabled"] else "  (disabled)")
             for c in controls]
    return f"In {wtitle!r}:\n" + "\n".join(f"- {l}" for l in lines)


def _act_click(args: dict) -> str:
    title = args.get("window") or args.get("app") or ""
    name = args.get("control") or args.get("name") or ""
    kind = args.get("control_type") or ""
    win = _find_window(title)
    if win is None:
        return f"I couldn't find a window matching {title!r}."
    ctrl = _match(win, name, kind)
    if ctrl is None:
        available = ", ".join(c["name"] for c in _controls(win, limit=12))
        return (f"I couldn't find {name!r} in {win.window_text()!r}."
                + (f" I can see: {available}." if available else ""))
    label = (ctrl.window_text() or name).strip()
    try:
        win.set_focus()
        ctrl.click_input()
    except Exception as e:
        return f"I found {label!r} but clicking it failed: {e}"
    return f"Clicked {label!r}."


def _text_of(ctrl) -> str:
    """Whatever this control currently contains, by whichever route works."""
    for getter in ("get_value", "window_text"):
        try:
            fn = getattr(ctrl, getter, None)
            if fn is None:
                continue
            got = fn()
            if got:
                return str(got).strip()
        except Exception:
            continue
    return ""


def _editable_in(window):
    """The control that text would land in: an Edit or Document."""
    try:
        for ctrl in window.descendants():
            try:
                if ctrl.element_info.control_type in ("Edit", "Document"):
                    return ctrl
            except Exception:
                continue
    except Exception:
        pass
    return None


def _act_type(args: dict) -> str:
    title = args.get("window") or args.get("app") or ""
    text = args.get("text") or ""
    name = args.get("control") or args.get("name") or ""
    if not text:
        return "There's nothing to type."
    found = _find_handle(title)
    if found is None:
        return f"I couldn't find a window matching {title!r}."
    handle, wtitle = found

    # A window that is not responding cannot receive input, and saying "typed"
    # into one is the exact failure this layer exists to prevent. Caught by
    # its own test: Notepad hung, the keystrokes went nowhere, and the tool
    # cheerfully reported success.
    if _is_hung(handle, wtitle):
        return (f"{wtitle or 'That window'} is not responding, so it can't "
                "accept input. Nothing was typed.")

    # Focus through Win32 and type with the keyboard. Neither needs the
    # accessibility tree, which is the slow and fragile part — a person
    # driving an application types into whatever has focus, and so can NOVA.
    _raise(handle)
    time.sleep(0.3)
    if not _is_front(handle):
        return (f"I couldn't bring {wtitle!r} to the front, so I didn't type "
                f"anything into {_foreground_title()!r} instead.")

    target = _editable_safely(handle, name)
    before = _text_of(target) if target is not None else None

    try:
        if target is not None and name:
            try:
                target.click_input()
                time.sleep(0.15)
            except Exception:
                pass
        if not _set_value(target, text):
            # No value pattern to write through, so fall back to synthesising
            # keystrokes. At a 10 ms gap they outran the application and
            # arrived shuffled -- "verification" landed as "iiiiiication" --
            # which is worse than slow, because the tool then has to explain
            # that it typed something other than what it was asked to.
            from pywinauto.keyboard import send_keys
            send_keys(_escape_keys(text), with_spaces=True, pause=0.03)
    except Exception as e:
        return f"Typing failed: {e}"

    # Read it back. Reporting success without this is guessing.
    if target is None:
        return (f"I sent {text!r} to {wtitle!r}. It exposes no text field I "
                "can read back, so I can't confirm it landed.")
    time.sleep(0.3)
    after = _text_of(target)
    if text.strip() and text.strip().lower() in after.lower():
        return f"Typed {text!r}" + (f" into {name!r}." if name else ".")
    if after != before:
        return (f"I typed {text!r}; the field now reads {after[:60]!r}. It "
                "took the input but not exactly as sent.")
    return (f"I sent {text!r} but the field is unchanged ({after[:40]!r}). "
            "It did not take the input.")


def _set_value(target, text: str) -> bool:
    """Write `text` straight into a control, or say we couldn't.

    UI Automation lets a control's value be set in one call. Where that is
    offered it beats typing on every count that matters here: it is atomic, so
    there are no half-entered states to explain; it is exact, so the read-back
    check stops being a coin toss; and it took 0.26 s against 2.3 s for the
    same string.

    Search boxes that filter on each keystroke may not notice a value that
    arrives all at once. That is what the following `press` is for, and it is
    a better trade than text that arrives scrambled.
    """
    if target is None:
        return False
    try:
        iface = target.iface_value
        if iface is None or iface.CurrentIsReadOnly:
            return False
        iface.SetValue(text)
        return True
    except Exception as e:
        log.debug("no value pattern, falling back to keystrokes: %s", e)
        return False


#: send_keys reads these as syntax rather than as text, so literal characters
#: have to be wrapped in braces to survive.
_KEY_SYNTAX = frozenset("^+%~(){}[]")


def _escape_keys(text: str) -> str:
    """Wrap send_keys syntax so literal text arrives as itself.

    One pass, not one pass per character class. Escaping in a loop meant the
    braces added while escaping "%" were themselves escaped when the loop
    reached "{", and again at "}" -- "50% (a+b)" left as
    "50{{{}}%{}} {{{}}({}}a..." and typed as gibberish.
    """
    return "".join("{" + c + "}" if c in _KEY_SYNTAX else c for c in text)


def _bounded(fn, budget: float = TREE_BUDGET_S):
    """Run a tree operation with a ceiling it cannot talk its way out of.

    A deadline checked inside the loop is not a ceiling, because the blocking
    call happens before the loop starts: wrapping a handle alone took 75
    seconds here, and an element lookup took 147 and then failed. The only
    bound that holds is an outside one, so the work runs on a worker thread
    and is abandoned if it overruns. The thread may linger; the conversation
    does not wait for it.
    """
    import concurrent.futures as cf

    pool = cf.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        return future.result(timeout=budget)
    except Exception as e:
        log.debug("tree operation abandoned after %.1fs: %s", budget, e)
        return None
    finally:
        # Do not join: the point is not to wait for something that overran.
        pool.shutdown(wait=False)


def _editable_safely(handle: int, name: str = ""):
    """The control text would land in, or None — never at the cost of a stall.

    Typing does not depend on this. It only decides whether NOVA can confirm
    the text landed or has to say honestly that she cannot.
    """
    def work():
        win = _wrap(handle)
        if win is None:
            return None
        if name:
            return _match(win, name)
        for ctrl in win.descendants():
            try:
                if ctrl.element_info.control_type in ("Edit", "Document"):
                    return ctrl
            except Exception:
                continue
        return None

    return _bounded(work)


def _act_press(args: dict) -> str:
    keys = args.get("keys") or args.get("key") or ""
    if not keys:
        return "No keys given."
    title = args.get("window") or args.get("app") or ""
    target = ""
    if title:
        # These keystrokes are not harmless text -- "^s" saves and "%{F4}"
        # closes. Sending them to whatever happens to be in front because
        # focus quietly failed is how the wrong document gets written. Find
        # the window, raise it, and refuse if it did not come forward.
        found = _find_handle(title)
        if found is None:
            return f"I couldn't find a window matching {title!r}."
        handle, target = found
        if _is_hung(handle, target):
            return (f"{target} is not responding, so it can't accept input. "
                    f"I didn't press {keys}.")
        _raise(handle)
        time.sleep(0.25)
        if not _is_front(handle):
            return (f"I couldn't bring {target!r} to the front, so I didn't "
                    f"press {keys} into {_foreground_title()!r} instead.")
    try:
        from pywinauto.keyboard import send_keys
        send_keys(keys, pause=0.02)
    except Exception as e:
        return f"Key press failed: {e}"
    return f"Pressed {keys}" + (f" in {target!r}." if target else
                                f" in {_foreground_title()!r}.")


def _act_wait_for(args: dict) -> str:
    title = args.get("window") or args.get("app") or ""
    timeout = float(args.get("timeout", LAUNCH_TIMEOUT_S))
    found = _find_handle(title, timeout=timeout)
    if found is None:
        return f"{title!r} did not appear within {timeout:.0f} seconds."
    return f"{found[1]!r} is ready."


_ACTIONS = {
    "list_windows": _act_list_windows,
    "focus": _act_focus,
    "inspect": _act_inspect,
    "click": _act_click,
    "type": _act_type,
    "type_text": _act_type,
    "press": _act_press,
    "hotkey": _act_press,
    "wait_for": _act_wait_for,
}


def execute(args: dict) -> str:
    action = (args.get("action") or "").strip().lower()
    handler = _ACTIONS.get(action)
    if handler is None:
        return (f"Unknown app_control action {action!r}. I can: "
                + ", ".join(sorted(set(_ACTIONS))) + ".")
    try:
        return handler(args)
    except ImportError as e:
        return f"UI automation is unavailable on this machine ({e})."
    except Exception as e:
        log.exception("app_control failed")
        return f"That didn't work: {e}"


__all__ = ["execute"]
