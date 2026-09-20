"""Trying an extension without letting it into NOVA.

Inspection is static and therefore safe but limited: it can see that a package
imports `socket`, not whether it works. At some point something has to run,
and the question is what it can reach when it does.

The properties worth having on Windows, where there is no seccomp and no
namespaces to hand:

* it runs in a **separate process**, so a crash, a hang or a memory bomb
  cannot take NOVA down with it, and nothing it does touches NOVA's own state
* it gets a **scrubbed environment**, so the user's API keys are not sitting
  in `os.environ` for anything that decides to look
* it runs in a **temporary directory**, so the obvious relative-path write
  lands somewhere disposable
* it is **killed on a timeout**, so `while True` costs seconds rather than a
  session

What this is not: containment. A determined package can still open a socket
or write an absolute path, because Windows offers no cheap way to stop it.
That limit is asserted below rather than left for someone to assume away --
`describe_isolation()` has to say so, because an honest "this is isolation,
not a sandbox" is worth more than a reassuring name.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nova_extensions.trial import TrialResult, describe_isolation, try_extension


def _pkg(tmp_path, body: str, name="cand"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "entry.py").write_text(body, encoding="utf-8")
    return root


# ── the process boundary ────────────────────────────────────────────────────

def test_it_runs_in_a_separate_process(tmp_path):
    root = _pkg(tmp_path, "import os\nprint('PID', os.getpid())\n")
    result = try_extension(root, "entry.py")

    assert result.ok, result.stderr
    printed = result.stdout.split()
    assert printed[0] == "PID"
    assert int(printed[1]) != os.getpid(), (
        "the candidate ran inside NOVA's own process"
    )


def test_a_crash_is_reported_not_raised(tmp_path):
    root = _pkg(tmp_path, "raise SystemExit('boom')\n")
    result = try_extension(root, "entry.py")
    assert result.ok is False
    assert result.returncode != 0


def test_a_hang_is_killed(tmp_path):
    root = _pkg(tmp_path, "while True:\n    pass\n")
    result = try_extension(root, "entry.py", timeout_seconds=2)

    assert result.timed_out is True
    assert result.ok is False
    # The behaviour, not a particular phrasing: the user has to learn that it
    # was stopped rather than that it finished.
    said = result.summary().lower()
    assert "time limit" in said or "timed out" in said, result.summary()
    assert "cleanly" not in said


def test_an_import_error_is_a_result_not_an_exception(tmp_path):
    root = _pkg(tmp_path, "import a_module_that_does_not_exist\n")
    result = try_extension(root, "entry.py")
    assert result.ok is False
    assert "ModuleNotFound" in result.stderr or "No module" in result.stderr


# ── what it cannot see ──────────────────────────────────────────────────────

def test_the_users_credentials_are_not_in_its_environment(tmp_path, monkeypatch):
    """The cheapest theft available to anything that runs: read os.environ."""
    monkeypatch.setenv("GEMINI_API_KEY", "ya29.SHOULD-NOT-BE-VISIBLE")
    monkeypatch.setenv("NOVA_SECRET_KEY", "also-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")

    root = _pkg(tmp_path, "import os\nprint('|'.join(sorted(os.environ)))\n")
    result = try_extension(root, "entry.py")

    assert result.ok, result.stderr
    assert "GEMINI_API_KEY" not in result.stdout
    assert "NOVA_SECRET_KEY" not in result.stdout
    assert "AWS_SECRET_ACCESS_KEY" not in result.stdout


def test_no_secret_value_survives_even_if_the_name_is_unfamiliar(tmp_path,
                                                                 monkeypatch):
    monkeypatch.setenv("SOME_VENDOR_TOKEN", "ya29.A-REAL-LOOKING-TOKEN")
    root = _pkg(tmp_path, "import os\nprint(os.environ.get('SOME_VENDOR_TOKEN',''))\n")
    result = try_extension(root, "entry.py")
    assert "ya29.A-REAL-LOOKING-TOKEN" not in result.stdout


def test_it_still_gets_enough_environment_to_run(tmp_path):
    """Strip too much and nothing runs, which teaches nothing."""
    root = _pkg(tmp_path, "import sys, json\nprint(json.dumps([1, 2]))\n")
    result = try_extension(root, "entry.py")
    assert result.ok, result.stderr
    assert "[1, 2]" in result.stdout


# ── where it writes ─────────────────────────────────────────────────────────

def test_a_relative_write_lands_somewhere_disposable(tmp_path):
    root = _pkg(tmp_path, "open('dropped.txt', 'w').write('x')\n")
    result = try_extension(root, "entry.py")

    assert result.ok, result.stderr
    assert not (Path.cwd() / "dropped.txt").exists(), (
        "the candidate wrote into NOVA's working directory"
    )
    assert not (root / "dropped.txt").exists(), (
        "the candidate wrote into its own source tree"
    )


# ── bounded output ──────────────────────────────────────────────────────────

def test_a_flood_of_output_is_truncated(tmp_path):
    root = _pkg(tmp_path, "print('x' * 200000)\n")
    result = try_extension(root, "entry.py", max_output=4000)
    assert len(result.stdout) <= 4200, len(result.stdout)


# ── honesty ─────────────────────────────────────────────────────────────────

def test_the_isolation_describes_its_own_limits():
    """A reassuring name is worse than an accurate sentence."""
    described = describe_isolation().lower()
    assert "not" in described
    assert "network" in described, (
        "it does not say that network access is still possible"
    )
    for word in ("sandboxed", "fully contained", "cannot escape"):
        assert word not in described, (
            f"claims more than it delivers: {word!r}"
        )


def test_a_missing_entrypoint_is_answered_not_raised(tmp_path):
    root = _pkg(tmp_path, "print('hi')\n")
    result = try_extension(root, "nope.py")
    assert result.ok is False
    assert "entrypoint" in result.summary().lower()


def test_the_result_reads_as_a_sentence(tmp_path):
    root = _pkg(tmp_path, "print('fine')\n")
    result = try_extension(root, "entry.py")
    assert isinstance(result, TrialResult)
    assert result.summary()
    assert "Traceback" not in result.summary()
