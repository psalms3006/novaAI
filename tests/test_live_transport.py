"""What NOVA puts on the wire, and what she does when the wire misbehaves.

These are the transport rules that came out of measuring an actual Gemini Live
session from a real machine: batch the microphone so the socket sees a fifth
as many messages, stop paying for silence while NOVA is the one talking, and
be patient enough with the keepalive that a congested uplink does not end the
conversation.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk.live_session import (
    MIC_BLOCK,
    MIC_RATE,
    SEND_BATCH_MS,
    SILENCE_KEEPALIVE_MS,
    WS_PING_INTERVAL_S,
    WS_PING_TIMEOUT_S,
    MicBatcher,
    _relax_websocket_keepalive,
)

FRAME = bytes(MIC_BLOCK * 2)                 # one 64 ms frame of silence
VOICE = bytes([7, 0]) * MIC_BLOCK            # one 64 ms frame of "audio"

#: Frames it takes to fill one outbound batch, rounded up the way the batcher
#: does it. Derived, not hard-coded, so retuning SEND_BATCH_MS cannot leave
#: these tests asserting against a number that no longer means anything.
BATCH_FRAMES = -(-int(MIC_RATE * SEND_BATCH_MS / 1000) * 2 // (MIC_BLOCK * 2))


class MicBatcherTests(unittest.TestCase):
    def test_batches_frames_up_to_the_target(self):
        b = MicBatcher()
        out = [b.add(VOICE, silent=False) for _ in range(BATCH_FRAMES)]
        self.assertTrue(all(o is None for o in out[:-1]),
                        "emitted before the batch was full")
        self.assertIsNotNone(out[-1], "never emitted a full batch")
        self.assertGreaterEqual(len(out[-1]) / 2 / MIC_RATE * 1000,
                                SEND_BATCH_MS * 0.9)

    def test_speech_is_never_dropped(self):
        """Silence may be throttled. A person talking may not be."""
        b = MicBatcher()
        sent = 0
        for _ in range(200):
            if b.add(VOICE, silent=False) is not None:
                sent += 1
        expected = 200 / BATCH_FRAMES
        self.assertGreaterEqual(sent, expected * 0.95,
                                f"only {sent} of ~{expected:.0f} batches sent")

    def test_silence_is_throttled_to_a_keepalive(self):
        b = MicBatcher()
        sent = 0
        for _ in range(200):                 # 12.8 s of muted microphone
            if b.add(FRAME, silent=True) is not None:
                sent += 1
        unthrottled = 200 / BATCH_FRAMES
        self.assertLess(sent, unthrottled / 2,
                        f"sent {sent} silent batches; throttling did nothing")

    def test_silence_still_reaches_the_socket(self):
        """Throttled is not withheld. A stream that stops looks abandoned."""
        b = MicBatcher(silence_ms=10)
        sent = 0
        for _ in range(60):
            if b.add(FRAME, silent=True) is not None:
                sent += 1
            time.sleep(0.001)
        self.assertGreater(sent, 0, "no silence was transmitted at all")

    def test_a_batch_with_any_speech_in_it_counts_as_speech(self):
        b = MicBatcher()
        out = None
        for i in range(BATCH_FRAMES):
            out = b.add(VOICE if i == 0 else FRAME, silent=(i != 0))
            if out is not None:
                break
        self.assertIsNotNone(
            out, "a batch containing the start of a word was thrown away")

    def test_reset_clears_a_partial_batch(self):
        b = MicBatcher()
        b.add(VOICE, silent=False)
        b.reset()
        self.assertIsNone(b.add(VOICE, silent=False),
                          "reset did not discard the partial batch")


class KeepaliveTests(unittest.TestCase):
    def test_timeout_is_wider_than_the_worst_measured_stall(self):
        """A 15 s send stall was measured on a real uplink; 20 s is not enough."""
        self.assertGreater(WS_PING_TIMEOUT_S, 20)
        self.assertGreaterEqual(WS_PING_TIMEOUT_S, 60)

    def test_pinging_is_still_enabled(self):
        """Patience, not blindness. A dead socket must still be detected."""
        self.assertIsNotNone(WS_PING_INTERVAL_S)
        self.assertGreater(WS_PING_INTERVAL_S, 0)

    def test_settings_reach_the_websocket_layer(self):
        class FakeApiClient:
            def __init__(self):
                self._websocket_ssl_ctx = {"ssl": object()}

        class FakeClient:
            def __init__(self):
                self._api_client = FakeApiClient()

        c = FakeClient()
        self.assertTrue(_relax_websocket_keepalive(c))
        self.assertEqual(c._api_client._websocket_ssl_ctx["ping_timeout"],
                         WS_PING_TIMEOUT_S)
        self.assertEqual(c._api_client._websocket_ssl_ctx["ping_interval"],
                         WS_PING_INTERVAL_S)

    def test_an_sdk_change_degrades_instead_of_crashing(self):
        """It reaches into a private attribute, so it must survive losing it."""
        class Missing:
            pass

        self.assertFalse(_relax_websocket_keepalive(Missing()))

        class WrongShape:
            def __init__(self):
                self._api_client = type("A", (), {"_websocket_ssl_ctx": None})()

        self.assertFalse(_relax_websocket_keepalive(WrongShape()))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class OfflineTests(unittest.TestCase):
    """Losing the network must pause voice, not end it.

    Observed during testing when this machine's DNS went down: six
    getaddrinfo failures inside ninety seconds exhausted the retry budget and
    NOVA gave up permanently. A laptop that sleeps, changes wifi, or goes
    through a tunnel does exactly that, and voice should come back on its own
    rather than needing the app restarted.
    """

    from desk.live_session import LiveManager

    def test_name_resolution_failures_are_recognised_as_offline(self):
        for err in ("[Errno 11001] getaddrinfo failed",
                    "[Errno 11002] getaddrinfo failed",
                    "Name or service not known",
                    "Temporary failure in name resolution",
                    "Network is unreachable"):
            with self.subTest(err=err):
                self.assertTrue(self.LiveManager._is_offline(err))

    def test_a_rejected_key_is_not_mistaken_for_being_offline(self):
        """These must not be confused: one is waited out, the other is fatal."""
        for err in ("API key not valid. Please pass a valid API key.",
                    "PERMISSION_DENIED", "UNAUTHENTICATED"):
            with self.subTest(err=err):
                self.assertFalse(self.LiveManager._is_offline(err))
                self.assertTrue(self.LiveManager._is_auth_failure(err))

    def test_a_server_error_is_neither(self):
        err = "sent 1011 (internal error) keepalive ping timeout"
        self.assertFalse(self.LiveManager._is_offline(err))
        self.assertFalse(self.LiveManager._is_auth_failure(err))

    def test_offline_retries_stay_frequent_enough_to_notice_recovery(self):
        self.assertLessEqual(self.LiveManager.OFFLINE_RETRY_DELAY, 15.0)
        self.assertGreaterEqual(self.LiveManager.OFFLINE_RETRY_DELAY, 1.0)

    def test_being_offline_does_not_consume_the_retry_budget(self):
        import inspect
        src = inspect.getsource(self.LiveManager._connect_and_run)
        offline_branch = src[src.index("elif self._is_offline"):]
        self.assertIn("consecutive_failures = 0", offline_branch.split("else:")[0],
                      "offline attempts still count toward giving up")
