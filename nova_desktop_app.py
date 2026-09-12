#!/usr/bin/env python3
"""
nova_desktop_app.py — NOVA Desktop application shell.

STRATEGY: Start Flask server in ~2 seconds via run_desk_server().
Heavy nova.init happens lazily (tools, MCP, embeddings, planner).
Window shows immediately. UI connects as soon as server is up.

Run:          python nova_desktop_app.py
Headless:     NOVA_DESK_HEADLESS=1 python nova_desktop_app.py
Browser fallback: if pywebview is missing, opens the SPA in the default browser.
"""
import os
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if "--desk" not in sys.argv:
    sys.argv.append("--desk")

# TLS trust must be established before any HTTPS client exists in this process.
try:
    from nova_tls import ensure_tls_trust as _ensure_tls_trust
    _ensure_tls_trust()
except Exception:
    pass

PORT = int(os.getenv("NOVA_DESK_PORT", "") or 8765)
DESK_URL = f"http://127.0.0.1:{PORT}"
START_MODE = os.getenv("NOVA_DESK_MODE", "full")
_log_file = Path.home() / ".nova" / "desktop_startup.log"


def _log(msg: str):
    try:
        _log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


_LOADING_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',sans-serif;
       display:flex;flex-direction:column;align-items:center;justify-content:center;
       height:100vh;overflow:hidden}
  .ring{width:64px;height:64px;border:3px solid #1a2332;border-top:3px solid #58a6ff;
        border-radius:50%;animation:spin 1s linear infinite;margin-bottom:20px}
  @keyframes spin{to{transform:rotate(360deg)}}
  h1{font-size:28px;font-weight:300;letter-spacing:3px;margin-bottom:8px;color:#e6edf3}
  .status{font-size:13px;color:#8b949e;transition:opacity .3s}
  .ready{color:#3fb950}
</style></head><body>
  <div class="ring"></div>
  <h1>NOVA</h1>
  <div class="status" id="status">Starting...</div>
  <script>
    var attempts=0;
    var statusEl=document.getElementById('status');
    (function poll(){
      attempts++;
      statusEl.textContent='Starting NOVA... ('+attempts+')';
      fetch('/api/health',{signal:AbortSignal.timeout(2000)})
        .then(function(r){ return r.json(); })
        .then(function(j){
          if(j && (j.ready || j.brain_ready)){
            statusEl.textContent='Ready!';
            statusEl.className='status ready';
            setTimeout(function(){window.location.href='/'},300);
          }else{setTimeout(poll,500)}
        })
        .catch(function(){setTimeout(poll,500)});
    })();
  </script>
</body></html>"""


def main() -> int:
    _log("=" * 60)
    _log("NOVA Desktop starting up")
    _log(f"START_MODE={START_MODE}")
    _log(f"DESK_URL={DESK_URL}")
    _log(f"Python {sys.version}")

    try:
        from desk.creds import bootstrap as _creds_bootstrap
        _creds_bootstrap()
        _log("Credentials bootstrapped")
    except Exception as e:
        _log(f"Credentials bootstrap: {e}")

    if os.environ.get("NOVA_DESK_HEADLESS") == "1":
        _log("Running headless")
        try:
            import nova
            nova.main()
        except Exception as e:
            _log(f"FATAL: {e}\n{traceback.format_exc()}")
            return 1
        return 0

    # ── Step 1: Start Flask server IMMEDIATELY (fast, ~2s) ─────────────
    def _start_server():
        try:
            _log("Starting Flask server via run_desk_server()")
            from desk.bridge import run_desk_server
            run_desk_server({})
        except Exception as e:
            _log(f"CRITICAL: Server failed: {e}\n{traceback.format_exc()}")

    server_thread = threading.Thread(target=_start_server, name="NOVAServer", daemon=True)
    server_thread.start()
    _log("Server thread started")

    # ── Step 1b: Initialize AI brain in background ─────────────────────
    # nova.main() does ALL heavy init (tools, MCP, embeddings, memory, planner, router)
    # then calls run_desk_server() which would duplicate our server.
    # We monkey-patch run_desk_server to a no-op so nova.main() only does init.
    def _init_nova_brain():
        try:
            _log("Brain: importing nova module...")
            import nova as nova_mod
            _orig_run_desk = getattr(nova_mod, 'run_desk_server', None)
            if callable(_orig_run_desk):
                nova_mod.run_desk_server = lambda *a, **kw: None
            _log("Brain: calling nova.main() (this takes ~25s for Ollama+embeddings)...")
            try:
                nova_mod.main()
            finally:
                if callable(_orig_run_desk):
                    nova_mod.run_desk_server = _orig_run_desk
            _log("Brain: nova.main() returned — init complete")
            # Diagnostic: check if router was actually set
            _nr = getattr(nova_mod, '_nova_router', None)
            _nr_type = type(_nr).__name__ if _nr else "None"
            _log(f"Brain: nova._nova_router = {_nr_type} ({_nr})")
            # Set flag so bridge knows brain is ready
            try:
                import desk.bridge as _br
                if hasattr(_br, '_brain_ready'):
                    _br._brain_ready = True
                _log("Brain: set _brain_ready flag in bridge")
            except Exception as fe:
                _log(f"Brain: could not set bridge flag: {fe}")
        except Exception as e:
            _log(f"Brain init FAILED: {e}\n{traceback.format_exc()}")

    brain_thread = threading.Thread(target=_init_nova_brain, name="NOVABrain", daemon=True)
    brain_thread.start()
    _log("Brain init thread started")

    # ── Step 2: Create window IMMEDIATELY ──────────────────────────────
    try:
        import webview
        _log(f"pywebview imported")
    except Exception as e:
        _log(f"pywebview not available: {e}")
        print("Opening NOVA in default browser...")
        webbrowser.open(f"{DESK_URL}/")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
        return 0

    ambient_overlay = None
    try:
        from desk.win_overlay import AmbientOverlay, FullWindowManager
        ambient_overlay = AmbientOverlay(width=120, height=120)
        _log("AmbientOverlay imported")
    except Exception as e:
        _log(f"Ambient overlay import failed: {e}")

    actual_mode = START_MODE
    if START_MODE == "ambient":
        if not ambient_overlay or not ambient_overlay.is_available():
            _log("Ambient mode not available — falling back to FULL")
            actual_mode = "full"

    _log(f"Actual launch mode: {actual_mode}")

    _log("Creating loading window")
    loading_window = webview.create_window(
        "NOVA",
        html=_LOADING_HTML,
        width=1280,
        height=840,
        min_size=(960, 620),
        background_color="#0d1117",
    )
    _log("Loading window created")

    def _on_backend_ready():
        _log("Backend ready — navigating to live UI")
        try:
            loading_window.load_url(f"{DESK_URL}/")
        except Exception as e:
            _log(f"load_url failed: {e}")

    def _wait_for_backend():
        import requests
        deadline = time.time() + 180
        _log(f"Waiting for backend at {DESK_URL}")
        while time.time() < deadline:
            try:
                r = requests.get(f"{DESK_URL}/api/health", timeout=2)
                if r.status_code < 500:
                    try:
                        payload = r.json()
                    except Exception:
                        payload = {}
                    # Wait until the NOVA brain (router) is actually ready, not just
                    # the HTTP server. This prevents the user from typing before the
                    # intelligence pipeline has finished initialising.
                    if payload.get("ready") or payload.get("brain_ready"):
                        _log(f"Backend ready (status={r.status_code}, ready=true)")
                        _on_backend_ready()
                        return True
                    else:
                        _log("Backend up but brain still initialising — waiting...")
            except Exception:
                pass
            time.sleep(0.5)
        _log("Backend not ready within deadline — navigating anyway")
        _on_backend_ready()
        return False

    # ── ambient presence ──────────────────────────────────────────────
    #
    # NOVA has one runtime and two presentation surfaces. The ambient orb is
    # what NOVA looks like when her main interface is NOT the thing the user is
    # looking at — never a second app, and never shown alongside the main
    # window.
    #
    # Visibility is owned by exactly one place: _ambient_watch() below, driven
    # by real Win32 window state. Nothing else calls show()/hide() on it, so
    # "main visible => ambient off" cannot drift out of sync.
    #
    # Transparency: pywebview's transparent=True throws inside
    # InitCoreWebView2Async on this backend (verified in isolation), so instead
    # the window is made layered and colour-keyed on pure black after creation.
    # The page paints the orb additively on #000, and every black pixel is
    # punched out by the compositor — a genuinely backgroundless orb.
    #
    # desk.win_overlay.AmbientOverlay is NOT used: it creates a layered Win32
    # window with no WM_PAINT handler and never draws anything.

    # Ambient is a bead, not a bar.
    #
    # It briefly became a 460x104 panel with the transcript beside the orb,
    # and that is a miniature application window sitting on the desktop — the
    # opposite of ambient. What belongs here is presence: a small orb the user
    # can see, drag and click, and nothing else. The transcript lives in the
    # full interface, where there is room for it.
    #
    # 44px is sized against the close button of an ordinary window — a little
    # larger, so it is comfortably clickable and its state is readable at a
    # glance, while still small enough to leave on top of real work.
    #
    # pywebview does not honour small sizes exactly (it reported 200x100 for a
    # 96px request), so the real bounds are forced with SetWindowPos once the
    # HWND exists.
    AMBIENT_PX = 44
    AMBIENT_W = AMBIENT_H = AMBIENT_PX

    ambient_window = None
    try:
        ambient_window = webview.create_window(
            "NOVA_AMBIENT",                      # distinct title so we can find the HWND
            url=f"{DESK_URL}/?mode=ambient",
            width=AMBIENT_W, height=AMBIENT_H,
            # pywebview defaults min_size to (200,100), which silently floors
            # the window and clipped the orb — the requested 72px was ignored
            # and SetWindowPos could move it but never shrink it.
            min_size=(1, 1),
            frameless=True,
            easy_drag=True,
            on_top=True,
            resizable=False,
            hidden=True,
            background_color="#000000",          # the colour-key
        )
        _log(f"Ambient window created ({AMBIENT_W}x{AMBIENT_H}, hidden)")
    except Exception as e:
        _log(f"Ambient window unavailable: {e}")

    _amb_pos_file = Path.home() / ".nova" / "ambient_pos.json"

    def _load_amb_pos():
        try:
            import json
            d = json.loads(_amb_pos_file.read_text(encoding="utf-8"))
            return int(d["x"]), int(d["y"])
        except Exception:
            return None

    def _save_amb_pos(x, y):
        try:
            import json
            _amb_pos_file.parent.mkdir(parents=True, exist_ok=True)
            _amb_pos_file.write_text(json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
        except Exception:
            pass

    def _clamp_to_desktop(x, y, w, h):
        """Keep the orb reachable across monitor/resolution changes.

        Uses the *virtual* screen (all monitors) rather than the primary one, so
        a position saved on an external display is still valid, and a position
        on a monitor that has since been unplugged is pulled back into view.
        """
        try:
            import ctypes
            u = ctypes.windll.user32
            vx = u.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            vy = u.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
            vw = u.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
            vh = u.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
            x = max(vx, min(int(x), vx + vw - w))
            y = max(vy, min(int(y), vy + vh - h))
        except Exception:
            pass
        return int(x), int(y)

    def _ambient_target_xy():
        """Saved position if it is still on a connected display, else default."""
        try:
            import ctypes
            u = ctypes.windll.user32
            saved = _load_amb_pos()
            if saved is None:
                sw = u.GetSystemMetrics(0)
                x, y = sw - AMBIENT_W - 48, 64
            else:
                x, y = saved
            return _clamp_to_desktop(x, y, AMBIENT_W, AMBIENT_H)
        except Exception:
            return 64, 64

    def _place_ambient(hwnd):
        """Force the true window bounds and restore the user's chosen position."""
        try:
            import ctypes
            u = ctypes.windll.user32
            saved = _load_amb_pos()
            if saved is None:
                # Default: top-right, inset from the edge, on the primary display.
                sw = u.GetSystemMetrics(0)
                x, y = sw - AMBIENT_W - 48, 64
            else:
                x, y = saved
            x, y = _clamp_to_desktop(x, y, AMBIENT_W, AMBIENT_H)
            HWND_TOPMOST, SWP_NOACTIVATE = -1, 0x0010
            u.SetWindowPos(hwnd, HWND_TOPMOST, x, y, AMBIENT_W, AMBIENT_H, SWP_NOACTIVATE)
            _log(f"Ambient placed at ({x},{y}) {AMBIENT_W}x{AMBIENT_H}")
        except Exception as e:
            _log(f"Ambient placement failed: {e}")

    def _remember_ambient_pos(hwnd):
        try:
            import ctypes

            class _R(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                            ("r", ctypes.c_long), ("b", ctypes.c_long)]

            r = _R()
            if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
                _save_amb_pos(r.l, r.t)
        except Exception:
            pass

    def _round_window(hwnd, px) -> bool:
        """Clip the ambient window to a circle.

        WebView2 renders through DirectComposition, which bypasses layered-
        window colour keying — SetLayeredWindowAttributes(LWA_COLORKEY) reports
        success but the black corners still paint (measured: corner (0,0,0)
        against a (243,243,243) backdrop). pywebview's transparent=True also
        throws on this backend.

        SetWindowRgn works at the window-manager level, so the compositor
        cannot ignore it: the corners stop being part of the window at all.
        They are not drawn, and clicks there fall through to whatever is
        underneath — which is also the click-through behaviour we want.
        """
        try:
            import ctypes
            u, g = ctypes.windll.user32, ctypes.windll.gdi32
            rgn = g.CreateEllipticRgn(0, 0, px + 1, px + 1)
            ok = u.SetWindowRgn(hwnd, rgn, True)
            _log(f"Ambient clipped to circle ({px}px): {bool(ok)}")
            return bool(ok)
        except Exception as e:
            _log(f"Ambient clip failed: {e}")
            return False

    def _make_transparent(hwnd) -> bool:
        """Punch the black background out of the ambient window."""
        try:
            import ctypes
            u = ctypes.windll.user32
            GWL_EXSTYLE, WS_EX_LAYERED, LWA_COLORKEY = -20, 0x00080000, 0x00000001
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
            # COLORREF is 0x00BBGGRR; pure black.
            ok = u.SetLayeredWindowAttributes(hwnd, 0x000000, 255, LWA_COLORKEY)
            # Colour-keyed pixels are also click-through: Windows routes
            # hit-testing past them, so the invisible area around the orb
            # does not block the app underneath.
            _log(f"Ambient colour-key applied to hwnd={hwnd}: {bool(ok)}")
            return bool(ok)
        except Exception as e:
            _log(f"Ambient colour-key failed: {e}")
            return False

    def _find_hwnd(title: str):
        try:
            import ctypes
            return ctypes.windll.user32.FindWindowW(None, title)
        except Exception:
            return 0

    # ── the one authority for ambient visibility ──────────────────────
    def _set_screen_watching(on: bool) -> None:
        """Turn NOVA's screen awareness on with ambient mode, off with it.

        Ambient is a presence over the desktop, so what is on the desktop is
        part of what she is aware of — the user asked for that by switching to
        it, and a button would only be a second way to say the same thing. The
        full interface is the opposite: she looks when asked, and not before.
        """
        try:
            import json as _json
            import urllib.request

            from desk import bridge as _bridge
            req = urllib.request.Request(
                f"{DESK_URL}/api/live/screen",
                data=_json.dumps({"watching": bool(on)}).encode(),
                headers={"Content-Type": "application/json",
                         "X-NOVA-Desk": _bridge.run_token},
                method="POST")
            with urllib.request.urlopen(req, timeout=3) as r:
                r.read()
        except Exception as e:
            # Vision is an enhancement; never let it disturb the window logic.
            _log(f"screen awareness toggle failed: {e}")

    def _ambient_watch():
        """Show the orb only while the main window is not the user's view of NOVA.

        MAIN visible+foreground        -> ambient OFF
        MAIN minimised                 -> ambient ON
        MAIN covered by another app    -> ambient ON
        MAIN restored/foregrounded     -> ambient OFF
        """
        import ctypes
        u = ctypes.windll.user32
        main_hwnd = 0
        amb_hwnd = 0
        keyed = False
        shown = None                       # tri-state so the first decision always applies
        main_was_foreground = False        # see below
        pending = None
        pending_since = 0.0
        DEBOUNCE_S = 0.6

        while True:
            time.sleep(0.4)
            try:
                if not main_hwnd:
                    main_hwnd = _find_hwnd("NOVA")
                if not amb_hwnd:
                    amb_hwnd = _find_hwnd("NOVA_AMBIENT")
                    if amb_hwnd and not keyed:
                        keyed = _make_transparent(amb_hwnd)
                if not main_hwnd:
                    continue

                minimised = bool(u.IsIconic(main_hwnd))
                fg = u.GetForegroundWindow()
                # Either NOVA window being foreground counts as "NOVA is present".
                nova_fg = fg in (main_hwnd, amb_hwnd) if amb_hwnd else (fg == main_hwnd)

                # Never show the orb on a fresh launch. Arm only once the main
                # window has actually been put on screen.
                #
                # This deliberately keys off *visible and not minimised*, not
                # *foreground*: if the user clicks another app while NOVA is
                # still loading, NOVA may never take focus, and keying off
                # foreground left the orb disabled for the whole session.
                if not main_was_foreground:
                    if u.IsWindowVisible(main_hwnd) and not minimised:
                        main_was_foreground = True
                        _log("Main window presented — ambient watch armed")
                    continue

                want = minimised or not nova_fg

                # Debounce. Focus moves constantly while the user works, and
                # toggling a topmost window on every transient change makes the
                # orb flicker. Require the condition to hold before acting.
                if want != pending:
                    pending = want
                    pending_since = time.time()
                stable = (time.time() - pending_since) >= DEBOUNCE_S

                if want != shown and stable:
                    shown = want
                    if ambient_window is None:
                        continue
                    if want:
                        _log("Ambient ON (main minimised=%s foreground=%s)" % (minimised, nova_fg))
                        _set_screen_watching(True)
                        ambient_window.show()
                        # Geometry and the layered style must be re-asserted
                        # AFTER show(): pywebview re-applies its own window size
                        # on show, which overrode the 72px bounds and left the
                        # colour-key stale (the orb came back as a black bar).
                        # Size/position go through pywebview's own API, not
                        # SetWindowPos: the WinForms backend re-lays-out the
                        # Form and silently overrode raw Win32 geometry (the
                        # window kept snapping back to 120x33 and clipped the
                        # orb). The colour-key still has to be re-asserted on
                        # the HWND after show().
                        try:
                            x, y = _ambient_target_xy()
                            time.sleep(0.12)
                            ambient_window.resize(AMBIENT_W, AMBIENT_H)
                            ambient_window.move(x, y)
                            _log(f"Ambient sized {AMBIENT_W}x{AMBIENT_H} at ({x},{y})")
                        except Exception as e:
                            _log(f"Ambient sizing failed: {e}")
                        if amb_hwnd:
                            for _ in range(4):
                                time.sleep(0.10)
                                keyed = _make_transparent(amb_hwnd)
                                _round_window(amb_hwnd, AMBIENT_PX)
                    else:
                        _log("Ambient OFF (main window is visible)")
                        # Back in the full interface, NOVA stops watching the
                        # screen. Ambient mode is her looking over the desktop;
                        # the full window is the user looking at her, and she
                        # should only look when asked.
                        _set_screen_watching(False)
                        if amb_hwnd:
                            _remember_ambient_pos(amb_hwnd)   # user may have dragged it
                        ambient_window.hide()
            except Exception as e:
                _log(f"Ambient watch error: {e}")

    threading.Thread(target=_ambient_watch, name="AmbientWatch", daemon=True).start()

    # The bridge asks for a *mode*; the watcher decides what is on screen. So
    # entering ambient just means "get the main window out of the way".
    def _enter_ambient():
        _log("Ambient requested: minimising main window")
        loading_window.minimize()

    def _exit_ambient():
        _log("Full requested: restoring main window")
        loading_window.restore()

    def _register_hooks():
        # The bridge is imported on the server thread; wait for it, then hand
        # over real window control.
        for _ in range(120):
            try:
                import desk.bridge as _br
                _br.register_ambient_hooks(enter=_enter_ambient, exit=_exit_ambient)
                _log("Ambient hooks registered with bridge")
                return
            except Exception:
                time.sleep(0.5)
        _log("Could not register ambient hooks")

    threading.Thread(target=_register_hooks, daemon=True).start()

    wait_thread = threading.Thread(target=_wait_for_backend, daemon=True)
    wait_thread.start()

    webview.start()

    if ambient_overlay:
        ambient_overlay.destroy()

    try:
        from desk.bridge import shutdown_desk_server
        shutdown_desk_server()
        _log("Desk server shut down")
    except Exception as e:
        _log(f"Desk server shutdown failed: {e}")

    time.sleep(0.6)
    _log("NOVA Desktop exiting")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        _log(f"FATAL UNHANDLED: {e}")
        _log(traceback.format_exc())
        sys.exit(1)
