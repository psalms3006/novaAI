"""The packaged app must never write into its own install directory.

Observed after a real silent install + uninstall: the install directory was
left holding data/nova_audit.ndjson, written there because the path is relative
and the frozen app's working directory is the install directory. Under
Program Files that directory is not writable by a standard user, so auditing
would silently disable itself — and it contradicts NOVA-Setup.iss's stated
guarantee that user data lives in the per-user APPDATA directory and
never inside the install directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import nova_safety
from orchestrator.audit_logger import AuditLogger


def _appdata_root() -> Path:
    base = os.getenv("APPDATA")
    return Path(base) / "NOVA" if base else Path.home() / ".nova"


def test_audit_ndjson_stays_relative_in_development():
    assert not getattr(sys, "frozen", False), "test assumes a source checkout"
    assert not AuditLogger.default_path().is_absolute()


def test_audit_ndjson_moves_to_app_data_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    p = AuditLogger.default_path()
    assert p.is_absolute()
    assert _appdata_root() in p.parents
    assert p.name == "nova_audit.ndjson"


def test_safety_runtime_dir_is_cwd_in_development():
    assert nova_safety._runtime_data_dir() == Path(".")


def test_safety_runtime_dir_moves_to_app_data_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    d = nova_safety._runtime_data_dir()
    assert d.is_absolute()
    assert d == _appdata_root()


def test_safety_files_are_not_bare_relative_names_when_frozen(monkeypatch):
    """Guards the shape of AUDIT_FILE / COST_FILE, which are module constants."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    root = nova_safety._runtime_data_dir()
    assert (root / "nova_audit.log").is_absolute()
    assert (root / "nova_cost.json").is_absolute()


def test_frozen_path_falls_back_when_appdata_is_missing(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    d = nova_safety._runtime_data_dir()
    assert d.is_absolute()
    assert d == Path.home() / ".nova"


def test_installer_declares_that_user_data_lives_outside_the_install_dir():
    iss = Path(__file__).resolve().parent.parent / "packaging" / "NOVA-Setup.iss"
    if not iss.exists():
        pytest.skip("installer script not present")
    text = iss.read_text(encoding="utf-8", errors="replace")
    assert "APPDATA" in text.upper()
