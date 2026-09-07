"""Every asset the desktop UI references must exist on disk.

The vendored three.module.js (r174) imports ./three.core.js, which was never
vendored alongside it. The import failed at load time, so the whole Three.js
module — and with it the Mind Map view — was dead in both the dev app and the
packaged build, with only a 404 in the server log to show for it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "desk" / "static"


def _referenced_assets() -> set[str]:
    refs: set[str] = set()

    html = (STATIC / "index.html").read_text(encoding="utf-8", errors="replace")
    refs |= {
        r[len("/static/"):]
        for r in re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    }

    for js in STATIC.rglob("*.js"):
        txt = js.read_text(encoding="utf-8", errors="replace")
        for spec in re.findall(r"""from\s+['"]([^'"]+)['"]""", txt):
            if spec.startswith("."):
                try:
                    refs.add((js.parent / spec).resolve()
                             .relative_to(STATIC.resolve()).as_posix())
                except ValueError:
                    pass
            elif spec.startswith("/static/"):
                refs.add(spec[len("/static/"):])
    return refs


def test_static_dir_exists():
    assert STATIC.is_dir(), f"missing SPA directory: {STATIC}"


def test_every_referenced_asset_exists():
    missing = sorted(r for r in _referenced_assets() if not (STATIC / r).exists())
    assert not missing, "UI references assets that do not exist: " + ", ".join(missing)


def test_at_least_some_references_were_found():
    """Guards the test itself — a broken parser would pass vacuously."""
    assert len(_referenced_assets()) >= 10


def test_three_core_is_vendored_next_to_three_module():
    """three.module.js is useless without its split-out core."""
    three = STATIC / "vendor" / "three"
    if not (three / "three.module.js").exists():
        pytest.skip("three.js is not vendored in this checkout")
    assert (three / "three.core.js").exists(), (
        "three.module.js imports ./three.core.js — vendor it too"
    )
