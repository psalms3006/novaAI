"""Installing an extension in a way that can be undone.

The states exist to stop one question being answered by a different one.
"It imported without error" is not "it is safe", and "it was safe last week"
is not "this version is safe". So an extension climbs:

    UNTRUSTED -> INSPECTED -> TESTING -> VERIFIED -> ENABLED

and each step has to be earned. Nothing may skip from INSPECTED to ENABLED,
because that is exactly the shortcut a plausible-looking package would
benefit from.

Two properties matter more than the rest:

* **Every install is reversible.** The previous version is kept before the
  new one lands, so a bad upgrade is a restore rather than a reinstall from
  wherever the user originally found it.
* **A new version is never installed because it exists.** Upgrading is a
  decision. Pinning makes it not even a question.
"""
from __future__ import annotations

import json

import pytest

from nova_extensions.registry import (
    ExtensionRegistry,
    IllegalTransition,
    TrustState,
)


def _source(tmp_path, name="weather", version="1.0.0", body="X = 1\n"):
    root = tmp_path / f"src-{name}-{version}"
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text(body, encoding="utf-8")
    (root / "nova_extension.json").write_text(json.dumps({
        "name": name, "version": version, "entrypoint": "main.py",
        "permissions": ["network"],
    }), encoding="utf-8")
    return root


@pytest.fixture
def registry(tmp_path):
    return ExtensionRegistry(root=str(tmp_path / "extensions"))


# ── the ladder ──────────────────────────────────────────────────────────────

def test_a_new_extension_starts_untrusted(registry, tmp_path):
    ext = registry.add(_source(tmp_path))
    assert ext.state is TrustState.UNTRUSTED


def test_it_climbs_one_step_at_a_time(registry, tmp_path):
    ext = registry.add(_source(tmp_path))
    registry.advance(ext.name, TrustState.INSPECTED, "read without running it")
    registry.advance(ext.name, TrustState.TESTING, "trial run started")
    registry.advance(ext.name, TrustState.VERIFIED, "trial passed")
    registry.advance(ext.name, TrustState.ENABLED, "the user said yes")
    assert registry.get(ext.name).state is TrustState.ENABLED


def test_it_cannot_skip_to_enabled(registry, tmp_path):
    """The shortcut a plausible-looking package would most like to take."""
    ext = registry.add(_source(tmp_path))
    registry.advance(ext.name, TrustState.INSPECTED, "read it")
    with pytest.raises(IllegalTransition):
        registry.advance(ext.name, TrustState.ENABLED, "seems fine")


def test_compiling_is_not_a_reason_to_trust(registry, tmp_path):
    ext = registry.add(_source(tmp_path))
    registry.advance(ext.name, TrustState.INSPECTED, "read it")
    with pytest.raises(IllegalTransition):
        registry.advance(ext.name, TrustState.VERIFIED, "it imported fine")


def test_every_step_records_why(registry, tmp_path):
    ext = registry.add(_source(tmp_path))
    registry.advance(ext.name, TrustState.INSPECTED, "no eval, no subprocess")
    history = registry.get(ext.name).history
    assert any("eval" in h["reason"] for h in history), history


# ── quarantine ──────────────────────────────────────────────────────────────

def test_anything_can_be_quarantined(registry, tmp_path):
    ext = registry.add(_source(tmp_path))
    registry.advance(ext.name, TrustState.INSPECTED, "read it")
    registry.quarantine(ext.name, "it phoned home during the trial")
    assert registry.get(ext.name).state is TrustState.QUARANTINED


def test_a_quarantined_extension_cannot_simply_be_enabled(registry, tmp_path):
    ext = registry.add(_source(tmp_path))
    registry.quarantine(ext.name, "suspicious")
    with pytest.raises(IllegalTransition):
        registry.advance(ext.name, TrustState.ENABLED, "looks alright now")


def test_clearing_quarantine_starts_the_climb_again(registry, tmp_path):
    ext = registry.add(_source(tmp_path))
    registry.quarantine(ext.name, "suspicious")
    registry.clear_quarantine(ext.name, "the user vouched for it")
    assert registry.get(ext.name).state is TrustState.UNTRUSTED, (
        "clearing quarantine restored trust it had not earned"
    )


