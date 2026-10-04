"""The ambient orb can be dragged out of the way, and clicked to open NOVA.

Reproduced 2026-09-24 with a replica of the ambient window (the real page,
the app's exact circle clip and window style) driven by the real mouse:

* with the layered colour key the app applied: a drag left the window where
  it was, a click did nothing, and the page received 0 pointer events;
* without it: the same drag moved the window 120 px and did not open the
  dashboard, and a click sent exactly one {"mode": "full"} to /api/ambient.

The key was meant to make the black around the orb transparent. Under
WebView2 it never did (DirectComposition paints past it); all it did was
take the orb's input away. That test drives the physical mouse, so it is not
part of the suite; these pin the structure it showed to matter.
"""
from __future__ import annotations

import re
from pathlib import Path

# The checks on the previous window (desk/static) that lived here moved to
# tests/test_desk_ui_behaviour.py when the interface became desk/ui.

ROOT = Path(__file__).resolve().parent.parent
SHELL = (ROOT / "nova_desktop_app.py").read_text(encoding="utf-8")


def _code(src: str) -> str:
    """Source without comments, so an explanation cannot satisfy a test."""
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def test_the_ambient_window_is_not_colour_keyed():
    code = _code(SHELL)
    # A call, not a mention: _round_window's docstring explains the key.
    assert not re.search(r"\.SetLayeredWindowAttributes\(", code), (
        "a layered colour key on the ambient window blocks every drag and click")
    assert not re.search(r"LWA_COLORKEY\s*=", code)


def test_the_ambient_window_is_draggable_and_round():
    """Now a native layered window (desk/ambient_native.py): it moves itself
    on a drag, and only its circle takes the mouse -- the corners are alpha 0,
    which Windows treats as not part of the window."""
    code = _code(SHELL)
    assert "AmbientOrbWindow(" in code
    from desk import ambient_native as an
    src = (ROOT / "desk" / "ambient_native.py").read_text(encoding="utf-8")
    assert "WM_MOUSEMOVE" in src and "DRAG_PX" in src and "self.move(" in src
    img = an.render_orb("breathing", 1.0, 44)
    assert img[0, 0, 3] == 0 and img[22, 22, 3] >= 1


