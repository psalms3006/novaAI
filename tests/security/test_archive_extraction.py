"""Archive extraction: the Zip Slip / Tar traversal boundary.

An archive is content the user did not author. These tests feed crafted
malicious archives (generated from bytes in the sandbox fixture, no
downloads) and prove extraction writes nothing outside the destination and
nothing into credential locations — then prove an honest archive still
extracts, so the control is not refusal-by-default.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from actions import file_processor


class TestZipSlipBlocked:
    def test_traversal_entry_writes_nothing_outside(self, sandbox, tmp_path):
        dest = tmp_path / "unzip-here"
        out = file_processor.execute({
            "file_path": str(sandbox["evil_zip"]),
            "action": "extract",
            "destination": str(dest),
        })
        assert out.startswith("Refused:"), out
        # The escape marker must not exist anywhere the entry aimed for.
        assert not (tmp_path / "escaped_by_zip.txt").exists()
        assert not (sandbox["root"] / "escaped_by_zip.txt").exists()
        assert not (sandbox["workspace"] / "escaped_by_zip.txt").exists()

    def test_refusal_does_not_extract_the_honest_entries_either(
            self, sandbox, tmp_path):
        """A refused archive must not land half its contents."""
        dest = tmp_path / "unzip-here"
        file_processor.execute({
            "file_path": str(sandbox["evil_zip"]),
            "action": "extract",
            "destination": str(dest),
        })
        if dest.exists():
            assert not (dest / "normal.txt").exists(), \
                "the 'safe' member was extracted before the refusal"


class TestTarTraversalBlocked:
    def test_parent_hop_entry_blocked(self, sandbox, tmp_path):
        dest = tmp_path / "untar-here"
        out = file_processor.execute({
            "file_path": str(sandbox["evil_tar"]),
            "action": "extract",
            "destination": str(dest),
        })
        assert out.startswith("Refused:"), out
        assert not (tmp_path / "escaped_by_tar.txt").exists()
        assert not (sandbox["workspace"] / "escaped_by_tar.txt").exists()

    def test_absolute_member_blocked(self, sandbox, tmp_path):
        dest = tmp_path / "untar-here"
        out = file_processor.execute({
            "file_path": str(sandbox["evil_tar"]),
            "action": "extract",
            "destination": str(dest),
        })
        assert not (Path("/") / "absolute_in_tar.txt").exists()
        assert not Path("absolute_in_tar.txt").exists()


class TestArchiveDroppingSecrets:
    """A tar that carries id_rsa into the extract destination is the same
    secret-planting act as writing it there directly."""

    def test_tar_containing_id_rsa_refused(self, sandbox, tmp_path):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            info = tarfile.TarInfo("project/id_rsa")
            data = b"DECOY-KEY"
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        evil = sandbox["workspace"] / "keydrop.tar"
        evil.write_bytes(buf.getvalue())

        dest = tmp_path / "keydrop-here"
        out = file_processor.execute({
            "file_path": str(evil),
            "action": "extract",
            "destination": str(dest),
        })
        assert out.startswith("Refused:"), out
        assert not (dest / "project" / "id_rsa").exists()


class TestBenignArchiveStillWorks:
    def test_good_zip_extracts(self, sandbox, tmp_path):
        dest = tmp_path / "good-here"
        out = file_processor.execute({
            "file_path": str(sandbox["good_zip"]),
            "action": "extract",
            "destination": str(dest),
        })
        assert "Extracted" in out, out
        assert (dest / "docs" / "readme.txt").read_text(
            encoding="utf-8") == "hello"
        assert (dest / "data" / "numbers.csv").exists()

    def test_archive_listing_still_works(self, sandbox):
        out = file_processor.execute({
            "file_path": str(sandbox["good_zip"]),
            "action": "list",
        })
        assert "readme.txt" in out


class TestExtractionDestinationPolicy:
    def test_destination_in_system_area_refused(self, sandbox):
        out = file_processor.execute({
            "file_path": str(sandbox["good_zip"]),
            "action": "extract",
            "destination": r"C:\Windows\Temp\unzipped",
        })
        assert out.startswith("Refused:")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
