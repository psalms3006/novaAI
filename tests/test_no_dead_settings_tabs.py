"""Every settings tab in the markup must have a handler behind it.

This exists because the Account tab shipped as a button that did nothing when
clicked. A control that is visible and inert is worse than an absent one: it
tells the user a feature exists and then fails silently.
"""
from __future__ import annotations

import io
import re


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8").read()


def test_every_settings_tab_has_a_handler():
    html = _read("desk/static/index.html")
    js = _read("desk/static/app.js")

    tabs = re.findall(r'<button\s+data-tab="([a-z_]+)"', html)
    assert tabs, "no settings tabs found in index.html"

    body = js[js.index("const SETTINGS_TABS = {"):]
    # `async` is allowed: a panel that fetches its own data needs it, and the
    # pattern predates the first one that did.
    handlers = set(re.findall(r"^\s{2}(?:async\s+)?([a-z_]+)\(host\)", body, re.M))

    missing = [t for t in tabs if t not in handlers]
    assert not missing, f"settings tabs with no handler: {missing}"


def test_the_account_tab_is_backed_by_the_account_module():
    js = _read("desk/static/app.js")
    body = js[js.index("const SETTINGS_TABS = {"):]
    account = body[body.index("account(host)"):body.index("general(host)")]
    assert "NovaAccount" in account
    assert "renderPanel" in account


def test_account_script_is_loaded_by_the_shell():
    html = _read("desk/static/index.html")
    assert "/static/account.js" in html, "account.js is never loaded"
