"""Waiting, and knowing who you are talking to.

Two things NOVA did that no amount of model speed could fix.

She slept six seconds before every REST call. It was there to stay inside a
10 RPM free-tier quota, and it worked, but it charged the worst case to every
turn: asking the time could not be answered in under six seconds however
trivial the question. A 429 already triggers a real backoff window, wired into
every REST caller, so the standing delay was defending something already
defended.

And she greeted people as "User" — the shipped default, which arrives looking
exactly like an answer someone gave. Greeting a stranger by a placeholder is
worse than admitting you have not been introduced, so the placeholder now
means "ask".
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class RestPacingTests(unittest.TestCase):
    """No standing delay; spacing only once the quota has objected."""

    def setUp(self):
        import nova
        self.nova = nova
        self._saved = nova._MIN_GAP_BETWEEN_CALLS
        self.addCleanup(setattr, nova, "_MIN_GAP_BETWEEN_CALLS", self._saved)

    def test_a_healthy_quota_costs_no_waiting(self):
        self.nova._MIN_GAP_BETWEEN_CALLS = 0.0
        client = mock.Mock()
        with mock.patch("time.sleep") as slept:
            self.nova._gemini_generate_with_delay(client, model="m")
        slept.assert_not_called()
        client.models.generate_content.assert_called_once()

    def test_the_delay_is_off_until_something_is_refused(self):
        """The default is the common case, and the common case is fine."""
        import importlib
        nova = importlib.reload(self.nova)
        self.assertEqual(nova._MIN_GAP_BETWEEN_CALLS, 0.0)

    def test_a_429_turns_pacing_on(self):
        self.nova._MIN_GAP_BETWEEN_CALLS = 0.0
        self.nova._record_rate_limit()
        self.assertEqual(self.nova._MIN_GAP_BETWEEN_CALLS,
                         self.nova._RATE_LIMIT_GAP_S)

    def test_pacing_eases_off_as_calls_start_working(self):
        """One success is not proof the quota refilled, so decay rather than drop."""
        self.nova._record_rate_limit()
        first = self.nova._MIN_GAP_BETWEEN_CALLS
        self.assertGreater(first, 0)
        seen = []
        for _ in range(6):
            self.nova._reset_rate_limit()
            seen.append(self.nova._MIN_GAP_BETWEEN_CALLS)
        self.assertLess(seen[0], first)          # eased, not cleared
        self.assertEqual(seen[-1], 0.0)          # and fully recovered
        self.assertEqual(seen, sorted(seen, reverse=True))

    def test_a_paced_call_waits_only_the_remaining_gap(self):
        self.nova._MIN_GAP_BETWEEN_CALLS = 6.0
        self.nova._last_gemini_call = 0.0
        client = mock.Mock()
        with mock.patch("time.time", return_value=1000.0), \
             mock.patch("time.sleep") as slept:
            self.nova._last_gemini_call = 996.0   # 4 s ago
            self.nova._gemini_generate_with_delay(client, model="m")
        slept.assert_called_once()
        self.assertAlmostEqual(slept.call_args[0][0], 2.0, places=3)


class GreetingIdentityTests(unittest.TestCase):
    def test_a_real_name_is_used(self):
        from desk.live_session import _known_name
        self.assertEqual(_known_name({"user_name": "Ada"}), "Ada")

    def test_the_shipped_default_is_not_a_name(self):
        """"User" is what settings.py ships, not something a person chose."""
        from desk.live_session import _known_name
        for placeholder in ("User", "user", "  ", "", "there", None):
            with self.subTest(value=placeholder):
                self.assertEqual(_known_name({"user_name": placeholder}), "")

    def test_the_default_really_is_the_placeholder(self):
        """If settings ever ships a different default this test should fail."""
        from desk.settings import _DEFAULTS
        from desk.live_session import _PLACEHOLDER_NAMES
        self.assertIn(_DEFAULTS["user_name"].lower(), _PLACEHOLDER_NAMES)

    def test_a_pronunciation_can_be_stored(self):
        from desk.settings import _DEFAULTS
        self.assertIn("user_name_pronunciation", _DEFAULTS)


class ScreenBackpressureTests(unittest.TestCase):
    """The voice is the conversation; the screen is only context."""

    def test_a_frame_is_held_back_once_the_microphone_is_queueing(self):
        from desk.live_session import MIC_QUEUE_FRAMES
        src = (ROOT / "desk" / "live_session.py").read_text(encoding="utf-8")
        start = src.index("async def _video_sender")
        body = src[start:start + 3000]
        self.assertIn("MIC_QUEUE_FRAMES // 4", body,
                      "screen frames must yield to a backed-up mic queue")
        self.assertGreater(MIC_QUEUE_FRAMES // 4, 0)


class ConnectivityQuietTests(unittest.TestCase):
    def test_the_probe_stands_down_during_a_live_stream(self):
        from nova_intelligence.connectivity import ConnectivityManager
        mon = ConnectivityManager.__new__(ConnectivityManager)
        with mock.patch.object(ConnectivityManager, "_live_traffic", return_value=True):
            self.assertTrue(mon._live_traffic())

    def test_no_desk_layer_means_no_suppression(self):
        """Connectivity sits below the desk and must work without it."""
        from nova_intelligence.connectivity import ConnectivityManager
        mon = ConnectivityManager.__new__(ConnectivityManager)
        with mock.patch.dict(sys.modules, {"desk.live_session": None}):
            self.assertFalse(mon._live_traffic())


if __name__ == "__main__":
    unittest.main(verbosity=2)
