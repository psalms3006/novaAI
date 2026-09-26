"""The desktop window gets the new interface (desk/ui), wired to this run.

Checks what a user's window actually receives from GET /: the built page,
with the per-run token filled in, never cached, every asset it references
served with a type the webview will execute -- and nothing fetched from the
internet, because NOVA must work offline.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "desk" / "static" / "ui" / "index.html"


@pytest.fixture()
def client():
    from desk import bridge
    bridge.app.config["TESTING"] = True
    with bridge.app.test_client() as c:
        yield c, bridge


def test_the_build_is_committed():
    assert BUILD.is_file(), "desk/static/ui is missing: run `npm run build` in desk/ui"


def test_root_serves_the_new_ui_with_this_runs_token(client):
    c, bridge = client
    r = c.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert '<div id="root"></div>' in html, "not the desk/ui build"
    assert "__DESK_TOKEN__" not in html and "__DESK_VERSION__" not in html
    assert f'window.DESK_TOKEN = "{bridge.run_token}"' in html
    assert r.headers.get("Cache-Control") == "no-store"


def test_ambient_mode_is_the_same_page(client):
    c, _ = client
    assert '<div id="root"></div>' in c.get("/?mode=ambient").get_data(as_text=True)


def test_every_asset_the_page_references_is_served(client):
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    refs = re.findall(r'(?:src|href)="(/static/ui/[^"]+)"', html)
    assert any(r.endswith(".js") for r in refs) and any(r.endswith(".css") for r in refs)
    for ref in refs:
        r = c.get(ref)
        assert r.status_code == 200, ref
        kind = r.headers.get("Content-Type", "")
        if ref.endswith(".js"):
            assert "javascript" in kind, f"{ref} served as {kind!r}; a module script with this type will not run"
        if ref.endswith(".css"):
            assert "css" in kind, f"{ref} served as {kind!r}"


def test_the_page_loads_nothing_from_the_internet():
    html = BUILD.read_text(encoding="utf-8")
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not external, f"external resources break offline use: {external}"
    css = "".join(p.read_text(encoding="utf-8") for p in BUILD.parent.glob("assets/*.css"))
    assert not re.findall(r"@import\s+url\(\s*['\"]?https?://", css)
    assert not re.findall(r"url\(\s*['\"]?https?://", css), "the stylesheet fetches from the internet"
