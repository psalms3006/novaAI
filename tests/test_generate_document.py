"""Asking for a document should produce a document.

"Create a PDF report about my project" has one acceptable outcome: a PDF the
user can open. Prose in the conversation that they are invited to paste into
Word is not the thing they asked for, and neither is a .pdf that is secretly
plain text — a lie they only discover when they double-click it.

So these tests check the file exists, opens as the format it claims, and lands
where the user meant. And that a failure is reported as one.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.generate_document import SUPPORTED, execute

BODY = (
    "# Overview\n\nNOVA is OMNIEL's flagship system.\n\n"
    "- voice first\n- sees the screen\n\n## Status\n\nRebuilt.\n"
)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def make(self, fmt, content=BODY, title="Report"):
        target = Path(self.dir.name) / f"doc.{fmt}"
        out = execute({"content": content, "title": title,
                       "format": fmt, "path": str(target)})
        return out, target

    def test_every_supported_format_produces_a_real_file(self):
        for fmt in SUPPORTED:
            with self.subTest(fmt=fmt):
                out, target = self.make(fmt)
                self.assertTrue(target.exists(), f"{fmt}: {out}")
                self.assertGreater(target.stat().st_size, 0, f"{fmt} is empty")
                self.assertIn("Saved", out)

    def test_a_pdf_is_actually_a_pdf(self):
        """Not a text file wearing the extension."""
        _, target = self.make("pdf")
        self.assertEqual(target.read_bytes()[:5], b"%PDF-")

    def test_office_formats_are_real_office_files(self):
        for fmt in ("docx", "xlsx", "pptx"):
            with self.subTest(fmt=fmt):
                _, target = self.make("csv" if fmt == "csv" else fmt)
                self.assertTrue(zipfile.is_zipfile(target),
                                f"{fmt} is not a valid OOXML package")

    def test_a_docx_keeps_the_structure_it_was_given(self):
        """Headings and bullets, not one undifferentiated wall of text."""
        from docx import Document

        _, target = self.make("docx")
        doc = Document(str(target))
        styles = {p.style.name for p in doc.paragraphs}
        self.assertTrue(any(s.startswith("Heading") or s == "Title"
                            for s in styles), styles)
        self.assertIn("List Bullet", styles)

    def test_csv_round_trips(self):
        import csv as _csv

        out, target = self.make("csv", content="name,role\nPsalms,builder")
        rows = list(_csv.reader(target.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(rows[0], ["name", "role"])
        self.assertEqual(rows[1], ["Psalms", "builder"])

    def test_reportlab_markup_characters_do_not_break_the_pdf(self):
        """< & > are markup to reportlab; unescaped they abort the build."""
        out, target = self.make("pdf", content="if a < b && c > d: print('<hi>')")
        self.assertTrue(target.exists(), out)
        self.assertEqual(target.read_bytes()[:5], b"%PDF-")


class DestinationTests(unittest.TestCase):
    def test_a_named_folder_resolves_to_the_real_one(self):
        from actions.file_controller import user_folder
        from actions.generate_document import _resolve_destination

        got = _resolve_destination("documents", "My Report", "pdf")
        self.assertEqual(got.parent, user_folder("documents"))
        self.assertEqual(got.name, "My Report.pdf")

    def test_a_full_file_path_is_honoured(self):
        from actions.generate_document import _resolve_destination

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "exact.pdf"
            self.assertEqual(_resolve_destination(str(target), "x", "pdf"), target)

    def test_no_destination_means_documents_not_the_working_directory(self):
        """For a packaged app the working directory is wherever the shortcut
        happened to point, which is nobody's idea of "save it"."""
        from actions.file_controller import user_folder
        from actions.generate_document import _resolve_destination

        got = _resolve_destination("", "Untitled Thing", "docx")
        self.assertEqual(got.parent, user_folder("documents"))

    def test_a_hostile_title_cannot_escape_the_filename(self):
        from actions.generate_document import _safe_stem

        for bad in ("../../etc/passwd", 'a"b:c|d?e*f', "con/../x"):
            stem = _safe_stem(bad)
            self.assertNotIn("/", stem)
            self.assertNotIn("\\", stem)
            self.assertNotIn(":", stem)


class HonestyTests(unittest.TestCase):
    def test_an_unsupported_format_is_refused_by_name(self):
        out = execute({"content": "x", "title": "y", "format": "exe"})
        self.assertNotIn("Saved", out)
        self.assertIn("exe", out)
        for fmt in SUPPORTED:
            self.assertIn(fmt, out), "it should say what it *can* do"

    def test_empty_content_is_refused_rather_than_written(self):
        out = execute({"content": "   ", "format": "pdf"})
        self.assertNotIn("Saved", out)

    def test_an_unwritable_destination_reports_failure(self):
        out = execute({"content": BODY, "title": "x", "format": "txt",
                       "path": "Z:/definitely/not/here/x.txt"})
        self.assertNotIn("Saved", out)
        self.assertTrue(out.lower().startswith(("i tried", "failed", "i couldn't")),
                        f"failure was not reported plainly: {out!r}")

    def test_success_reports_where_and_how_big(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "r.txt"
            out = execute({"content": BODY, "title": "R", "format": "txt",
                           "path": str(target)})
            self.assertIn(str(target), out)
            self.assertIn(str(target.stat().st_size), out)


class RegistrationTests(unittest.TestCase):
    def test_nova_actually_offers_the_tool(self):
        """An unregistered generator is a generator nobody can reach."""
        import nova
        names = {t["name"] for t in nova.TOOL_DECLARATIONS}
        self.assertIn("generate_document", names)

    def test_writing_a_document_does_not_stop_to_ask(self):
        """Creating a file the user asked for is not a destructive act."""
        from nova_core import permissions as P
        d = P.check_tool("nova", "generate_document",
                         args={"content": "x", "format": "pdf"})
        self.assertEqual(d.effect.value, "allow")

    def test_untrusted_content_still_cannot_write_files(self):
        from nova_core import permissions as P
        from nova_core import trust as T
        d = P.check_tool("nova", "generate_document",
                         args={"content": "x"}, trust=T.Trust.UNTRUSTED)
        self.assertIn(d.effect.value, ("confirm", "deny"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
