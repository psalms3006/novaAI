"""Every asset the desktop UI references must exist on disk.

A vendored three.module.js once imported a ./three.core.js that was never
vendored alongside it, so the Mind Map was dead in both the dev app and the
packaged build with only a 404 in the server log to show for it. The window is
now the desk/ui build in desk/static/ui; this checks what that build points at:
the page's script and stylesheet, and every font and icon the stylesheet loads.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "desk" / "static"
UI = STATIC / "ui"


def _referenced_assets() -> set[str]:
    refs: set[str] = set()
    html = (UI / "index.html").read_text(encoding="utf-8", errors="replace")
    refs |= {r[len("/static/"):] for r in re.findall(r'(?:src|href)="(/static/[^"]+)"', html)}
    for css in UI.glob("assets/*.css"):
        text = css.read_text(encoding="utf-8", errors="replace")
        for url in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", text):
            if url.startswith("data:") or url.startswith("#"):
                continue
            if url.startswith("/static/"):
                refs.add(url[len("/static/"):])
            else:
                refs.add((css.parent / url.split("?")[0].split("#")[0]).resolve().relative_to(STATIC.resolve()).as_posix())
    return refs


def test_static_dir_exists():
    assert UI.is_dir(), f"missing interface build: {UI} (run `npm run build` in desk/ui)"


def test_every_referenced_asset_exists():
    missing = sorted(r for r in _referenced_assets() if not (STATIC / r).exists())
    assert not missing, "UI references assets that do not exist: " + ", ".join(missing)


def test_at_least_some_references_were_found():
    """Guards the test itself -- a broken parser would pass vacuously."""
    refs = _referenced_assets()
    assert len(refs) >= 10, refs
    assert any(r.endswith(".js") for r in refs) and any(r.endswith(".woff2") for r in refs)
