"""Pinned model names expire; aliases do not.

Checked against the live Gemini model list on 2026-09-20:

    gemini-2.0-flash                 RETIRED (404: "no longer available")
    gemini-2.5-flash-preview-05-20   RETIRED

`actions/screen_processor.py` resolved its vision model from
`config/api_keys.json`, which pinned `gemini-2.0-flash`, so every screen
analysis through that path would have failed with a 404 — and that module is
the natural foundation for ambient screen watching. `nova_agents.BrowserAgent`
pinned the other one.

The desktop path was unaffected because it uses the alias
`gemini-flash-latest`, which is the whole point: an alias keeps working when
Google retires a version, and a dated preview string is a time bomb with a
long fuse.

This test cannot call the API — it must pass offline and in CI. So it guards
the *class* of mistake instead: model ids in the repository should be aliases,
or be on a short list of pinned ids someone has deliberately accepted.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Confirmed gone. Anything here is a live bug, not a style preference.
RETIRED = {
    "gemini-2.0-flash",
    "gemini-2.5-flash-preview-05-20",
}

#: Pinned ids that were alive when last checked and are deliberately pinned.
#: Voice and audio need specific builds; an alias would move under them.
ACCEPTED_PINS = {
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-3.1-flash-live-preview",
    "gemini-3.1-flash",
    "gemini-3.6-flash",
}

#: Tracked paths that are not our configuration.
SKIP_DIRS = {"archive", ".devcloud", "tests"}

MODEL_RE = re.compile(r"gemini-[0-9][0-9a-zA-Z.\-]*")


def _source_files():
    """Only files git tracks.

    Walking the tree instead picked up the abandoned build directories, which
    vendor Google's API discovery cache and mention hundreds of model names
    that are nothing to do with this project's configuration.
    """
    import subprocess

    listed = subprocess.run(
        ["git", "ls-files", "*.py", "*.toml", "*.json"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.splitlines()

    for name in listed:
        path = REPO / name
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def _model_mentions():
    """Model ids that the program could actually use.

    For Python this reads string constants via the AST rather than grepping
    the file. A `#` comment explaining which id was retired is commentary, not
    configuration, and a test that cannot tell them apart punishes writing the
    explanation down. Docstrings are string constants and so are still
    checked -- a docstring stating a stale default is a small lie that will
    mislead the next reader.
    """
    import ast

    found = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(REPO).as_posix()

        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for match in MODEL_RE.finditer(node.value):
                        found.append((rel, node.lineno, match.group(0)))
            continue

        for match in MODEL_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append((rel, line, match.group(0)))
    return found


def test_no_retired_model_is_referenced():
    """Fails while any known-dead model id is still configured."""
    hits = [(f, l, m) for f, l, m in _model_mentions() if m in RETIRED]
    assert hits == [], (
        "these model ids no longer exist and will return HTTP 404:\n  "
        + "\n  ".join(f"{f}:{l} -> {m}" for f, l, m in hits)
        + "\nPrefer an alias such as gemini-flash-latest."
    )


def test_every_pinned_model_is_one_we_have_accepted():
    """A new pinned version should be a decision, not an accident."""
    unknown = sorted({
        f"{f}:{l} -> {m}"
        for f, l, m in _model_mentions()
        if m not in ACCEPTED_PINS and m not in RETIRED
    })
    assert unknown == [], (
        "unrecognised pinned model id(s):\n  " + "\n  ".join(unknown)
        + "\nEither use an alias (gemini-flash-latest / gemini-flash-lite-latest) "
          "or add it to ACCEPTED_PINS once you have checked it exists."
    )
