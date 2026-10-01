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
# Started with Windows as "background" (desk.startup): the orb only.
if "--background" in sys.argv:
    START_MODE = "ambient"
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
          if(j && (j.ready || j.brain_ready || j.auth_required)){
            statusEl.textContent='Ready!';
            statusEl.className='status ready';
            setTimeout(function(){window.location.href='/'},300);
          }else{setTimeout(poll,500)}
        })
        .catch(function(){setTimeout(poll,500)});
    })();
  </script>
</body></html>"""


def _claim_single_instance():
    """Refuse to start a second NOVA, and say why.

    Nothing stopped two from running. The second loses the port, so it has no
    window and looks like it simply failed to launch — but it still opens the
    microphone and still streams audio to the model. Two copies then fight
    over one microphone and share one uplink, which is enough on a modest
    connection to start dropping the user's speech: measured here with several
    stacked instances, mic sends reaching 21 seconds and 555 frames of speech
    discarded, against zero of either once a single instance was running.

    A named mutex is the Windows way to ask "is one already running?" and the
    kernel releases it when the process dies, so a crash cannot leave NOVA
    permanently unable to start. Returns a handle to keep alive, or None when
    the check could not be made -- never blocking startup over its own
    failure.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        ERROR_ALREADY_EXISTS = 183
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        handle = k32.CreateMutexW(None, False, "Global\\NOVA.Desktop.SingleInstance")
        # GetLastError must be read straight after the call that set it.
        already = (k32.GetLastError() == ERROR_ALREADY_EXISTS)
        if already:
            if handle:
                k32.CloseHandle(handle)
            return False
        return handle
    except Exception as e:
        _log(f"single-instance check unavailable: {e}")
        return None


def _arg_after(flag: str) -> str:
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


def _health_check_only() -> int:
    """Run by the update helper after installing a new version: prove this
    build starts (imports load, the server answers) without opening a window,
    touching an account, or starting the brain. The helper restores the
    previous version if the marker never appears."""
    import requests
    version = _arg_after("--post-update")
    _log(f"Health check for {version or '?'} on {DESK_URL}")

    def _serve():
        from desk.bridge import run_desk_server
        run_desk_server({})
    threading.Thread(target=_serve, name="NOVAHealth", daemon=True).start()
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if requests.get(f"{DESK_URL}/api/health", timeout=2).status_code == 200:
                from desk import updater
                updater.mark_healthy(version)
                _log("Health check passed")
                return 0
        except Exception:
            pass
        time.sleep(0.5)
    _log("Health check failed: the server never answered")
    return 1