# ── installing and undoing ──────────────────────────────────────────────────

def test_installing_puts_the_files_where_nova_can_find_them(registry, tmp_path):
    ext = registry.install(_source(tmp_path))
    installed = registry.path_for(ext.name)
    assert (installed / "main.py").exists()
    assert (installed / "nova_extension.json").exists()


def test_upgrading_keeps_the_version_it_replaced(registry, tmp_path):
    registry.install(_source(tmp_path, version="1.0.0", body="X = 1\n"))
    registry.install(_source(tmp_path, version="2.0.0", body="X = 2\n"))

    assert registry.get("weather").version == "2.0.0"
    assert "1.0.0" in registry.versions("weather")


def test_rolling_back_restores_the_previous_version(registry, tmp_path):
    registry.install(_source(tmp_path, version="1.0.0", body="X = 1\n"))
    registry.install(_source(tmp_path, version="2.0.0", body="X = 2\n"))

    assert registry.rollback("weather") is True
    assert registry.get("weather").version == "1.0.0"
    assert "X = 1" in (registry.path_for("weather") / "main.py").read_text()


def test_rolling_back_the_first_version_is_refused_not_guessed(registry, tmp_path):
    registry.install(_source(tmp_path, version="1.0.0"))
    assert registry.rollback("weather") is False
    assert registry.get("weather").version == "1.0.0"


def test_a_failed_install_leaves_the_working_version_in_place(registry, tmp_path):
    registry.install(_source(tmp_path, version="1.0.0", body="X = 1\n"))

    broken = _source(tmp_path, version="2.0.0")
    (broken / "nova_extension.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(Exception):
        registry.install(broken)

    assert registry.get("weather").version == "1.0.0", (
        "a failed install destroyed the version that was working"
    )
    assert "X = 1" in (registry.path_for("weather") / "main.py").read_text()


def test_uninstalling_removes_it(registry, tmp_path):
    registry.install(_source(tmp_path))
    registry.uninstall("weather")
    assert registry.get("weather") is None
    assert not registry.path_for("weather").exists()


# ── versions ────────────────────────────────────────────────────────────────

def test_a_newer_version_is_not_installed_just_because_it_exists(registry, tmp_path):
    registry.install(_source(tmp_path, version="1.0.0"))
    registry.note_available("weather", "1.5.0")

    ext = registry.get("weather")
    assert ext.version == "1.0.0", "it upgraded itself"
    assert ext.available == "1.5.0"
    assert registry.upgradable("weather") is True


def test_a_pinned_extension_reports_no_upgrade(registry, tmp_path):
    registry.install(_source(tmp_path, version="1.0.0"))
    registry.pin("weather")
    registry.note_available("weather", "1.5.0")
    assert registry.upgradable("weather") is False


def test_a_pin_can_be_lifted(registry, tmp_path):
    registry.install(_source(tmp_path, version="1.0.0"))
    registry.pin("weather")
    registry.unpin("weather")
    registry.note_available("weather", "1.5.0")
    assert registry.upgradable("weather") is True


# ── durability ──────────────────────────────────────────────────────────────

def test_the_register_survives_a_restart(registry, tmp_path):
    registry.install(_source(tmp_path))
    registry.advance("weather", TrustState.INSPECTED, "read it")

    reopened = ExtensionRegistry(root=str(tmp_path / "extensions"))
    ext = reopened.get("weather")
    assert ext is not None
    assert ext.state is TrustState.INSPECTED
    assert ext.history, "the reasoning was lost"


def test_an_unknown_extension_is_answered_not_raised(registry):
    assert registry.get("nope") is None
    assert registry.rollback("nope") is False
    assert registry.upgradable("nope") is False


def test_what_nova_says_about_it_is_accurate(registry, tmp_path):
    registry.install(_source(tmp_path, version="1.0.0"))
    registry.note_available("weather", "1.5.0")
    said = registry.describe("weather")
    assert "weather" in said
    assert "1.0.0" in said
    assert "untrusted" in said.lower() or "not" in said.lower()
