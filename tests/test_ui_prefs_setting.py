"""The desktop window's look is a real, persisted, validated setting.

The window runs in pywebview's private mode, which wipes browser storage at
every launch, so a theme kept in localStorage would silently reset each time
NOVA starts. `ui_prefs` keeps it in settings.json instead -- and because the
page writes it, every field is checked against a whitelist: an unknown key or
an out-of-range value is dropped, never stored.
"""
from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(tmp_path / "roaming" / "NOVA"))
    import desk.settings as mod
    return importlib.reload(mod)


def test_defaults_to_empty_prefs(settings):
    assert settings.all()["ui_prefs"] == {}


def test_valid_prefs_persist_to_disk(settings):
    settings.set_many({"ui_prefs": {"theme_id": "pearl", "glass_blur": 20, "ambient_motion": False}})
    on_disk = json.loads(settings.settings_path().read_text(encoding="utf-8"))
    assert on_disk["ui_prefs"] == {"theme_id": "pearl", "glass_blur": 20, "ambient_motion": False}
    assert settings.all()["ui_prefs"]["theme_id"] == "pearl"


def test_updates_merge_key_by_key(settings):
    settings.set_many({"ui_prefs": {"theme_id": "obsidian", "quality": "balanced"}})
    settings.set_many({"ui_prefs": {"quality": "performance"}})
    assert settings.all()["ui_prefs"] == {"theme_id": "obsidian", "quality": "performance"}


@pytest.mark.parametrize(
    "bad",
    [
        {"theme_id": "hot-pink"},          # not a theme
        {"glass_blur": 400},               # out of range
        {"glass_opacity": "76"},           # wrong type
        {"ambient_motion": "yes"},         # not a bool
        {"specular": True},                # bool is not a number here
        {"evil": "<script>"},              # unknown key
    ],
)
def test_invalid_values_are_dropped(settings, bad):
    settings.set_many({"ui_prefs": {"theme_id": "titanium"}})
    settings.set_many({"ui_prefs": bad})
    assert settings.all()["ui_prefs"] == {"theme_id": "titanium"}


def test_a_non_dict_update_is_ignored(settings):
    settings.set_many({"ui_prefs": {"theme_id": "titanium"}})
    settings.set_many({"ui_prefs": "pearl"})
    assert settings.all()["ui_prefs"] == {"theme_id": "titanium"}


def test_a_hand_edited_file_is_cleaned_on_read(settings):
    path = settings.settings_path()
    path.write_text(json.dumps({"ui_prefs": {"theme_id": "pearl", "junk": 1, "glass_blur": -5}}), encoding="utf-8")
    assert settings.all()["ui_prefs"] == {"theme_id": "pearl"}
