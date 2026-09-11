"""Where NOVA puts a file when the user says "my Documents folder".

The naive answer, `Path.home() / "Documents"`, is wrong on a great many
Windows machines: once OneDrive takes over the folder, the real one lives
under ~/OneDrive/Documents and the naive guess silently creates a second,
empty Documents the user never opens. NOVA then truthfully reports a path
nobody can find. Asking the shell is the only correct way.

And she must never claim a save she did not make. A write that raised is
obvious; a write that went somewhere unexpected is not, and "I've saved it to
Documents" is exactly the sentence a user will not check.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.file_controller import _resolve_path, execute, user_folder


class FolderResolutionTests(unittest.TestCase):
    NAMES = ("documents", "downloads", "desktop", "pictures", "videos", "music")

    def test_every_common_folder_resolves(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                p = user_folder(name)
                self.assertIsInstance(p, Path)
                self.assertTrue(str(p), f"{name} resolved to nothing")

    def test_no_user_name_is_assumed(self):
        """It must work on a machine that is not this one."""
        import inspect

        from actions import file_controller
        src = inspect.getsource(file_controller)
        self.assertNotIn("Lenovo", src, "a specific user's name is hard-coded")
        self.assertNotIn("C:\\Users\\", src.replace("C:\\Users\\<", ""))

    @unittest.skipUnless(os.name == "nt", "Windows known folders")
    def test_windows_folders_come_from_the_shell(self):
        """Not string-concatenated onto the home directory."""
        import inspect

        from actions import file_controller
        src = inspect.getsource(file_controller)
        self.assertIn("SHGetKnownFolderPath", src)

    def test_spoken_phrasings_resolve_to_the_same_place(self):
        docs = user_folder("documents")
        for phrasing in ("documents", "Documents", "my documents",
                         "the documents folder", "my Documents folder"):
            with self.subTest(phrasing=phrasing):
                self.assertEqual(_resolve_path(phrasing), docs)

    def test_a_file_inside_a_named_folder_resolves(self):
        got = _resolve_path("Documents/notes.txt")
        self.assertEqual(got.parent, user_folder("documents"))
        self.assertEqual(got.name, "notes.txt")

    def test_a_full_path_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "x.txt"
            self.assertEqual(_resolve_path(str(target)), target)


class VerificationTests(unittest.TestCase):
    """A reported success must correspond to a file that exists."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_a_save_is_confirmed_against_the_disk(self):
        target = Path(self.dir.name) / "report.txt"
        out = execute({"action": "create_file", "path": str(target),
                       "content": "hello"})
        self.assertTrue(target.exists(), "nothing was written")
        self.assertIn("Saved", out)
        self.assertIn(str(len("hello")), out, "size was not reported")

    def test_a_failed_save_says_so(self):
        out = execute({"action": "write",
                       "path": "Z:/definitely/not/here/x.txt",
                       "content": "nope"})
        self.assertNotIn("Saved to", out)
        self.assertTrue(out.lower().startswith(("error", "failed", "permission")),
                        f"a failure was not reported as one: {out!r}")

    def test_missing_parent_directories_are_created(self):
        target = Path(self.dir.name) / "a" / "b" / "c.txt"
        execute({"action": "create_file", "path": str(target), "content": "x"})
        self.assertTrue(target.exists())

    def test_a_copy_is_confirmed_at_the_destination(self):
        src = Path(self.dir.name) / "src.txt"
        src.write_text("payload")
        dest = Path(self.dir.name) / "out" / "dest.txt"
        out = execute({"action": "copy", "path": str(src),
                       "destination": str(dest)})
        self.assertTrue(dest.exists())
        self.assertIn("Saved to", out)

    def test_a_move_notices_if_the_original_survived(self):
        src = Path(self.dir.name) / "m.txt"
        src.write_text("payload")
        dest = Path(self.dir.name) / "moved.txt"
        out = execute({"action": "move", "path": str(src),
                       "destination": str(dest)})
        self.assertTrue(dest.exists())
        self.assertFalse(src.exists())
        self.assertNotIn("warning", out)

    def test_reading_a_missing_file_is_not_reported_as_success(self):
        out = execute({"action": "read",
                       "path": str(Path(self.dir.name) / "nope.txt")})
        self.assertIn("not found", out.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
