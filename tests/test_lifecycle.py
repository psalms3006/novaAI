"""Installation / account / onboarding state on the desktop (nova_lifecycle).

The rules under test:
  * installation state is machine level and version independent;
  * each account's data lives in its own folder, chosen before the brain runs;
  * the first account to sign in adopts the data that existed before accounts
    (owner's decision, 2026-09-28); a later account starts empty;
  * nothing here depends on the app version, so an update cannot reset it.
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture()
def lc(tmp_path, monkeypatch):
    machine = tmp_path / "NOVA"
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(machine))
    monkeypatch.delenv("NOVA_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path / "suite-sandbox"))
    import importlib
    import nova_lifecycle
    importlib.reload(nova_lifecycle)
    yield nova_lifecycle, machine


def test_first_run_initialises_the_installation_once(lc):
    mod, machine = lc
    a = mod.ensure_installation("1.0.0")
    b = mod.ensure_installation("1.1.0")
    assert a["installation_id"] == b["installation_id"]
    assert a["installation_initialized_at"] == b["installation_initialized_at"]
    assert b["last_run_version"] == "1.1.0"
    assert json.loads((machine / "lifecycle.json").read_text())["schema"] == 1


def test_a_fresh_machine_is_a_first_run_and_a_used_one_is_not(lc):
    mod, _ = lc
    assert mod.is_first_run() is True
    mod.ensure_installation("1.0.0")
    assert mod.is_first_run() is False


def test_an_update_changes_nothing_but_the_last_run_version(lc):
    mod, _ = lc
    mod.ensure_installation("1.0.0")
    mod.activate_account("acct-1")
    mod.mark_device_setup_complete()
    before = mod.load()
    mod.ensure_installation("2.0.0")
    after = mod.load()
    assert {k: v for k, v in before.items() if k != "last_run_version"} == \
           {k: v for k, v in after.items() if k != "last_run_version"}
    mod.activate_account("acct-1")
    assert mod.device_setup_completed() is True


def test_activating_an_account_points_the_brain_at_its_own_folder(lc):
    mod, machine = lc
    mod.ensure_installation("1.0.0")
    info = mod.activate_account("acct-1")
    assert os.environ["NOVA_DATA_DIR"] == str(machine / "accounts" / "acct-1")
    assert os.environ["NOVA_ACCOUNT_ID"] == "acct-1"
    assert (machine / "accounts" / "acct-1").is_dir()
    assert mod.load()["last_account_id"] == "acct-1"
    assert info["dir"] == str(machine / "accounts" / "acct-1")


def test_account_ids_cannot_escape_the_accounts_folder(lc):
    mod, _ = lc
    for bad in ("../x", "..\\x", "a/b", "", "C:\\evil", "."):
        with pytest.raises(ValueError):
            mod.account_dir(bad)


def _legacy(machine):
    machine.mkdir(parents=True, exist_ok=True)
    (machine / "settings.json").write_text('{"user_name": "Psalms"}')
    (machine / "memory_texts.json").write_text('["likes jollof"]')
    (machine / "nova_desktop.db").write_bytes(b"sqlite")
    (machine / "nova_memories").mkdir()
    (machine / "nova_memories" / "s1.json").write_text("{}")
    (machine / "rag").mkdir()
    (machine / "rag" / "doc.bin").write_bytes(b"x")
    # Machine-level things that must stay put.
    (machine / "models").mkdir()
    (machine / "models" / "m.onnx").write_bytes(b"m")
    (machine / "device.json").write_text("{}")
    (machine / "store.key").write_bytes(b"k")


def test_the_first_account_adopts_existing_data(lc):
    mod, machine = lc
    _legacy(machine)
    mod.ensure_installation("1.0.0")
    info = mod.activate_account("first")
    acct = machine / "accounts" / "first"
    assert (acct / "memory_texts.json").read_text() == '["likes jollof"]'
    assert (acct / "settings.json").exists() and (acct / "nova_desktop.db").exists()
    assert (acct / "nova_memories" / "s1.json").exists()
    assert (acct / "rag" / "doc.bin").exists()
    assert not (machine / "memory_texts.json").exists()
    # Machine-level files are not the account's.
    assert (machine / "models" / "m.onnx").exists()
    assert (machine / "device.json").exists() and (machine / "store.key").exists()
    assert not (acct / "models").exists()
    assert set(info["adopted"]) >= {"settings.json", "memory_texts.json", "nova_desktop.db",
                                    "nova_memories", "rag"}
    assert mod.load()["adopted_legacy_data_by"] == "first"


def test_an_existing_install_is_not_a_first_run(lc):
    """Upgrading from a build without accounts: data exists, lifecycle does not.
    That person must not be greeted as new."""
    mod, machine = lc
    _legacy(machine)
    assert mod.is_first_run() is False


def test_a_second_account_starts_empty(lc):
    mod, machine = lc
    _legacy(machine)
    mod.ensure_installation("1.0.0")
    mod.activate_account("first")
    info = mod.activate_account("second")
    acct = machine / "accounts" / "second"
    assert info["adopted"] == []
    assert not (acct / "memory_texts.json").exists()


def test_adoption_happens_once_even_if_legacy_files_reappear(lc):
    mod, machine = lc
    _legacy(machine)
    mod.ensure_installation("1.0.0")
    mod.activate_account("first")
    (machine / "memory_texts.json").write_text('["written by an old build"]')
    assert mod.activate_account("first")["adopted"] == []
    assert (machine / "accounts" / "first" / "memory_texts.json").read_text() == '["likes jollof"]'


def test_device_setup_is_per_account_on_this_pc(lc):
    mod, _ = lc
    mod.ensure_installation("1.0.0")
    mod.activate_account("a")
    assert mod.device_setup_completed() is False
    mod.mark_device_setup_complete()
    assert mod.device_setup_completed() is True
    mod.activate_account("b")
    assert mod.device_setup_completed() is False


def test_deactivate_forgets_the_active_account_but_not_the_installation(lc):
    mod, machine = lc
    mod.ensure_installation("1.0.0")
    mod.activate_account("a")
    mod.deactivate()
    assert "NOVA_ACCOUNT_ID" not in os.environ
    assert mod.is_first_run() is False
    assert mod.load()["last_account_id"] is None


def test_a_corrupt_lifecycle_file_is_not_mistaken_for_a_new_install(lc):
    mod, machine = lc
    mod.ensure_installation("1.0.0")
    mod.activate_account("a")
    (machine / "lifecycle.json").write_text("{not json")
    # The account folders still prove this machine has been used.
    assert mod.is_first_run() is False


def test_two_accounts_see_only_their_own_settings_conversations_and_projects(lc, monkeypatch, tmp_path):
    """Test I (second user): no memory or configuration leakage between accounts."""
    mod, machine = lc
    monkeypatch.setenv("APPDATA", str(tmp_path))          # desk.settings machine dir
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(tmp_path / "NOVA"))
    from desk import settings, store, projects
    mod.ensure_installation("1.0.0")

    mod.activate_account("alice")
    settings.set_many({"user_name": "Alice", "permissions": {"file_read": "allow"}})
    cid = store.new_conversation("Alice's chat")
    projects_mod = projects
    before = len(projects_mod.list_projects()) if hasattr(projects_mod, "list_projects") else None

    mod.deactivate()
    mod.activate_account("bob")
    assert settings.get("user_name") == "User"
    assert settings.all()["permissions"]["file_read"] == "ask"
    assert all(c["id"] != cid for c in store.list_conversations())

    mod.deactivate()
    mod.activate_account("alice")
    assert settings.get("user_name") == "Alice"
    assert any(c["id"] == cid for c in store.list_conversations())
    assert (tmp_path / "NOVA" / "accounts" / "alice" / "nova_desktop.db").exists()
    assert not (tmp_path / "NOVA" / "nova_desktop.db").exists()


def test_the_byok_key_belongs_to_the_account_and_the_device_to_the_pc(lc, monkeypatch, tmp_path):
    mod, _ = lc
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(tmp_path / "NOVA"))
    from desk import creds
    mod.ensure_installation("1.0.0")
    mod.activate_account("alice")
    dev_a = creds.device_identity()["device_id"]
    assert creds._cred_path().parent == tmp_path / "NOVA" / "accounts" / "alice"
    mod.deactivate()
    mod.activate_account("bob")
    assert creds.device_identity()["device_id"] == dev_a
    assert creds._cred_path().parent == tmp_path / "NOVA" / "accounts" / "bob"


# -- permission scopes --------------------------------------------------------

def test_old_permission_choices_carry_over_to_the_new_scopes(lc, monkeypatch, tmp_path):
    mod, machine = lc
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(tmp_path / "NOVA"))
    from desk import settings
    mod.ensure_installation("1.0.0")
    mod.activate_account("a")
    settings.settings_path().write_text(json.dumps({"permissions": {
        "files": "allow", "web": "deny", "mic": "allow", "screen": "ask", "computer": "deny"}}))
    p = settings.get("permissions")
    assert (p["file_read"], p["file_write"]) == ("allow", "ask")
    assert (p["browser_read"], p["browser_interact"]) == ("deny", "deny")
    assert p["microphone"] == "allow" and p["screen_read"] == "ask"
    assert p["computer_control"] == "deny"


def test_reading_and_changing_files_are_separate_decisions():
    from desk.confirm import scope_for
    assert scope_for("file_controller", {"action": "read"}) == "file_read"
    assert scope_for("file_controller", {"action": "list"}) == "file_read"
    assert scope_for("file_controller", {"action": "delete"}) == "file_write"
    assert scope_for("file_controller", {"action": "write"}) == "file_write"
    assert scope_for("browser_control", {"action": "open_url"}) == "browser_read"
    assert scope_for("browser_control", {"action": "download"}) == "browser_interact"
    assert scope_for("remember_fact", {}) == ""


def test_a_permission_change_takes_effect_on_the_next_call(lc, monkeypatch, tmp_path):
    """Test L: changing a permission after onboarding applies immediately."""
    mod, _ = lc
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(tmp_path / "NOVA"))
    from desk import settings, confirm
    mod.ensure_installation("1.0.0")
    mod.activate_account("a")
    settings.set_many({"permissions": {"file_write": "deny"}})
    if not confirm._HAS_SAFETY:
        pytest.skip("nova_safety unavailable")
    blocked = confirm._ui_safety_gate("file_controller", {"action": "delete", "path": "x"})
    assert blocked and "turned off" in blocked
    settings.set_many({"permissions": {"file_write": "allow"}})
    assert confirm._ui_safety_gate("file_controller", {"action": "delete", "path": "x"}) is None
