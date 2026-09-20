"""Does the built bundle actually contain the source we have?

Every existing packaging test is a static assertion about the spec file --
that it lists a hidden import, that a data file is not turned into a folder.
Not one compares the artifact to the source, which is why this went unnoticed:

* `dist/NOVADesktop2` and `dist_stage/NOVADesktop2` were 10 commits behind
  HEAD, and their copy of `desk/static/app.js` hashed to a value that appears
  in no commit at all -- built from a dirty working tree.
* `nova_identity` and `nova_self`, two whole packages, were absent from the
  PYZ table of contents. A packaged smoke test would have exercised code from
  before either existed and passed.

These tests skip when no bundle is present, so a source-only checkout is not
punished for it. When a bundle IS present they are the difference between
"the build succeeded" and "the build contains this code".
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "dist" / "NOVADesktop2"
INTERNAL = BUNDLE / "_internal"


pytestmark = pytest.mark.skipif(
    not BUNDLE.is_dir(), reason="no dist/NOVADesktop2 bundle to check"
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_bundled_frontend_matches_the_source():
    """An uncompressed data file, so this compares content, not module names."""
    source = REPO / "desk" / "static" / "app.js"
    packaged = INTERNAL / "desk" / "static" / "app.js"
    if not packaged.exists():
        pytest.skip("frontend not present in this bundle")

    assert _digest(packaged) == _digest(source), (
        f"the bundled app.js is not the one in this checkout "
        f"({packaged.stat().st_size} bytes vs {source.stat().st_size}). "
        f"The bundle is stale -- rebuild with --clean."
    )


def test_every_top_level_nova_package_reaches_the_bundle():
    """Pure Python lives in the PYZ, whose module names are stored as plain text.

    A package absent here is absent from the app, however healthy the build
    log looked.
    """
    toc = REPO / "build" / "nova_desktop" / "PYZ-00.toc"
    if not toc.exists():
        pytest.skip("no PYZ table of contents from this build")

    listed = toc.read_text(encoding="utf-8", errors="replace")

    expected = sorted(
        p.name for p in REPO.iterdir()
        if p.is_dir() and p.name.startswith("nova_")
        and (p / "__init__.py").exists()
        and p.name not in {"nova_cloud", "nova_embedder"}   # server-side / model data
    )
    missing = [name for name in expected if name not in listed]

    assert missing == [], (
        f"packages absent from the build: {missing}. They are reached by "
        f"function-local imports, so PyInstaller cannot always see them -- "
        f"add them to the collect_submodules sweep in the spec."
    )


def test_the_bundle_records_which_commit_it_came_from():
    info = INTERNAL / "BUILDINFO.json"
    if not info.exists():
        pytest.skip("bundle predates the build stamp")

    stamp = json.loads(info.read_text(encoding="utf-8"))
    assert stamp.get("commit") and stamp["commit"] != "unknown", stamp
    assert stamp.get("clean_tree") is True, (
        f"this bundle was built from a dirty tree, so it corresponds to no "
        f"commit. Uncommitted at build time: {stamp.get('dirty_files')}"
    )


def test_the_bundle_is_not_behind_the_current_checkout():
    """A bundle from an older commit is a bundle that will lie to a smoke test."""
    info = INTERNAL / "BUILDINFO.json"
    if not info.exists():
        pytest.skip("bundle predates the build stamp")

    built = json.loads(info.read_text(encoding="utf-8")).get("commit", "")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO,
        capture_output=True, text=True,
    ).stdout.strip()

    if not head or built == "unknown":
        pytest.skip("cannot determine commits")

    behind = subprocess.run(
        ["git", "rev-list", "--count", f"{built}..{head}"], cwd=REPO,
        capture_output=True, text=True,
    ).stdout.strip()

    assert behind == "0", (
        f"the bundle is {behind} commit(s) behind HEAD (built {built[:8]}, "
        f"HEAD {head[:8]}). Rebuild before trusting any packaged test."
    )
