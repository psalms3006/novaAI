"""The native ambient orb: what it draws and which state it shows."""
import time

import numpy as np
import pytest

from desk import ambient_native as an
from desk import orb_engine


def _reference_dots(dots, px, c, swell, dark):
    """The obvious painter: one dot at a time, far to near, "over"."""
    color = np.zeros((px, px), np.float64)
    alpha = np.zeros((px, px), np.float64)
    yy, xx = np.mgrid[0:px, 0:px] + 0.5
    for d in dots:
        x, y, r = c + (d["x"] - c) * swell, c + (d["y"] - c) * swell, d["r"] * swell
        cov = np.clip(r - np.hypot(xx - x, yy - y) + 0.5, 0, 1) * d["a"]
        cov = np.minimum(cov, 0.9999)
        w = min(1, max(0, d["white"]))
        v = (1 - w) if dark else w
        color = v * cov + color * (1 - cov)
        alpha = cov + alpha * (1 - cov)
    return color, alpha


@pytest.mark.parametrize("state", list(orb_engine.STATE_TO_MODE))
@pytest.mark.parametrize("px", [44, 96])
def test_fast_painter_draws_what_the_obvious_one_does(state, px):
    mode, _, opts = orb_engine.resolve_preset(state, 64)
    dots, _ = orb_engine.MODE_FRAMES[mode](px, 2.3, opts)
    fast_c, fast_a = an._composite_dots([dict(d) for d in dots], px, px / 2, 1.05, True)
    ref_c, ref_a = _reference_dots(dots, px, px / 2, 1.05, True)
    assert np.abs(fast_c - ref_c).max() < 2e-3
    assert np.abs(fast_a - ref_a).max() < 2e-3


def test_frame_is_see_through_between_dots_and_one_target():
    img = an.render_orb("breathing", 1.0, 44)
    assert img.shape == (44, 44, 4)
    a = img[..., 3]
    assert (a == 1).mean() > 0.5          # most of the circle: invisible but clickable
    assert a.max() > 200                  # the dots themselves are solid
    assert a[0, 0] == 0                   # outside the circle: not part of the window
    assert (img[..., 0] <= a).all()       # premultiplied: colour never exceeds alpha


def test_light_theme_draws_dark_ink():
    dark = an.render_orb("searching", 1.0, 44, dark=True)
    light = an.render_orb("searching", 1.0, 44, dark=False)
    lit = dark[..., 3] > 128
    assert dark[..., 0][lit].mean() > light[..., 0][lit].mean()


def test_tracker_follows_voice_and_tools():
    t = an.OrbStateTracker()
    assert t.current()[0] == "breathing"
    t.on_live({"type": "state", "state": "listening"})
    assert t.current()[0] == "listening"
    t.on_live({"type": "state", "state": "speaking"})
    t.on_live({"type": "audio_level", "level": 0.7})
    assert t.current() == ("composing", 0.7)
    t.on_live({"type": "tool_call", "tools": ["web_search"]})
    assert t.current()[0] == "searching"
    time.sleep(0.01)
    t.on_live({"type": "tool_call", "tools": ["nova_learning"]})
    assert t.current()[0] == "weaving"                 # the newest tool shows
    t.on_live({"type": "tool_result", "tool": "nova_learning"})
    t.on_live({"type": "tool_result", "tool": "web_search"})
    assert t.current()[0] == "composing"
    t.on_live({"type": "state", "state": "closed"})
    assert t.current()[0] == "breathing"


def test_tracker_follows_typed_chat_and_forgets_stuck_tools():
    t = an.OrbStateTracker()
    t.on_bus({"type": "orb_state", "state": "executing"})
    assert t.current()[0] == "working"
    t.on_bus({"type": "agent_start", "agent_id": "a1", "tool": "research_report"})
    assert t.current()[0] == "searching"
    t.on_bus({"type": "agent_done", "agent_id": "a1"})
    t.on_bus({"type": "orb_state", "state": "idle"})
    assert t.current()[0] == "breathing"
    t.on_live({"type": "tool_call", "tools": ["app_control"]})
    assert t.current(time.time() + an.TOOL_HOLD_S + 1)[0] == "breathing"