def main() -> int:
    if "--health-check-only" in sys.argv:
        return _health_check_only()
    _log("=" * 60)
    _log("NOVA Desktop starting up")
    _log(f"START_MODE={START_MODE}")
    _log(f"DESK_URL={DESK_URL}")
    _log(f"Python {sys.version}")

    _instance_lock = _claim_single_instance()
    if _instance_lock is False and "--after-restart" in sys.argv:
        # A deliberate restart (sign-out, account switch, update): the old
        # process is exiting and will release the lock in a moment.
        deadline = time.time() + 20
        while _instance_lock is False and time.time() < deadline:
            time.sleep(0.25)
            _instance_lock = _claim_single_instance()
    if _instance_lock is False:
        _log("Another NOVA is already running — not starting a second one.")
        print("NOVA is already running. Look for the orb, or close the other "
              "copy first.\n(Two copies share one microphone and one "
              "connection, which stops NOVA hearing you.)")
        return 0
    # Held for the life of the process; released by the kernel on exit.
    globals()["_INSTANCE_LOCK"] = _instance_lock

    # What happened to the last update (installed / rolled back), recorded
    # and reported before anything else can overwrite it.
    try:
        from desk import updater as _updater
        _res = _updater.collect_result()
        if _res:
            _log(f"Last update: {_res.get('from')} -> {_res.get('version')}: {_res.get('result')}"
                 + (f" ({_res.get('error')})" if _res.get('error') else ""))
    except Exception as e:
        _log(f"Update result check failed: {e}")

    # ── Who is this, and have they been here before? ─────────────────
    # Before credentials and before the brain: both read the signed-in
    # account's folder, which only exists once the account is known.
    import nova_lifecycle
    import nova_runtime
    from nova_version import APP_VERSION
    nova_runtime.set_logger(_log)
    first_run = nova_lifecycle.is_first_run()
    nova_lifecycle.ensure_installation(APP_VERSION)
    os.environ["NOVA_FIRST_RUN"] = "1" if first_run else "0"
    auth_gate = False
    try:
        import nova_account
        acct = nova_account.account()
        if acct.configured:
            if acct.signed_in and acct.user_id:
                info = nova_lifecycle.activate_account(acct.user_id)
                _log(f"Account {acct.user_id[:8]}… active"
                     + (f"; adopted {len(info['adopted'])} existing item(s)" if info["adopted"] else ""))
            else:
                # Sign-in is required on a build with an account server. The
                # window opens on sign-in; the brain starts after it.
                auth_gate = True
                _log("No signed-in account: waiting for sign-in before starting the brain")
        else:
            _log("No NOVA account server configured: running locally without sign-in")
    except Exception as e:
        _log(f"Account check failed ({e}); continuing without an account")
    os.environ["NOVA_AUTH_GATE"] = "1" if auth_gate else "0"
    _log(f"Lifecycle: first_run={first_run} version={APP_VERSION} gate={auth_gate}")

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

    # ── Step 1b: the brain, unless sign-in has to come first ─────────
    if auth_gate:
        _log("Brain deferred until sign-in")
    else:
        nova_runtime.start_brain()

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
                    if (payload.get("ready") or payload.get("brain_ready")
                            or payload.get("auth_required")):
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
    # Shape: a WebView2 window cannot be see-through here (transparent=True
    # painted an opaque square; a colour key never reached WebView2 and also
    # swallowed mouse input), so the orb is drawn natively with per-pixel
    # alpha -- desk/ambient_native.py.
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

    # The orb is thinking-orbs drawn natively into a layered window
    # (desk/ambient_native.py): the desktop shows through between its dots.
    # A WebView2 page cannot be transparent here -- measured, it painted an
    # opaque square -- so the ambient orb is no longer a web page.
    def _orb_dark() -> bool:
        try:
            from desk import settings as _ds
            return (_ds.get("ui_prefs") or {}).get("theme_id", "obsidian") != "pearl"
        except Exception:
            return True

    def _orb_clicked():
        # A click is "bring her back", always. Asking the bridge alone did
        # nothing when the person had minimised the window themselves: the
        # bridge still thought the mode was "full", saw no change, and never
        # ran its restore hook (the old web orb had the same dead click).
        try:
            loading_window.restore()
            try:
                import ctypes
                hwnd = ctypes.windll.user32.FindWindowW(None, "NOVA")
                if hwnd:
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            _log("Ambient orb clicked: main window restored")
        except Exception as e:
            _log(f"ambient click restore failed: {e}")
        # ...and keep the bridge's idea of the mode right.
        try:
            import json as _json
            import urllib.request
            from desk import bridge as _bridge
            req = urllib.request.Request(
                f"{DESK_URL}/api/ambient", data=_json.dumps({"mode": "full"}).encode(),
                headers={"Content-Type": "application/json", "X-NOVA-Desk": _bridge.run_token},
                method="POST")
            with urllib.request.urlopen(req, timeout=3) as r:
                r.read()
        except Exception as e:
            _log(f"ambient click: bridge not told ({e})")

    ambient_window = None
    try:
        from desk.ambient_native import AmbientOrbWindow, OrbStateTracker, attach_live_events
        _orb_tracker = OrbStateTracker()
        attach_live_events(_orb_tracker)

        def _hook_bus():
            for _ in range(240):
                try:
                    import desk.bridge as _br
                    _br.add_event_listener(_orb_tracker.on_bus)
                    return
                except Exception:
                    time.sleep(0.5)
        threading.Thread(target=_hook_bus, daemon=True).start()
        ambient_window = AmbientOrbWindow(AMBIENT_PX, _orb_tracker, on_click=_orb_clicked, dark=_orb_dark)
        if not ambient_window.hwnd:
            raise RuntimeError("the window could not be created")
        _log(f"Ambient orb created ({AMBIENT_PX}px, native, see-through, hidden)")
    except Exception as e:
        ambient_window = None
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
                data=_json.dumps({"watching": bool(on), "source": "ambient"}).encode(),
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
                        # The native orb is exactly its size and has no
                        # corners to clip: place it, then show it.
                        try:
                            x, y = _ambient_target_xy()
                            ambient_window.move(x, y)
                            _log(f"Ambient at ({x},{y}) {AMBIENT_PX}px")
                        except Exception as e:
                            _log(f"Ambient placement failed: {e}")
                        ambient_window.show()
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

    # Automatic updates: checked in the background, applied when NOVA quits.
    try:
        from desk import updater as _updater

        def _required(version):
            try:
                import desk.bridge as _br
                _br.publish_event({"type": "update.required", "version": version,
                                   "message": "This version of NOVA is no longer supported. "
                                              "Updating now…"})
            except Exception:
                pass
            time.sleep(4)          # long enough for the window to say why
        _updater.start_background(on_required=_required)
    except Exception as e:
        _log(f"Updater not started: {e}")

    webview.start()

    try:
        from desk import updater as _updater
        if _updater.staged():
            _log("Update staged: handing it to the installer helper on quit")
            _updater.apply_staged(relaunch=False)
    except Exception as e:
        _log(f"Update on quit failed to start: {e}")

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
