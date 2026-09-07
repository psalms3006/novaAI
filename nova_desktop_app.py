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
