"""`open_app` must not be a way to run arbitrary commands.

The unknown-application branch built a shell string by interpolation:

    cmd = f'start "" "{app_name}"'
    subprocess.Popen(cmd, shell=True, ...)

`app_name` comes from the model, and the model can be steered by a web page
that `web_search` just fetched. A name containing a quote and an `&` closes
the quote and hands the rest to cmd.exe. `APP_LAUNCH` is ALLOW at USER trust,
so nothing else in the chain asks first.

These tests assert the command never reaches the shell — not merely that the
reply looks like a refusal.
"""
from __future__ import annotations

import subprocess

import pytest

import actions.open_app as open_app


HOSTILE = [
    'notepad" & calc & "',            # close the quote, chain a command
    "notepad & calc",                  # bare chaining
    "notepad | calc",                  # pipe
    "notepad && calc",                 # conditional chain
    "foo\ncalc",                       # newline as a separator
    "notepad`calc`",                   # backtick
    "$(calc)",                         # substitution
    "notepad %COMSPEC%",               # environment expansion
]


@pytest.fixture
def no_shell(monkeypatch):
    """Record every launch attempt instead of performing it."""
    launched = []

    def fake_popen(cmd, *a, **kw):
        launched.append(cmd)
        raise AssertionError(
            f"a command reached subprocess.Popen: {cmd!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return launched


@pytest.mark.parametrize("app_name", HOSTILE)
def test_a_name_carrying_shell_syntax_never_reaches_the_shell(no_shell, app_name):
    result = open_app.execute({"app_name": app_name})

    assert no_shell == [], f"launched {no_shell!r}"
    assert "open" not in result.lower() or "can't" in result.lower() or \
           "cannot" in result.lower() or "won't" in result.lower(), result


def test_an_ordinary_application_name_is_still_accepted(monkeypatch):
    """Guard against fixing this by refusing everything."""
    seen = {}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return (b"", b"")

    def fake_popen(cmd, *a, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = open_app.execute({"app_name": "notepad"})

    assert "cmd" in seen, "a legitimate application was not launched"
    assert "opened" in result.lower(), result


def test_a_real_path_with_spaces_is_still_accepted(monkeypatch):
    """Program names legitimately contain spaces and colons and backslashes."""
    seen = {}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return (b"", b"")

    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, *a, **kw: (seen.__setitem__("cmd", cmd), FakeProc())[1],
    )

    result = open_app.execute({"app_name": r"C:\Program Files\Thing\thing.exe"})

    assert "cmd" in seen, "a legitimate path was rejected"
    assert "opened" in result.lower(), result


def test_program_files_x86_is_not_mistaken_for_an_attack(monkeypatch):
    """Parentheses are ordinary in Windows paths and harmless inside quotes."""
    seen = {}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return (b"", b"")

    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, *a, **kw: (seen.__setitem__("cmd", cmd), FakeProc())[1],
    )

    result = open_app.execute(
        {"app_name": r"C:\Program Files (x86)\Thing\thing.exe"}
    )

    assert "cmd" in seen, "a path containing (x86) was rejected"
    assert "opened" in result.lower(), result
