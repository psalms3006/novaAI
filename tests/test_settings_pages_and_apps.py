"""The 2026-09-30 log: NOVA clicked through Settings fifteen times, could not
find 'ListItem: Accounts', had no scroll, and no way to list or uninstall
programs. These pin the fixes without opening real windows."""
import pytest

from actions import app_control as ac
from actions import computer_settings as cs


def test_typed_names_from_inspect_are_split():
    assert ac._split_typed("ListItem: Accounts", "") == ("Accounts", "ListItem")
    assert ac._split_typed("Button: OK", "") == ("OK", "Button")
    # an explicit kind wins over the prefix
    assert ac._split_typed("ListItem: Accounts", "Text") == ("Accounts", "Text")
    # ordinary names that happen to contain a colon are left alone
    assert ac._split_typed("Note: hello", "") == ("Note: hello", "")
    assert ac._split_typed("Your info", "") == ("Your info", "")


def test_scroll_is_a_registered_action():
    assert ac._ACTIONS["scroll"] is ac._act_scroll


def test_open_settings_maps_names_to_pages(monkeypatch):
    opened = []
    monkeypatch.setattr(cs.os, "startfile", lambda uri: opened.append(uri), raising=False)
    monkeypatch.setattr(cs.platform, "system", lambda: "Windows")
    assert "yourinfo" in cs._open_settings("accounts")
    assert "otherusers" in cs._open_settings("Other users")
    assert "appsfeatures" in cs._open_settings("installed apps")
    assert opened == ["ms-settings:yourinfo", "ms-settings:otherusers", "ms-settings:appsfeatures"]


def test_unknown_settings_page_opens_nothing(monkeypatch):
    opened = []
    monkeypatch.setattr(cs.os, "startfile", lambda uri: opened.append(uri), raising=False)
    msg = cs._open_settings("zzqx")
    assert "don't know" in msg and opened == []


def test_execute_routes_new_actions(monkeypatch):
    monkeypatch.setattr(cs, "_open_settings", lambda p: f"OPEN {p}")
    monkeypatch.setattr(cs, "_list_apps", lambda n: f"LIST {n}")
    monkeypatch.setattr(cs, "_uninstall_app", lambda n: f"UNINSTALL {n}")
    assert cs.execute({"action": "open_settings", "value": "wifi"}) == "OPEN wifi"
    assert cs.execute({"action": "list_apps", "value": "edge"}) == "LIST edge"
    assert cs.execute({"action": "uninstall_app", "value": "foo"}) == "UNINSTALL foo"


def test_uninstall_of_missing_program_does_nothing(monkeypatch):
    calls = []

    def fake(*argv, timeout=120):
        calls.append(argv)
        return 0, "No installed package found matching input criteria."
    monkeypatch.setattr(cs, "_winget", fake)
    assert "nothing to uninstall" in cs._uninstall_app("zz-nope")
    assert all(a[0] == "list" for a in calls)


def test_uninstall_requires_a_name():
    assert "which program" in cs._uninstall_app("  ")


def test_uninstall_always_asks_even_when_allowed():
    from desk import confirm
    assert confirm.irreversible("computer_settings", {"action": "uninstall_app", "value": "x"})
    assert not confirm.irreversible("computer_settings", {"action": "list_apps"})
    assert not confirm.irreversible("computer_settings", {"action": "open_settings"})


def test_permission_tables_classify_new_actions():
    from nova_core import permissions as p
    assert "uninstall_app" in p.ALWAYS_CONFIRM_ACTIONS
    assert "list_apps" in p.READ_ONLY_ACTIONS
    assert "scroll" in p.LOW_RISK_ACTIONS and "open_settings" in p.LOW_RISK_ACTIONS
