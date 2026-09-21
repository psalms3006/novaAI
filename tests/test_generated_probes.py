"""Working out what to ask a new extension, and asking it.

§33 wants NOVA to generate tests for something she has just been handed. The
tempting reading is "have the model write a test file", which produces
plausible code that may test nothing -- and would have to be run to find out,
which is the one thing inspection exists to avoid deciding blindly.

So the probes are derived from what is actually in the package: the functions
it exposes, the capabilities the AST found, whether it reads credentials.
Each probe is a small script that answers one question and is run through the
trial runner, in a separate process with the environment scrubbed.

The questions come straight from the spec, and the useful ones are the
unhappy paths -- what happens with no credentials, what happens when the
network is not there -- because those are what a connector gets wrong and what
a demo never covers.
"""
from __future__ import annotations

import json

import pytest

from nova_extensions.probes import Probe, generate_probes, run_probes


def _pkg(tmp_path, files: dict, name="cand"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _manifest(**kw):
    base = {"name": "cand", "version": "1.0.0", "entrypoint": "main.py"}
    base.update(kw)
    return json.dumps(base)


# ── what gets asked ─────────────────────────────────────────────────────────

def test_every_package_is_at_least_asked_whether_it_loads(tmp_path):
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(),
        "main.py": "def hello():\n    return 'hi'\n",
    })
    probes = generate_probes(root)
    assert any(p.question == "does it load" for p in probes), \
        [p.question for p in probes]


def test_a_package_that_reaches_the_network_is_asked_about_no_network(tmp_path):
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(permissions=["network"]),
        "main.py": "import requests\n\ndef fetch(u):\n    return requests.get(u)\n",
    })
    probes = generate_probes(root)
    questions = [p.question for p in probes]
    assert any("network" in q for q in questions), questions


def test_a_package_that_reads_credentials_is_asked_about_missing_ones(tmp_path):
    """The failure a connector gets wrong and a demo never covers."""
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(),
        "main.py": (
            "import os\n\n"
            "def client():\n"
            "    return os.environ['SERVICE_API_KEY']\n"
        ),
    })
    probes = generate_probes(root)
    questions = " ".join(p.question for p in probes)
    assert "credential" in questions or "missing" in questions, questions


def test_public_functions_are_each_worth_a_question(tmp_path):
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(),
        "main.py": (
            "def alpha():\n    return 1\n\n"
            "def beta():\n    return 2\n\n"
            "def _private():\n    return 3\n"
        ),
    })
    probes = generate_probes(root)
    body = " ".join(p.script for p in probes)
    assert "alpha" in body and "beta" in body
    assert "_private" not in body, "a private helper is not part of the surface"


def test_the_number_of_probes_is_bounded(tmp_path):
    funcs = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(200))
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(), "main.py": funcs,
    })
    probes = generate_probes(root, max_probes=10)
    assert len(probes) <= 10


def test_generating_probes_does_not_run_the_package(tmp_path):
    canary = tmp_path / "IT_RAN"
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(),
        "main.py": (
            "from pathlib import Path\n"
            f"Path(r{str(canary)!r}).write_text('executed')\n"
            "def f():\n    return 1\n"
        ),
    })
    generate_probes(root)
    assert not canary.exists(), "generating probes executed the package"


# ── running them ────────────────────────────────────────────────────────────

def test_a_working_package_passes_its_probes(tmp_path):
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(),
        "main.py": "def hello():\n    return 'hi'\n",
    })
    report = run_probes(root, generate_probes(root), timeout_seconds=30)

    assert report.passed >= 1, report.summary()
    assert report.failed == 0, report.summary()


def test_a_package_that_explodes_on_import_fails_the_first_probe(tmp_path):
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(),
        "main.py": "raise RuntimeError('nope')\n",
    })
    report = run_probes(root, generate_probes(root), timeout_seconds=30)

    assert report.failed >= 1
    assert report.passed == 0
    assert "does it load" in report.summary()


def test_the_report_says_what_was_only_mocked(tmp_path):
    """§34: if only mocked tests were possible, say so."""
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(permissions=["network"]),
        "main.py": "import requests\n\ndef fetch(u):\n    return requests.get(u)\n",
    })
    report = run_probes(root, generate_probes(root), timeout_seconds=30)
    said = report.summary().lower()
    assert "not" in said
    assert "real" in said or "credential" in said or "live" in said, said


def test_a_clean_run_is_not_called_verified(tmp_path):
    root = _pkg(tmp_path, {
        "nova_extension.json": _manifest(),
        "main.py": "def hello():\n    return 'hi'\n",
    })
    report = run_probes(root, generate_probes(root), timeout_seconds=30)
    said = report.summary().lower()
    assert "verified" not in said, (
        "a handful of smoke probes was described as verification"
    )


def test_running_no_probes_is_reported_honestly(tmp_path):
    root = _pkg(tmp_path, {"nova_extension.json": _manifest()})
    report = run_probes(root, [], timeout_seconds=10)
    assert report.passed == 0 and report.failed == 0
    assert "nothing" in report.summary().lower()


def test_a_package_is_imported_by_its_own_name_not_as_init(tmp_path):
    """Found by pointing the tool at a real package.

    `__init__.py` is not importable as "__init__" -- the probe failed with
    ModuleNotFoundError for a reason that had nothing to do with the package
    under test, which is the worst kind of red.
    """
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    probes = generate_probes(pkg)
    body = " ".join(p.script for p in probes)
    assert "import mypkg" in body, body
    assert "import __init__" not in body

    report = run_probes(pkg, probes, timeout_seconds=30)
    assert report.failed == 0, report.summary()
