"""Looking at something a user pointed NOVA at, without running it.

"NOVA, learn this folder" is the most dangerous sentence in the product. The
obvious implementation -- fetch, import, see what happens -- is remote code
execution wearing a friendly hat, and importing a module *is* running it:
top-level code executes on import, before any check could intervene.

So inspection is entirely static. The package is read as text and parsed as a
syntax tree; nothing is imported, nothing is executed, and no subprocess is
started. The output is a description and a verdict, and the verdict is allowed
to be "no".

The second rule is that a manifest is a claim, not evidence. An extension
declaring `"permissions": []` while importing `socket` has told us something
useful -- that its author is careless or lying -- and that is worth more than
the declaration itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nova_extensions.inspect import (
    Risk,
    inspect_resource,
)


def _pkg(tmp_path, files: dict, name="thing"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


# ── the cardinal rule ───────────────────────────────────────────────────────

def test_inspection_never_imports_what_it_inspects(tmp_path):
    """Top-level code runs on import, before any check could stop it.

    The canary writes a file. If inspection imports the module, the file
    appears, and this test fails -- which is the whole point.
    """
    canary = tmp_path / "IT_RAN"
    root = _pkg(tmp_path, {
        "__init__.py": "",
        "evil.py": (
            "from pathlib import Path\n"
            f"Path(r{str(canary)!r}).write_text('executed')\n"
        ),
    })

    report = inspect_resource(root)

    assert not canary.exists(), (
        "inspecting the package executed it; importing is running"
    )
    assert report is not None


def test_a_package_that_cannot_even_be_parsed_is_reported_not_raised(tmp_path):
    root = _pkg(tmp_path, {"broken.py": "def (:::\n"})
    report = inspect_resource(root)
    assert report.risk is not Risk.LOW
    assert any("parse" in r.lower() or "syntax" in r.lower()
               for r in report.reasons), report.reasons


# ── what it notices ─────────────────────────────────────────────────────────

def test_network_access_is_noticed(tmp_path):
    root = _pkg(tmp_path, {"api.py": "import requests\n\ndef go():\n    pass\n"})
    report = inspect_resource(root)
    assert "network" in report.capabilities_seen, report.capabilities_seen


def test_running_other_programs_is_noticed(tmp_path):
    root = _pkg(tmp_path, {"r.py": "import subprocess\n"})
    report = inspect_resource(root)
    assert "process" in report.capabilities_seen


def test_evaluating_strings_is_treated_as_dangerous(tmp_path):
    root = _pkg(tmp_path, {"d.py": "def f(s):\n    return eval(s)\n"})
    report = inspect_resource(root)
    assert report.risk is Risk.HIGH, report.reasons
    assert any("eval" in r.lower() for r in report.reasons)


def test_filesystem_writes_are_noticed(tmp_path):
    root = _pkg(tmp_path, {"w.py": "def f(p):\n    open(p, 'w').write('x')\n"})
    report = inspect_resource(root)
    assert "filesystem" in report.capabilities_seen


def test_a_plain_library_is_low_risk(tmp_path):
    root = _pkg(tmp_path, {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "README.md": "# calc\nAdds numbers.\n",
    })
    report = inspect_resource(root)
    assert report.risk is Risk.LOW, report.reasons


# ── manifests are claims, not evidence ──────────────────────────────────────

def test_a_manifest_is_read(tmp_path):
    root = _pkg(tmp_path, {
        "nova_extension.json": json.dumps({
            "name": "weather", "version": "1.0.0",
            "description": "Weather lookups", "permissions": ["network"],
            "entrypoint": "weather.py",
        }),
        "weather.py": "import requests\n",
    })
    report = inspect_resource(root)
    assert report.manifest is not None
    assert report.manifest.get("name") == "weather"


def test_a_manifest_understating_what_the_code_does_is_contradicted(tmp_path):
    """The case worth catching: declares nothing, opens sockets."""
    root = _pkg(tmp_path, {
        "nova_extension.json": json.dumps({
            "name": "innocent", "version": "1.0.0", "permissions": [],
            "entrypoint": "main.py",
        }),
        "main.py": "import socket, subprocess\n",
    })
    report = inspect_resource(root)

    assert report.undeclared, report.reasons
    assert "network" in report.undeclared or "process" in report.undeclared
    assert report.risk is not Risk.LOW


def test_a_manifest_pointing_at_a_file_that_is_not_there_is_caught(tmp_path):
    root = _pkg(tmp_path, {
        "nova_extension.json": json.dumps({
            "name": "x", "version": "1.0.0", "entrypoint": "missing.py",
        }),
    })
    report = inspect_resource(root)
    assert any("entrypoint" in r.lower() for r in report.reasons), report.reasons


# ── what it refuses to read ─────────────────────────────────────────────────

def test_secrets_are_not_ingested(tmp_path):
    root = _pkg(tmp_path, {
        "ok.py": "x = 1\n",
        ".env": "SECRET_TOKEN=ya29.REAL-LOOKING-SECRET\n",
        "id_rsa": "-----BEGIN PRIVATE KEY-----\nAAAA\n",
    })
    report = inspect_resource(root)

    blob = json.dumps(report.to_dict())
    assert "ya29.REAL-LOOKING-SECRET" not in blob
    assert "BEGIN PRIVATE KEY" not in blob
    assert any("secret" in r.lower() or "credential" in r.lower()
               for r in report.reasons), report.reasons


def test_gitignored_files_are_skipped(tmp_path):
    root = _pkg(tmp_path, {
        ".gitignore": "build/\n",
        "keep.py": "x = 1\n",
        "build/generated.py": "import subprocess\n",
    })
    report = inspect_resource(root)
    assert "build/generated.py" not in report.files_examined
    assert "keep.py" in report.files_examined


def test_an_enormous_tree_is_bounded(tmp_path):
    files = {f"mod{i}.py": "x = 1\n" for i in range(500)}
    root = _pkg(tmp_path, files)
    report = inspect_resource(root, max_files=50)
    assert len(report.files_examined) <= 50
    assert any("only" in r.lower() or "bounded" in r.lower() or
               "limit" in r.lower() for r in report.reasons), report.reasons


# ── the verdict ─────────────────────────────────────────────────────────────

def test_a_clean_inspection_does_not_mean_trusted(tmp_path):
    """Compiling is not a reason to trust something."""
    root = _pkg(tmp_path, {"calc.py": "def add(a, b):\n    return a + b\n"})
    report = inspect_resource(root)
    assert report.trust == "inspected", report.trust
    assert report.installable is False, (
        "a static read is not grounds for installing anything"
    )


def test_the_report_says_what_it_could_not_determine(tmp_path):
    root = _pkg(tmp_path, {"calc.py": "def add(a, b):\n    return a + b\n"})
    report = inspect_resource(root)
    assert report.reasons, "a verdict with no reasoning is not reviewable"


def test_a_missing_path_is_answered_not_raised(tmp_path):
    report = inspect_resource(tmp_path / "nope")
    assert report.risk is Risk.UNKNOWN
    assert report.installable is False
