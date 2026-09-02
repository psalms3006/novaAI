"""Quick smoke test for LiveManager API surface (no Gemini key needed)."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from desk.live_session import LiveManager, LiveState

def test_status_idle():
    mgr = LiveManager()
    s = mgr.status()
    assert s["state"] == "idle", f"expected idle, got {s['state']}"
    assert "model" in s and "voice" in s
    print("PASS: status idle")

def test_start_no_key():
    orig = os.environ.get("GEMINI_API_KEY")
    os.environ.pop("GEMINI_API_KEY", None)
    try:
        mgr = LiveManager()
        r = mgr.start()
        assert not r["ok"], f"expected ok=False, got {r}"
        assert "reason" in r
        print("PASS: start no key")
    finally:
        if orig:
            os.environ["GEMINI_API_KEY"] = orig

def test_subscribe_unsubscribe():
    mgr = LiveManager()
    q = mgr.subscribe()
    mgr.unsubscribe(q)
    mgr.unsubscribe(q)  # double unsubscribe is safe
    print("PASS: subscribe/unsubscribe")

def test_stop_idle():
    mgr = LiveManager()
    r = mgr.stop()
    assert r["ok"]
    print("PASS: stop idle")

def test_send_text_idle():
    mgr = LiveManager()
    r = mgr.send_text("hello")
    assert not r["ok"]
    print("PASS: send_text idle")

def test_bridge_routes():
    from desk.bridge import app
    rules = {r.rule for r in app.url_map.iter_rules()}
    for ep in ["/api/live/start", "/api/live/stop", "/api/live/status", "/api/live/ws"]:
        assert ep in rules, f"missing route: {ep}"
    print("PASS: bridge routes")

if __name__ == "__main__":
    test_status_idle()
    test_start_no_key()
    test_subscribe_unsubscribe()
    test_stop_idle()
    test_send_text_idle()
    test_bridge_routes()
    print("\nALL TESTS PASSED")
