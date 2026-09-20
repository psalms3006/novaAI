"""Where NOVA's runtime state is allowed to land.

The installer guarantees that user data never lives inside the install
directory, so an uninstall can remove the program without touching the
person's memories and a standard user can run NOVA from Program Files at all.
`nova.py` honours that with `_DATA_DIR`, and `nova_safety` has its own copy of
the rule with a comment explaining why.

Two subsystems did not:

* `task_manager.py` anchored its state file to `os.path.dirname(__file__)` —
  the install directory once frozen. `_save()` swallows every exception, so
  tasks would have failed to persist in silence.
* `core/goal_engine.py` used the bare relative path "goals.json", which
  resolves against whatever directory the shortcut happened to launch from.

In development both of those are the repository itself, which is how one
user's goals and tasks came to be tracked in git.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import nova_paths


def _resolve(path) -> Path:
    return Path(str(path)).resolve()


def test_the_data_directory_is_the_sandbox_the_suite_asked_for():
    """conftest.py sets NOVA_DATA_DIR before anything is imported."""
    assert os.environ.get("NOVA_DATA_DIR"), "conftest should have set this"
    assert _resolve(nova_paths.data_dir()) == _resolve(os.environ["NOVA_DATA_DIR"])


def test_an_explicit_data_directory_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path))
    assert _resolve(nova_paths.data_dir()) == _resolve(tmp_path)


def test_a_frozen_install_keeps_user_data_out_of_the_install_directory(monkeypatch, tmp_path):
    """Frozen, with no override, data belongs beside the user — not the exe."""
    monkeypatch.delenv("NOVA_DATA_DIR", raising=False)
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

    resolved = _resolve(nova_paths.data_dir())
    assert resolved == _resolve(tmp_path / "Roaming" / "NOVA")
    assert Path(os.getcwd()).resolve() not in resolved.parents
    assert resolved != Path(os.getcwd()).resolve()


def test_the_task_manager_keeps_its_state_in_the_data_directory():
    """Fails while TaskManager defaults to its own module directory."""
    from task_manager import TaskManager

    manager = TaskManager()
    assert _resolve(manager.path).parent == _resolve(nova_paths.data_dir()), (
        f"task state landed in {manager.path!r}"
    )


def test_the_goal_engine_keeps_goals_in_the_data_directory():
    """Fails while GoalEngine defaults to the bare relative 'goals.json'."""
    from core.goal_engine import GoalEngine

    engine = GoalEngine()
    assert _resolve(engine.store.path).parent == _resolve(nova_paths.data_dir()), (
        f"goals landed in {engine.store.path!r}"
    )


@pytest.mark.parametrize("name", ["nova_tasks_aios.json", "goals.json"])
def test_runtime_state_does_not_land_in_the_repository(name):
    """The concrete symptom: these were tracked files a dev run rewrote."""
    landed = _resolve(nova_paths.data_file(name))
    repo = Path(__file__).resolve().parents[1]
    assert repo not in landed.parents, f"{name} would be written into {repo}"
