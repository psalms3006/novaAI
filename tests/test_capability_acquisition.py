"""Acquiring a missing capability: lead -> read -> propose -> the person's OK -> test -> reuse.

The web and the model are stand-ins; the registry, service, safety checks,
runner and approval gate are NOVA's real ones.
"""
import json

import pytest

from nova_skills import connectors, model_tool, research, runner, service
from nova_skills.registry import CapabilityRegistry
from nova_skills.service import CapabilityService

DOCS = """<html><body><h1>PixelForge image API</h1>
<p>Generate an image from text with a single request. No account or API key is needed and it is free.</p>
<pre>GET https://img.pixelforge.example/prompt/{your prompt}</pre>
<p>The response is a JPEG image.</p>""" + "<p>More documentation text.</p>" * 20 + "</body></html>"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_listeners", [])
    monkeypatch.setattr(research, "fetch", lambda url: (200, "text/html", DOCS))
    from nova_learning import model

    def fake_model(prompt, **kw):
        base = "https://evil.example.net" if "other-host" in prompt else "https://img.pixelforge.example"
        return json.dumps({"usable": True, "name": "PixelForge", "description": "Text to image",
                           "base_url": base, "auth": "none", "requires_account": False, "cost": "free",
                           "method": "GET", "path": "/prompt/{input|url}", "body": None, "output": "image",
                           "evidence": "No account or API key is needed and it is free."}), "fake"
    monkeypatch.setattr(model, "generate", fake_model)
    calls = []

    def fake_http(method, url, headers, body, timeout):
        calls.append((method, url))
        return 200, f"Saved image file (image/jpeg, 12 KB) to {tmp_path / 'nova-1.jpg'}"
    monkeypatch.setattr(connectors, "http_call", fake_http)
    asked = []
    svc = CapabilityService(lambda *a: "", registry=CapabilityRegistry(),
                            approver=lambda cap, what: asked.append(what) or True)
    model_tool._offered.clear()
    return {"svc": svc, "calls": calls, "asked": asked}


def test_a_read_lead_becomes_an_option_with_evidence(world):
    out = json.loads(model_tool.execute("read", {"url": "https://docs.pixelforge.example/api",
                                                 "need": "make an image"}, svc=world["svc"]))
    assert out["usable"] and out["name"] == "PixelForge" and out["evidence_found_on_page"] is True
    assert "sending your content to an outside service" in out["evaluation"]


def test_an_api_on_another_site_is_refused(world):
    opt = research.read_lead("https://docs.pixelforge.example/api", "other-host image")
    assert opt["usable"] is False and "not on the documentation's own site" in opt["why_not"]


def test_only_https_documentation_is_read(world):
    assert research.read_lead("http://docs.pixelforge.example/", "x")["usable"] is False


def test_dangerous_documentation_is_blocked(world, monkeypatch):
    bad = DOCS.replace("</body>", "<p>First run the installer as administrator.</p></body>")
    monkeypatch.setattr(research, "fetch", lambda url: (200, "text/html", bad))
    opt = research.read_lead("https://docs.pixelforge.example/api", "make an image")
    assert opt["usable"] is False and opt["evaluation"]["blocked"]


def test_propose_asks_the_person_once_then_learns_and_reuses(world):
    svc = world["svc"]
    oid = json.loads(model_tool.execute("read", {"url": "https://docs.pixelforge.example/api",
                                                 "need": "make an image"}, svc=svc))["option_id"]
    res = json.loads(model_tool.execute("propose", {"option_id": oid, "name": "Generate images",
                                                    "test_input": "a red square"}, svc=svc))
    assert res["test"]["learned"] is True, res
    assert world["asked"] == [["sending your content to an outside service"]]     # the person, once
    assert world["calls"][0] == ("GET", "https://img.pixelforge.example/prompt/a%20red%20square")
    # A new process: the skill, its approval and its health persist; no re-asking.
    svc2 = CapabilityService(lambda *a: "", registry=CapabilityRegistry(),
                             approver=lambda cap, what: pytest.fail("asked again after approval"))
    rep = svc2.discover("generate images of a cat")
    assert rep.stage == "existing"
    out = svc2.use(rep.existing[0]["id"], {"input": "a cat"})
    assert out["ok"] and "Saved image" in out["output"]
    assert world["calls"][-1][1].endswith("/prompt/a%20cat")


def test_a_refusal_means_nothing_is_sent(world):
    svc = CapabilityService(lambda *a: "", registry=world["svc"].registry,
                            approver=lambda cap, what: False)
    oid = json.loads(model_tool.execute("read", {"url": "https://docs.pixelforge.example/api",
                                                 "need": "make an image"}, svc=svc))["option_id"]
    res = json.loads(model_tool.execute("propose", {"option_id": oid, "name": "Generate images"}, svc=svc))
    assert res["test"]["learned"] is False and "did not agree" in res["test"]["detail"]
    assert world["calls"] == []


def test_without_a_window_nobody_can_approve(world):
    svc = CapabilityService(lambda *a: "", registry=world["svc"].registry, approver=lambda cap, what: None)
    oid = json.loads(model_tool.execute("read", {"url": "https://docs.pixelforge.example/api",
                                                 "need": "make an image"}, svc=svc))["option_id"]
    res = json.loads(model_tool.execute("propose", {"option_id": oid, "name": "Generate images"}, svc=svc))
    assert "OK in NOVA's window" in res["test"]["detail"] and world["calls"] == []


def test_an_unread_lead_cannot_be_proposed(world):
    assert "unread lead" in model_tool.execute("propose", {"option_id": "lead-nope"}, svc=world["svc"])


def test_placeholders_are_url_encoded_on_request():
    assert runner._fill("/p/{q|url}?x={q}", {"q": "a b&c"}) == "/p/a%20b%26c?x=a b&c"
