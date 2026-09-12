"""Driving an application has one unacceptable outcome: a confident lie.

NOVA saying "I typed that into Spotify" when the keystrokes went to a dead
window is worse than an error, because the user believes it and moves on. The
three bugs these tests pin down all had that shape:

  * `inspect` raised NameError on a leftover variable, so the action the model
    is told to call first never worked at all;
  * `_find_handle` ranked a hung window above the live one because it looked
    for "not responding" in the *title*, and Windows only renames a window
    once it has already stopped answering;
  * `_raise` attached the wrong pair of threads, so Windows refused the
    foreground change and input went to whatever was in front.

None of these need a desktop to test. The window list, the hung-window check
and the value pattern are all seams, so they are driven with fakes here and
the real thing is exercised by hand.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import app_control as A


class FakeValuePattern:
    def __init__(self, readonly=False, fail=False):
        self.CurrentIsReadOnly = 1 if readonly else 0
        self._fail = fail
        self.written = None

    def SetValue(self, text):
        if self._fail:
            raise RuntimeError("pattern not supported")
        self.written = text


class FakeControl:
    def __init__(self, iface=None):
        self.iface_value = iface


class ValueWritingTests(unittest.TestCase):
    """Typing should go through the value pattern when one is offered."""

    def test_a_writable_control_is_written_in_one_call(self):
        iface = FakeValuePattern()
        self.assertTrue(A._set_value(FakeControl(iface), "hello"))
        self.assertEqual(iface.written, "hello")

    def test_a_readonly_control_falls_back_to_keystrokes(self):
        iface = FakeValuePattern(readonly=True)
        self.assertFalse(A._set_value(FakeControl(iface), "hello"))
        self.assertIsNone(iface.written)

    def test_a_control_without_the_pattern_falls_back(self):
        self.assertFalse(A._set_value(FakeControl(None), "hello"))

    def test_a_pattern_that_raises_falls_back_rather_than_propagating(self):
        """The keystroke path is still available; an exception would lose it."""
        iface = FakeValuePattern(fail=True)
        self.assertFalse(A._set_value(FakeControl(iface), "hello"))

    def test_nothing_to_write_into_falls_back(self):
        self.assertFalse(A._set_value(None, "hello"))


class WindowRankingTests(unittest.TestCase):
    """The live window must win, even when the dead one has the better title.

    This is the exact shape seen on a real desktop: Notepad puts up a bare
    "Notepad" window that has stopped pumping messages alongside the real
    "Untitled - Notepad", and the bare one is an exact title match.
    """

    LIVE = (330062, "Untitled - Notepad")
    HUNG = (198128, "Notepad")

    def _find(self, hung_handles, windows=None):
        windows = windows if windows is not None else [self.LIVE, self.HUNG]

        user32 = mock.MagicMock()
        user32.IsHungAppWindow.side_effect = lambda h: 1 if h in hung_handles else 0
        # Every candidate reports the same client area, so ranking has to turn
        # on liveness rather than on size.
        user32.GetClientRect.side_effect = lambda h, p: None

        with mock.patch.object(A, "_enum_windows", return_value=windows), \
             mock.patch("ctypes.windll") as windll:
            windll.user32 = user32
            return A._find_handle("Notepad", timeout=0)

    def test_a_hung_window_loses_to_a_live_one(self):
        self.assertEqual(self._find({self.HUNG[0]}), self.LIVE)

    def test_the_title_marker_is_still_honoured(self):
        """A ghost window is named "(Not Responding)" and is never the target."""
        ghost = (395716, "Notepad (Not Responding)")
        got = self._find(set(), windows=[ghost, self.LIVE])
        self.assertEqual(got, self.LIVE)

    def test_no_match_returns_nothing_rather_than_a_wrong_window(self):
        with mock.patch.object(A, "_enum_windows", return_value=[(1, "Calculator")]), \
             mock.patch("ctypes.windll"):
            self.assertIsNone(A._find_handle("Spotify", timeout=0))


class EscapingTests(unittest.TestCase):
    def test_send_keys_syntax_is_escaped(self):
        """Unescaped, "50% (a+b)" is read as keystroke syntax, not as text."""
        out = A._escape_keys("50% (a+b)")
        for ch in "%+()":
            self.assertIn("{" + ch + "}", out)


class DispatchTests(unittest.TestCase):
    def test_an_unknown_action_lists_the_real_ones(self):
        out = A.execute({"action": "levitate"})
        self.assertIn("levitate", out)
        self.assertIn("inspect", out)

    def test_missing_automation_is_reported_as_unavailable(self):
        """Without pywinauto the tool must say so, not raise into the turn."""
        with mock.patch.dict(A._ACTIONS,
                             {"inspect": mock.Mock(side_effect=ImportError("no pywinauto"))}):
            out = A.execute({"action": "inspect", "window": "x"})
        self.assertIn("unavailable", out.lower())

    def test_inspect_reports_the_window_it_found(self):
        """The NameError here meant this action never once returned a result."""
        with mock.patch.object(A, "_find_handle", return_value=(1, "Spotify Premium")), \
             mock.patch.object(A, "_wrap", return_value=object()), \
             mock.patch.object(A, "_controls",
                               return_value=[{"type": "Button", "name": "Play",
                                              "enabled": True}]):
            out = A.execute({"action": "inspect", "window": "Spotify"})
        self.assertIn("Spotify Premium", out)
        self.assertIn("Play", out)

    def test_inspect_says_so_when_a_window_exposes_nothing(self):
        with mock.patch.object(A, "_find_handle", return_value=(1, "Game")), \
             mock.patch.object(A, "_wrap", return_value=object()), \
             mock.patch.object(A, "_controls", return_value=[]):
            out = A.execute({"action": "inspect", "window": "Game"})
        self.assertIn("Game", out)
        self.assertIn("no controls", out.lower())


class TypingRefusalTests(unittest.TestCase):
    def test_nothing_is_typed_into_a_window_that_cannot_be_raised(self):
        """Typing into whatever is in front instead is the worst outcome."""
        with mock.patch.object(A, "_find_handle", return_value=(1, "Spotify")), \
             mock.patch.object(A, "_raise"), \
             mock.patch.object(A, "_foreground_title", return_value="Some Other App"):
            out = A.execute({"action": "type", "window": "Spotify", "text": "hi"})
        self.assertIn("didn't type", out.lower())

    def test_a_hung_window_is_refused_before_anything_is_sent(self):
        with mock.patch.object(A, "_find_handle",
                               return_value=(1, "Notepad (Not Responding)")):
            out = A.execute({"action": "type", "window": "Notepad", "text": "hi"})
        self.assertIn("nothing was typed", out.lower())

    def test_empty_text_is_refused(self):
        self.assertIn("nothing to type",
                      A.execute({"action": "type", "window": "x", "text": ""}).lower())


class WrongWindowTests(unittest.TestCase):
    """The right application in front is not the same as the right window.

    Asking for "Notepad" while "*model.safetensors - Notepad" held the
    foreground passed a substring check and the keystrokes went into a file
    that had nothing to do with the task. Identity, not titles.
    """

    ASKED = (1001, "Untitled - Notepad")
    OTHER = (2002, "*model.safetensors - Notepad")

    def _front(self, handle):
        """Pretend `handle` owns the foreground."""
        titles = dict([self.ASKED, self.OTHER])
        return (mock.patch.object(A, "_foreground_handle", return_value=handle),
                mock.patch.object(A, "_foreground_title",
                                  return_value=titles.get(handle, "")))

    def test_typing_refuses_a_sibling_window_of_the_same_app(self):
        fh, ft = self._front(self.OTHER[0])
        with mock.patch.object(A, "_find_handle", return_value=self.ASKED), \
             mock.patch.object(A, "_raise"), fh, ft, \
             mock.patch.object(A, "_set_value") as set_value:
            out = A.execute({"action": "type", "window": "Notepad", "text": "hi"})
        self.assertIn("didn't type", out.lower())
        self.assertIn("model.safetensors", out)
        set_value.assert_not_called()

    def test_typing_proceeds_when_the_right_window_is_front(self):
        fh, ft = self._front(self.ASKED[0])
        with mock.patch.object(A, "_find_handle", return_value=self.ASKED), \
             mock.patch.object(A, "_raise"), fh, ft, \
             mock.patch.object(A, "_editable_safely", return_value=None), \
             mock.patch.object(A, "_set_value", return_value=True) as set_value:
            out = A.execute({"action": "type", "window": "Notepad", "text": "hi"})
        set_value.assert_called_once()
        self.assertNotIn("didn't type", out.lower())

    def test_pressing_refuses_a_sibling_window_of_the_same_app(self):
        """"^s" sent to the wrong window writes the wrong file."""
        fh, ft = self._front(self.OTHER[0])
        with mock.patch.object(A, "_find_handle", return_value=self.ASKED), \
             mock.patch.object(A, "_raise"), fh, ft, \
             mock.patch("pywinauto.keyboard.send_keys") as send:
            out = A.execute({"action": "press", "window": "Notepad", "keys": "^s"})
        self.assertIn("didn't press", out.lower())
        send.assert_not_called()

    def test_pressing_refuses_a_window_that_is_not_responding(self):
        with mock.patch.object(A, "_find_handle",
                               return_value=(1, "Notepad (Not Responding)")), \
             mock.patch("pywinauto.keyboard.send_keys") as send:
            out = A.execute({"action": "press", "window": "Notepad", "keys": "^s"})
        self.assertIn("not responding", out.lower())
        send.assert_not_called()

    def test_focus_reports_failure_when_a_sibling_is_front(self):
        fh, ft = self._front(self.OTHER[0])
        with mock.patch.object(A, "_find_handle", return_value=self.ASKED), \
             mock.patch.object(A, "_raise"), fh, ft:
            out = A.execute({"action": "focus", "window": "Notepad"})
        self.assertIn("model.safetensors", out)
        self.assertNotIn("is now in front", out)

    def test_a_dialog_owned_by_the_target_still_counts_as_front(self):
        """An app may answer by opening a dialog; that is not a failure."""
        with mock.patch.object(A, "_foreground_handle", return_value=3003), \
             mock.patch("ctypes.windll") as windll:
            windll.user32.GetWindow.return_value = self.ASKED[0]
            self.assertTrue(A._is_front(self.ASKED[0]))


class RegistrationTests(unittest.TestCase):
    def test_nova_actually_offers_the_tool(self):
        import nova
        names = {t["name"] for t in nova.TOOL_DECLARATIONS}
        self.assertIn("app_control", names)

    def test_the_tool_is_dispatchable(self):
        """Declared but unroutable is the same as absent, from the model's side."""
        import nova
        self.assertIn("app_control", nova._validate_tool_modules())

    def test_looking_at_a_window_does_not_stop_to_ask(self):
        from nova_core import permissions as P
        for action in ("inspect", "list_windows"):
            with self.subTest(action=action):
                d = P.check_tool("nova", "app_control", args={"action": action})
                self.assertEqual(d.effect.value, "allow")

    def test_driving_an_app_is_allowed_but_declared(self):
        """Confirming every click would make a voice task unusable."""
        from nova_core import permissions as P
        d = P.check_tool("nova", "app_control",
                         args={"action": "click", "control": "Play"})
        self.assertEqual(d.effect.value, "allow")

    def test_untrusted_content_cannot_drive_applications(self):
        """A web page must not be able to press buttons on the desktop."""
        from nova_core import permissions as P
        from nova_core import trust as T
        d = P.check_tool("nova", "app_control",
                         args={"action": "click", "control": "Send"},
                         trust=T.Trust.UNTRUSTED)
        self.assertIn(d.effect.value, ("confirm", "deny"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
