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

They deliberately hold a bundle only to the commit it *claims*, and skip when
it is behind HEAD. Source moves ahead of the last build constantly, and a
suite that goes red for that teaches people to ignore it.

The cost of that choice, stated plainly: a skip reads as a pass, so these
cannot by themselves stop someone running a packaged smoke test against an old
bundle. What they do guarantee is the failure that actually occurred — a
bundle asserting a commit while missing two of its packages and carrying a
frontend from no commit at all. After a rebuild the gate opens and every
check runs for real.
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


def _built_commit() -> str:
    info = INTERNAL / "BUILDINFO.json"
    if not info.exists():
        return ""
    try:
        return json.loads(info.read_text(encoding="utf-8")).get("commit", "")
    except Exception:
        return ""


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
    ).stdout.strip()


def _require_a_bundle_claiming_to_be_current():
    """Only hold a bundle to the source it says it was built from.

    Source moves ahead of the last build constantly during development, and a
    suite that goes red for that teaches people to ignore it. What must never
    happen is the opposite: a bundle that claims a commit and does not contain
    it, which is how a packaged smoke test comes to pass against code from
    before the fix it is supposedly testing.
    """
    built, head = _built_commit(), _head()
    if not built or not head:
        pytest.skip("cannot determine which commit this bundle came from")
    if built != head:
        behind = subprocess.run(
            ["git", "rev-list", "--count", f"{built}..{head}"], cwd=REPO,
            capture_output=True, text=True,
        ).stdout.strip() or "?"
        pytest.skip(
            f"bundle is from {built[:8]}, {behind} commit(s) behind HEAD — "
            f"rebuild before trusting any packaged result"
        )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_bundled_frontend_matches_the_source():
    """An uncompressed data file, so this compares content, not module names."""
    _require_a_bundle_claiming_to_be_current()
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
    _require_a_bundle_claiming_to_be_current()
    toc = REPO / "build" / "nova_desktop" / "PYZ-00.toc"
    if not toc.exists():
        pytest.skip("no PYZ table of contents from this build")

    listed = toc.read_text(encoding="utf-8", errors="replace")

    packages = sorted(
        p.name for p in REPO.iterdir()
        if p.is_dir() and p.name.startswith("nova_")
        and (p / "__init__.py").exists()
        and p.name not in {"nova_cloud", "nova_embedder"}   # server-side / model data
    )

    # Top-level modules too, not only packages. nova_paths and nova_proactive
    # are single files that the app imports at startup; checking directories
    # alone would have declared a bundle healthy while it was missing them.
    modules = sorted(
        p.stem for p in REPO.glob("nova_*.py")
        if p.is_file() and not p.stem.endswith("_test")
    )

    expected = packages + modules
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
