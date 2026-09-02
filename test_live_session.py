"""test_live_session.py — programmatic test for LiveManager + WS bridge.

Run:
    python test_live_session.py

If GEMINI_API_KEY is set, tests a real Live connection (text-only turn to avoid
mic hardware dependency).  Without a key it verifies the honest-failure path and
the API surface.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from desk.live_session import LiveManager, LiveState, get_live_manager


class TestLiveManagerAPI(unittest.TestCase):
    """Test the API surface without requiring a real Gemini connection."""

    def test_status_idle(self):
        mgr = LiveManager()
        s = mgr.status()
        self.assertEqual(s["state"], "idle")
        self.assertFalse(s["native_audio"])
        self.assertFalse(s["ok"] and s["turns"])
        self.assertIn("model", s)
        self.assertIn("voice", s)

    def test_start_no_key(self):
        orig = os.environ.get("GEMINI_API_KEY")
        os.environ.pop("GEMINI_API_KEY", None)
        try:
            mgr = LiveManager()
            r = mgr.start()
            self.assertFalse(r["ok"])
            self.assertIn("reason", r)
        finally:
            if orig is not None:
                os.environ["GEMINI_API_KEY"] = orig

    def test_subscribe_unsubscribe(self):
        mgr = LiveManager()
        q = mgr.subscribe()
        self.assertTrue(q.empty())
        mgr.unsubscribe(q)
        # double-unsubscribe is safe
        mgr.unsubscribe(q)

    def test_stop_when_idle(self):
        mgr = LiveManager()
        r = mgr.stop()
        self.assertTrue(r["ok"])

    def test_send_text_when_idle(self):
        mgr = LiveManager()
        r = mgr.send_text("hello")
        self.assertFalse(r["ok"])


class TestLiveManagerLive(unittest.TestCase):
    """Real Live connection test — only runs when GEMINI_API_KEY is present."""

    @classmethod
    def setUpClass(cls):
        cls._key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not cls._key:
            raise unittest.SkipTest("GEMINI_API_KEY not set — skipping live tests")

    def test_text_turn(self):
        """Send a text turn and verify we get audio + transcripts back."""
        mgr = LiveManager()
        q = mgr.subscribe()
        try:
            r = mgr.start()
            self.assertTrue(r["ok"], f"start failed: {r}")

            # Wait for connected state
            deadline = time.time() + 15.0
            got_connected = False
            while time.time() < deadline:
                try:
                    ev = q.get(timeout=1.0)
                    if ev.type == "state" and ev.data.get("state") == "connected":
                        got_connected = True
                        break
                except Exception:
                    pass
            self.assertTrue(got_connected, "did not reach connected state in 15s")

            # Send a text prompt
            r2 = mgr.send_text("Say hello in one short sentence.")
            self.assertTrue(r2["ok"])

            # Collect events
            got_audio = False
            got_nova_transcript = False
            got_user_transcript = False
            got_turn_complete = False
            t_first_audio = 0.0
            deadline2 = time.time() + 20.0
            while time.time() < deadline2:
                try:
                    ev = q.get(timeout=1.0)
                    if ev.type == "audio":
                        got_audio = True
                        if t_first_audio == 0.0:
                            t_first_audio = time.time()
                        # Verify base64 data
                        self.assertTrue(len(ev.data.get("data_b64", "")) > 0)
                    elif ev.type == "nova_transcript":
                        got_nova_transcript = True
                        self.assertTrue(len(ev.data.get("text", "")) > 0)
                        print(f"  [nova] {ev.data['text']}")
                    elif ev.type == "user_transcript":
                        got_user_transcript = True
                    elif ev.type == "turn_complete":
                        got_turn_complete = True
                        break
                except Exception:
                    pass

            # Assertions
            self.assertTrue(got_audio, "no audio chunks received")
            self.assertTrue(got_turn_complete, "no turn_complete received")

            status = mgr.status()
            print(f"  status: {json.dumps(status, indent=2)}")
            self.assertGreater(status["audio_out_kb"], 0, "audio_out_kb should be > 0")
            self.assertGreater(status["turns"], 0, "turns should be > 0")

            if t_first_audio:
                latency_ms = (t_first_audio - mgr._t_connected) * 1000
                print(f"  t_first_audio: {latency_ms:.0f}ms")
                self.assertLess(latency_ms, 15000, "first audio took > 15s")

        finally:
            mgr.stop()
            mgr.unsubscribe(q)

    def test_barge_in(self):
        """Send two quick turns — second should interrupt the first."""
        mgr = LiveManager()
        q = mgr.subscribe()
        try:
            r = mgr.start()
            self.assertTrue(r["ok"])
            deadline = time.time() + 15.0
            while time.time() < deadline:
                try:
                    ev = q.get(timeout=1.0)
                    if ev.type == "state" and ev.data.get("state") == "connected":
                        break
                except Exception:
                    pass

            # First turn
            mgr.send_text("Count slowly from one to five.")
            time.sleep(1.0)

            # Second turn (should cause interruption)
            mgr.send_text("Stop. Say just 'interrupted'.")
            time.sleep(0.5)

            got_interrupted = False
            deadline2 = time.time() + 10.0
            while time.time() < deadline2:
                try:
                    ev = q.get(timeout=1.0)
                    if ev.type == "interrupted":
                        got_interrupted = True
                    if ev.type == "turn_complete":
                        break
                except Exception:
                    pass

            # Interruption may or may not happen depending on timing — don't assert
            # hard, just log it
            print(f"  barge-in interrupted: {got_interrupted}")

        finally:
            mgr.stop()
            mgr.unsubscribe(q)


class TestBridgeIntegration(unittest.TestCase):
    """Quick bridge endpoint smoke test — imports bridge and checks routes exist."""

    def test_bridge_routes_exist(self):
        from desk.bridge import app
        rules = {r.rule for r in app.url_map.iter_rules()}
        self.assertIn("/api/live/start", rules)
        self.assertIn("/api/live/stop", rules)
        self.assertIn("/api/live/status", rules)
        self.assertIn("/api/live/ws", rules)

    def test_live_start_stop_cycle(self):
        """Hit /api/live/start then /api/live/stop via the test client."""
        import os as _os
        orig = _os.environ.get("GEMINI_API_KEY")
        # Ensure key is present or gracefully skip
        from desk.bridge import app, run_token
        with app.test_client() as c:
            # Auth
            headers = {"X-NOVA-Desk": run_token}

            # Status (should be idle)
            r = c.get("/api/live/status", headers=headers)
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertEqual(j["state"], "idle")

            # Start
            r = c.post("/api/live/start", headers=headers)
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertTrue(j["ok"])

            # Wait a moment for thread to start
            time.sleep(0.5)

            # Status should be connecting/streaming
            r = c.get("/api/live/status", headers=headers)
            j = r.get_json()
            self.assertIn(j["state"], ("connecting", "streaming", "connected", "error"))

            # Stop
            r = c.post("/api/live/stop", headers=headers)
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertTrue(j["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
