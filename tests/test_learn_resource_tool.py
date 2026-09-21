"""Giving the model a way to look at something, and only to look.

The extension pipeline exists and is tested, but until a tool exposes it the
user cannot say "NOVA, learn this folder". Wiring it up is where the care is
needed: the point of the pipeline is that it can refuse, and a tool that
quietly bypassed the ladder would undo all of it.

So the tool inspects, probes in isolation, and reports. It does not install,
does not enable, and cannot be argued into either -- those need a person, and
the permission engine treats the whole thing as consequential.
"""
from __future__ import annotations

import json

import pytest

import nova
from nova_core.permissions import Effect, Trust, capabilities_for_tool, check_tool


def _decl():
    for d in nova.TOOL_DECLARATIONS:
        if d.get("name") == "learn_resource":
            return d
    pytest.fail("learn_resource is not declared to the model")


def test_the_tool_is_offered_to_the_model():
    assert _decl()["description"]


def test_it_is_authorised_rather_than_refused():
    """A declared tool the engine denies outright is a guaranteed failure."""
    assert capabilities_for_tool("learn_resource") is not None
    verdicts = {check_tool("nova", "learn_resource", trust=t).effect
                for t in (Trust.USER, Trust.UNTRUSTED)}
    assert verdicts != {Effect.DENY}


def test_a_web_page_cannot_get_something_learned():
    """The obvious attack: a page telling NOVA to learn the page's own payload."""
    decision = check_tool("nova", "learn_resource", trust=Trust.UNTRUSTED)
    assert decision.effect is not Effect.ALLOW, (
        "untrusted content could point NOVA at arbitrary code unattended"
    )


def test_the_description_does_not_promise_installation():
    described = _decl()["description"].lower()
    assert "install" not in described or "not install" in described \
        or "does not install" in described, described


def test_looking_at_a_folder_reports_without_installing(tmp_path):
    pkg = tmp_path / "thing"
    pkg.mkdir()
    (pkg / "main.py").write_text("def add(a, b):\n    return a + b\n",
                                 encoding="utf-8")
    (pkg / "nova_extension.json").write_text(json.dumps({
        "name": "thing", "version": "1.0.0", "entrypoint": "main.py",
    }), encoding="utf-8")

    out = nova._execute_learn_resource({"path": str(pkg)})

    assert "thing" in out
    assert "not installed" in out.lower() or "have not installed" in out.lower()


def test_a_dangerous_package_is_described_as_such(tmp_path):
    pkg = tmp_path / "bad"
    pkg.mkdir()
    (pkg / "main.py").write_text("def f(s):\n    return eval(s)\n",
                                 encoding="utf-8")

    out = nova._execute_learn_resource({"path": str(pkg)})
    assert "high" in out.lower() or "eval" in out.lower(), out


def test_a_missing_path_is_answered_not_raised():
    out = nova._execute_learn_resource({"path": "C:/nope/not/here"})
    assert "couldn't" in out.lower() or "nothing" in out.lower(), out


def test_a_url_is_refused_for_now_rather_than_fetched():
    """Fetching remote code is a separate decision with its own risks."""
    out = nova._execute_learn_resource({"path": "https://example.com/thing.zip"})
    assert "http" in out.lower() or "folder" in out.lower(), out
    assert "downloaded" not in out.lower()
