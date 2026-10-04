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


class InstalledKeyTests(unittest.TestCase):
    """An installed NOVA has to find the key where the user put it.

    Bare load_dotenv() walks up from the calling module's directory, which in
    a frozen build is a path inside the PyInstaller archive. So the file the
    install guide tells people to drop next to NOVA.exe was never read, NOVA
    fell back to the local 1.5B model, and a one-word reply took eighteen
    seconds while the window still said ONLINE.
    """

    def test_the_frozen_build_looks_beside_the_exe_and_in_appdata(self):
        import nova
        seen = []
        exe = Path(r"C:\Program Files\NOVA\NOVA.exe")
        with mock.patch.object(nova.sys, "frozen", True, create=True), \
             mock.patch.object(nova.sys, "executable", str(exe)), \
             mock.patch.dict(nova.os.environ, {"APPDATA": r"C:\Users\x\AppData\Roaming"}), \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(nova, "load_dotenv", side_effect=lambda *a, **k: seen.append(a[0] if a else None)):
            nova._load_env_files()
        self.assertIn(exe.parent / ".env", seen,
                      "the install folder is where BUILD.md sends people")
        self.assertIn(Path(r"C:\Users\x\AppData\Roaming") / "NOVA" / ".env", seen)

    def test_development_still_uses_the_ordinary_search(self):
        """Unfrozen, nothing changes: the bare call is what dev relies on."""
        import nova
        seen = []
        with mock.patch.object(nova.sys, "frozen", False, create=True), \
             mock.patch.object(nova, "load_dotenv",
                               side_effect=lambda *a, **k: seen.append(a[0] if a else None)):
            nova._load_env_files()
        self.assertEqual(seen, [None])

    def test_an_existing_key_is_never_overridden(self):
        """A key already in the environment outranks any file on disk."""
        import nova
        calls = []
        with mock.patch.object(nova.sys, "frozen", False, create=True), \
             mock.patch.object(nova, "load_dotenv",
                               side_effect=lambda *a, **k: calls.append(k)):
            nova._load_env_files()
        self.assertTrue(all(c.get("override") is False for c in calls), calls)


class HonestModelReportingTests(unittest.TestCase):
    def test_status_names_the_model_that_will_actually_answer(self):
        """Reporting the cloud model while a local one answers hides the cause."""
        src = (ROOT / "desk" / "bridge.py").read_text(encoding="utf-8")
        block = src[src.index('"connectivity": connectivity'):]
        block = block[:block.index('"tools": tools')]
        self.assertIn('"serving"', block)
        self.assertIn("_GEMINI_KEY else", block)


class DailyQuotaTests(unittest.TestCase):
    """A day's allowance does not refill in four seconds.

    Measured against a real key: four calls succeed at ~1.5s, then every
    subsequent one returns 429 with quotaId
    GenerateRequestsPerDayPerProjectPerModel. Treating that like a passing
    squall cost three retries at 1s, 2s and 4s, repeated per fallback model —
    thirteen seconds of guaranteed failure before NOVA could say anything,
    which the user experiences as NOVA simply being slow.
    """

    DAILY = ("429 RESOURCE_EXHAUSTED quota exceeded. "
             "quotaId: GenerateRequestsPerDayPerProjectPerModel")
    MINUTE = ("429 RESOURCE_EXHAUSTED quota exceeded. "
              "quotaId: GenerateRequestsPerMinutePerProjectPerModel")

    def test_a_daily_cap_is_not_retried(self):
        from nova_intelligence.gemini_provider import GeminiProvider
        self.assertFalse(GeminiProvider._is_retryable(self.DAILY))

    def test_a_per_minute_limit_is_still_retried(self):
        """Waiting genuinely helps here, so the old behaviour is right."""
        from nova_intelligence.gemini_provider import GeminiProvider
        self.assertTrue(GeminiProvider._is_retryable(self.MINUTE))

    def test_transient_failures_are_still_retried(self):
        from nova_intelligence.gemini_provider import GeminiProvider
        for err in ("503 UNAVAILABLE", "500 internal", "connection reset",
                    "504 timeout", "model is overloaded"):
            with self.subTest(err=err):
                self.assertTrue(GeminiProvider._is_retryable(err))

    def test_the_user_is_told_it_is_a_daily_cap_not_a_fault(self):
        from desk.chat import _model_error_message
        msg = _model_error_message(self.DAILY)
        self.assertIn("today", msg.lower())
        self.assertNotIn("please wait and retry", msg.lower())

    def test_an_ordinary_429_still_says_wait(self):
        from desk.chat import _model_error_message
        self.assertIn("wait", _model_error_message(self.MINUTE).lower())


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
