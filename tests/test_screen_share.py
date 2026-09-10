"""What NOVA can see, and what it costs her to see it.

Screen awareness shares the voice session's uplink, which measurements showed
is already the tightest resource in the pipeline. So the rules that matter are
about restraint: sample rather than stream, skip what has not changed, keep a
frame comparable in size to a second of audio, and never look at all unless
explicitly asked to.
"""
from __future__ import annotations

import io
import sys
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk.screen_share import (
    CHANGE_THRESHOLD,
    JPEG_QUALITY,
    MAX_EDGE_PX,
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    ScreenShare,
)

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False


class FakeGrab:
    """Stands in for an mss screenshot."""

    def __init__(self, img):
        self.size = img.size
        self.rgb = img.convert("RGB").tobytes()


class FakeSct:
    def __init__(self, images):
        self.images = list(images)
        self.monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]
        self.grabs = 0

    def grab(self, _monitor):
        img = self.images[min(self.grabs, len(self.images) - 1)]
        self.grabs += 1
        return FakeGrab(img)


def _screen(seed, w=1920, h=1080):
    """A picture with real structure — a flat colour compresses to nothing."""
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 255, (h // 8, w // 8, 3), dtype=np.uint8)
    return Image.fromarray(a).resize((w, h), Image.NEAREST)


@unittest.skipUnless(HAS_PIL, "Pillow not installed")
class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.share = ScreenShare(sink=lambda b, m: self.sent.append((b, m)))

    def test_an_unchanged_screen_costs_nothing(self):
        """Someone reading a page must not be paying for it every two seconds."""
        img = _screen(1)
        sct = FakeSct([img])
        self.share._tick(sct, sct.monitors[0])
        self.assertEqual(len(self.sent), 1, "the first frame was not sent")
        for _ in range(5):
            self.share._tick(sct, sct.monitors[0])
        self.assertEqual(len(self.sent), 1,
                         "an unchanged screen was transmitted repeatedly")
        self.assertEqual(self.share.frames_skipped, 5)

    def test_a_changed_screen_is_sent(self):
        sct = FakeSct([_screen(1)])
        self.share._tick(sct, sct.monitors[0])
        sct.images = [_screen(2)]
        sct.grabs = 0
        self.share._tick(sct, sct.monitors[0])
        self.assertEqual(len(self.sent), 2, "a window switch was not noticed")

    def test_a_static_screen_is_refreshed_eventually(self):
        """The model's view must not go quietly stale."""
        img = _screen(1)
        sct = FakeSct([img])
        self.share._tick(sct, sct.monitors[0])
        self.share._last_sent_at = time.time() - MAX_INTERVAL_S - 1
        self.share._tick(sct, sct.monitors[0])
        self.assertEqual(len(self.sent), 2)

    def test_a_blinking_cursor_is_not_a_change(self):
        base = _screen(3)
        edited = base.copy()
        for x in range(400, 404):          # a caret, a few pixels wide
            for y in range(500, 520):
                edited.putpixel((x, y), (255, 255, 255))
        sct = FakeSct([base])
        self.share._tick(sct, sct.monitors[0])
        sct.images = [edited]
        sct.grabs = 0
        self.share._tick(sct, sct.monitors[0])
        self.assertEqual(len(self.sent), 1,
                         "a blinking cursor triggered a transmission")

    def test_a_frame_is_small_enough_to_share_the_uplink(self):
        """A frame competes with the audio stream, which is ~32 kB/s."""
        sct = FakeSct([_screen(4)])
        self.share._tick(sct, sct.monitors[0])
        payload, mime = self.sent[0]
        self.assertEqual(mime, "image/jpeg")
        self.assertLess(len(payload), 200 * 1024,
                        f"a screen frame is {len(payload) / 1024:.0f} kB")

    def test_a_frame_is_downscaled_but_still_legible(self):
        sct = FakeSct([_screen(5)])
        self.share._tick(sct, sct.monitors[0])
        img = Image.open(io.BytesIO(self.sent[0][0]))
        self.assertLessEqual(max(img.size), MAX_EDGE_PX)
        self.assertGreaterEqual(max(img.size), 640,
                                "downscaled past the point of reading text")

    def test_nothing_is_written_to_disk(self):
        import inspect
        from desk import screen_share
        src = inspect.getsource(screen_share)
        for bad in (".save(path", "open(path", "NamedTemporaryFile", "mkstemp"):
            self.assertNotIn(bad, src,
                             f"screen frames may reach the disk via {bad}")


class ControlTests(unittest.TestCase):
    def test_watching_is_off_until_asked_for(self):
        share = ScreenShare(sink=lambda b, m: None)
        self.assertFalse(share.enabled)
        self.assertFalse(share.status()["watching"])

    def test_stop_releases_the_last_captured_frame(self):
        """Nothing of the screen is kept once NOVA is no longer looking."""
        share = ScreenShare(sink=lambda b, m: None)
        share._last_thumb = np.zeros((64, 64), dtype=np.float32)
        share.stop()
        self.assertIsNone(share._last_thumb)

    def test_missing_dependencies_are_reported_not_crashed(self):
        import desk.screen_share as ss
        original = ss.HAS_MSS
        try:
            ss.HAS_MSS = False
            share = ScreenShare(sink=lambda b, m: None)
            result = share.start()
            self.assertFalse(result["ok"])
            self.assertIn("mss", result["reason"])
            self.assertFalse(share.enabled)
        finally:
            ss.HAS_MSS = original


class BudgetTests(unittest.TestCase):
    def test_sampling_interval_is_conversational_but_affordable(self):
        self.assertGreaterEqual(MIN_INTERVAL_S, 1.0)
        self.assertLessEqual(MIN_INTERVAL_S, 3.0)

    def test_change_threshold_is_neither_deaf_nor_twitchy(self):
        self.assertGreater(CHANGE_THRESHOLD, 0.0)
        self.assertLess(CHANGE_THRESHOLD, 10.0)

    def test_quality_is_set_for_size_not_beauty(self):
        self.assertLessEqual(JPEG_QUALITY, 70)


class SessionIntegrationTests(unittest.TestCase):
    def test_screen_share_cannot_start_without_a_voice_session(self):
        """It shares the conversation; there has to be one to share."""
        from desk.live_session import LiveManager
        m = LiveManager()
        result = m.set_screen_share(True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["watching"])

    def test_frames_go_into_the_existing_session(self):
        """Not a second Gemini connection — ambient mode is one NOVA."""
        import inspect
        from desk.live_session import LiveManager
        src = inspect.getsource(LiveManager._video_sender)
        self.assertIn("session.send_realtime_input", src)
        self.assertIn("video=", src)

    def test_a_stale_frame_is_dropped_rather_than_queued(self):
        from desk.live_session import LiveManager
        import inspect
        src = inspect.getsource(LiveManager._offer_video)
        self.assertIn("QueueFull", src,
                      "screen frames can back up and delay audio")

    def test_losing_vision_does_not_take_down_the_conversation(self):
        import inspect
        from desk.live_session import LiveManager
        src = inspect.getsource(LiveManager._video_sender)
        self.assertNotIn("raise", src,
                         "a failed screen frame propagates and kills the session")

    def test_screen_sharing_stops_with_the_session(self):
        import inspect
        from desk.live_session import LiveManager
        src = inspect.getsource(LiveManager._connect_and_run)
        self.assertIn("self._screen.stop()", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
