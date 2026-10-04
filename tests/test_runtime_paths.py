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


def test_audit_ndjson_follows_the_one_data_directory_rule():
    """The audit log used to carry its own copy of the path rule.

    It resolved to a relative "data/" directory in development, which meant
    the repository -- the same class of mistake that had one user's memories
    and tasks tracked in git -- and it ignored NOVA_DATA_DIR, so the sandbox
    the suite sets up did not contain it. It now asks nova_paths, which is
    the single place that decides.
    """
    import nova_paths

    resolved = AuditLogger.default_path()
    assert resolved.name == "nova_audit.ndjson"
    assert str(resolved).startswith(str(nova_paths.data_dir())), (
        f"audit log at {resolved}, data dir is {nova_paths.data_dir()}"
    )


def test_audit_ndjson_moves_to_app_data_when_frozen(monkeypatch):
    """The guarantee that still matters: never inside the install directory.

    NOVA_DATA_DIR is cleared here because the suite sets it, and it
    deliberately wins over the frozen default.
    """
    monkeypatch.delenv("NOVA_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    p = AuditLogger.default_path()
    assert p.is_absolute()
    assert _appdata_root() in p.parents
    assert p.name == "nova_audit.ndjson"


def test_safety_runtime_dir_follows_the_one_data_directory_rule():
    """nova_safety used to carry its own copy of the frozen/dev rule.

    Because it ignored NOVA_DATA_DIR, the suite's sandbox did not contain it
    and running the tests wrote confirmation records into the developer's real
    audit log. That was found by reading a user's audit trail after an
    incident and finding test fixtures interleaved with the events under
    investigation.
    """
    import nova_paths

    assert nova_safety._runtime_data_dir() == nova_paths.data_dir()


def test_safety_runtime_dir_moves_to_app_data_when_frozen(monkeypatch):
    """The guarantee that still matters: never the install directory.

    NOVA_DATA_DIR is cleared because the suite sets it and it deliberately
    wins over the frozen default.
    """
    monkeypatch.delenv("NOVA_DATA_DIR", raising=False)
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
    monkeypatch.delenv("NOVA_DATA_DIR", raising=False)
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


# ── data locations must not be hardcoded to one developer's machine ──────────

def test_zim_path_is_derived_not_hardcoded():
    import nova
    assert "project-nova/data/zim" not in nova.ZIM_DATA_PATH.replace("\\", "/").lower() or \
        Path(nova.ZIM_DATA_PATH).is_absolute()
    # It must resolve relative to the installed code, not a fixed home folder.
    assert Path(nova.ZIM_DATA_PATH).is_absolute()


def test_zim_path_follows_the_repo_in_development():
    import nova
    repo = Path(nova.__file__).resolve().parent
    assert Path(nova.ZIM_DATA_PATH) == repo / "data" / "zim"


def test_maps_path_follows_the_repo_in_development():
    import nova
    repo = Path(nova.__file__).resolve().parent
    assert Path(nova.OFFLINE_MAPS_PATH) == repo / "data" / "maps"


def test_zim_path_is_overridable_by_env(monkeypatch):
    monkeypatch.setenv("NOVA_ZIM_DIR", r"D:\somewhere\zim")
    import nova
    assert nova._zim_data_path() == r"D:\somewhere\zim"


def test_zim_path_moves_to_app_data_when_frozen(monkeypatch):
    import nova
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    p = Path(nova._zim_data_path())
    assert _appdata_root() in p.parents


def test_offline_knowledge_downloads_go_to_app_data_when_frozen(monkeypatch):
    """Multi-GB user downloads must survive uninstall/upgrade."""
    from nova_intelligence.offline_knowledge import OfflineKnowledgeManager
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    p = OfflineKnowledgeManager.default_data_path()
    assert _appdata_root() in p.parents


def test_dev_agent_projects_dir_actually_exists():
    """The old value resolved to C:/Users/<u>/Users/Lenovo/project-nova."""
    import actions.dev_agent as da
    assert da.PROJECTS_DIR.exists()
