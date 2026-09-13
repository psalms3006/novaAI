"""Not spending the uplink on an empty room, and not running twice.

Two reasons NOVA stopped hearing people.

She streamed 32 KB/s of microphone audio continuously, whether or not anyone
was talking. The gate suppressed her own voice and the batcher throttled that,
but nobody noticed the far more common case: a room with no one speaking in
it. On a connection without headroom, room tone is the bandwidth the user's
next sentence needed.

And nothing stopped a second NOVA starting. The second loses the port so it
has no window and looks like it failed to launch, but it still opens the
microphone and still streams. Measured with several stacked instances: mic
sends reaching 21 seconds and 555 frames of speech discarded. With a single
instance, zero of either.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nova_voice as nv
from desk.live_session import MicBatcher, SEND_BATCH_MS, SILENCE_KEEPALIVE_MS


def frames(gate, amp, n=1, size=1024):
    out = []
    for _ in range(n):
        out.append(gate.process((np.random.randn(size) * amp).astype(np.int16)))
    return out


class QuietRoomTests(unittest.TestCase):
    def setUp(self):
        self.gate = nv.VoiceGate(chunk_samples=1024)

    def test_an_empty_room_is_recognised_as_quiet(self):
        frames(self.gate, 25, n=40)
        self.assertTrue(self.gate.last_was_quiet)

    def test_someone_speaking_is_not_quiet(self):
        frames(self.gate, 25, n=40)
        frames(self.gate, 3000, n=1)
        self.assertFalse(self.gate.last_was_quiet)

    def test_a_pause_between_words_does_not_count_as_quiet(self):
        """Cutting the uplink mid-sentence truncates the turn."""
        frames(self.gate, 25, n=40)
        frames(self.gate, 3000, n=1)
        frames(self.gate, 25, n=6)          # ~380 ms of breath
        self.assertFalse(self.gate.last_was_quiet)

    def test_quiet_returns_once_they_have_actually_stopped(self):
        frames(self.gate, 25, n=40)
        frames(self.gate, 3000, n=1)
        self.gate._last_loud_at = time.time() - (nv.QUIET_HOLDOVER_S + 0.3)
        frames(self.gate, 25, n=1)
        self.assertTrue(self.gate.last_was_quiet)

    def test_a_digitally_silent_input_cannot_learn_a_floor_of_zero(self):
        """A muted device or an idle virtual cable must not call hiss speech."""
        g = nv.VoiceGate(chunk_samples=1024)
        for _ in range(40):
            g.process(np.zeros(1024, dtype=np.int16))
        self.assertTrue(g.last_was_quiet)

    def test_a_noisy_room_still_hears_a_raised_voice(self):
        """The floor is learned, so a fan must not make someone inaudible."""
        g = nv.VoiceGate(chunk_samples=1024)
        frames(g, 200, n=60)                # noisy room
        frames(g, 5000, n=1)                # someone talks over it
        self.assertFalse(g.last_was_quiet)


class BatcherThrottleTests(unittest.TestCase):
    """Quiet is throttled to a keepalive; speech is never held back."""

    def setUp(self):
        self.b = MicBatcher()
        self.frame = bytes(1024 * 2)
        self.per_batch = int(16000 * SEND_BATCH_MS / 1000) * 2

    def _feed(self, silent, n):
        sent = 0
        for _ in range(n):
            if self.b.add(self.frame, silent) is not None:
                sent += 1
        return sent

    def test_speech_is_sent_every_batch(self):
        n = (self.per_batch // len(self.frame)) * 5
        self.assertGreaterEqual(self._feed(False, n), 4)

    def test_quiet_is_throttled_to_a_keepalive(self):
        n = (self.per_batch // len(self.frame)) * 8
        sent = self._feed(True, n)
        self.assertLessEqual(sent, 2, "quiet should collapse to a keepalive")

    def test_a_batch_with_any_sound_in_it_is_sent(self):
        """One loud frame in a batch makes the whole batch worth sending."""
        per = self.per_batch // len(self.frame)
        out = None
        for i in range(per):
            out = self.b.add(self.frame, i != 0)   # first frame is real audio
        self.assertIsNotNone(out)

    def test_the_keepalive_gap_is_a_real_interval(self):
        self.assertGreater(SILENCE_KEEPALIVE_MS, SEND_BATCH_MS)


class SingleInstanceTests(unittest.TestCase):
    def test_a_second_instance_is_refused(self):
        import nova_desktop_app as app
        if sys.platform != "win32":
            self.skipTest("named mutex is the Windows mechanism")
        first = app._claim_single_instance()
        self.assertNotIn(first, (False,), "the first copy must be allowed")
        try:
            self.assertIs(app._claim_single_instance(), False)
        finally:
            if first not in (False, None):
                import ctypes
                ctypes.windll.kernel32.CloseHandle(first)

    def test_the_check_never_blocks_startup_on_its_own_failure(self):
        """A broken guard must not be the reason NOVA will not start."""
        import nova_desktop_app as app
        from unittest import mock
        with mock.patch.object(app.os, "name", "posix"):
            self.assertIsNone(app._claim_single_instance())


if __name__ == "__main__":
    unittest.main(verbosity=2)
