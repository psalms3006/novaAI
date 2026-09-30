"""nova_learning — the mechanics of learning, with a stand-in model.

The stand-in follows the prompt formats literally (each "- " line of a source
becomes an item citing that source; "avoid X" vs "use X" is a conflict;
answers list the items they were given with their sources). It is not clever
and does not need to be: these tests check provenance, merging, contradiction
resolution, verification bookkeeping, persistence, incremental updates,
resumption and failure honesty. Whether a real model learns is tested live.
"""
import json
import re

import pytest

from nova_learning import consolidate, inventory, model, retrieve
from nova_learning import service as svc_mod
from nova_learning.model_tool import execute
from nova_learning.service import LearningService
from nova_learning.store import KnowledgeStore


# ── a literal-minded stand-in model ──────────────────────────────────────────
class FakeModel:
    def __init__(self):
        self.calls = []
        self.fail_after = None           # raise ModelUnavailable after N calls
        self.extract_calls = 0

    def __call__(self, prompt, *, images=None, want_json=True, allow_local=True):
        self.calls.append(prompt[:60])
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise model.ModelUnavailable("quota exhausted (test)")
        if "Extract reusable knowledge" in prompt:
            self.extract_calls += 1
            return json.dumps(self._extract(prompt)), "fake"
        if "DIRECTLY CONTRADICT" in prompt:
            return json.dumps(self._conflicts(prompt)), "fake"
        if "writing an exam" in prompt:
            return json.dumps(self._exam(prompt)), "fake"
        if "Grade an assistant" in prompt:
            m = re.search(r"SHOULD REFLECT:\n(.*?)\nANSWER:\n(.*?)\n\nCriteria", prompt, re.S)
            expected, answer = m.group(1), m.group(2)
            if "TEST TYPE: conflict" in prompt:
                ok = "conflicts with" in answer.lower()
            else:
                words = set(re.findall(r"[a-z]{5,}", expected.lower()))
                ok = sum(w in answer.lower() for w in words) >= max(1, len(words) // 3)
            return json.dumps({"pass": ok, "reason": "fake grader"}), "fake"
        if "Answer using ONLY the knowledge below" in prompt:
            items = re.findall(r"- \[(\w+)\] (.*?) \(sources: (.*?)\)", prompt)
            task = prompt.split("QUESTION / TASK:\n", 1)[1]
            lines = []
            for _kind, stmt, src in items:
                if "Does this follow" in task and stmt[:30] in task:
                    lines.append(f"No — this conflicts with: {stmt} [{src}]")
                lines.append(f"{stmt} [{src}]")
            return "\n".join(lines) or "The knowledge does not cover this.", "fake"
        raise AssertionError("unexpected prompt: " + prompt[:80])

    def _extract(self, prompt):
        items, notes = [], {}
        for label, _rel, body in re.findall(r"\[(S\d+)\] document: (\S+).*?\n<<<\n(.*?)\n>>>", prompt, re.S):
            for line in body.splitlines():
                if line.startswith("- "):
                    stmt = line[2:].strip()
                    kind = "rule" if re.match(r"(always|never|avoid)", stmt.lower()) else "principle"
                    items.append({"kind": kind, "statement": stmt, "sources": [label],
                                  "quote": stmt[:40], "confidence": "medium"})
        for label, rel in re.findall(r"\[(S\d+)\] image: (\S+)", prompt):
            notes[label] = f"a layout reference ({rel})"
            items.append({"kind": "pattern",
                          "statement": "Examples consistently use one accent colour on a neutral background.",
                          "sources": [label], "quote": "", "confidence": "medium"})
        items.append({"kind": "principle", "statement": "Invented guidance with no source",
                      "sources": ["S99"], "quote": "x", "confidence": "high"})
        return {"items": items, "contradictions": [], "image_notes": notes}

    def _conflicts(self, prompt):
        rows = re.findall(r"(k_\w+): \[\w+\] (.*)", prompt)
        out = []
        for i, (ia, a) in enumerate(rows):
            for ib, b in rows[i + 1:]:
                ta, tb = a.lower(), b.lower()
                if ("avoid" in ta) != ("avoid" in tb) and "rounded" in ta and "rounded" in tb:
                    out.append({"a": ia, "b": ib, "note": "rounded cards"})
        return {"conflicts": out}

    def _exam(self, prompt):
        ids = re.findall(r"^(k_\w+): \[\w+\] (.*)$", prompt, re.M)
        v = re.search(r"BREAKS item (k_\w+) \(\"(.*?)\"\)", prompt)
        first = [i for i, _ in ids[:3]]
        return {"A": {"question": f"What does the material emphasise about {ids[0][1][:30]}?", "expect": first},
                "B": {"question": f"A landing page is needed. Which principles apply? {ids[1][1][:30]}",
                      "expect": first},
                "C": {"question": f"Plan: do the opposite of this -> {v.group(2)}. Does this follow the "
                                  f"learned guidance?", "expect": [v.group(1)]},
                "E": {"question": f"New task: an onboarding screen. Apply what you learned. {ids[2][1][:30]}",
                      "expect": first}}


@pytest.fixture
def fake(monkeypatch):
    m = FakeModel()
    monkeypatch.setattr(model, "generate", m)
    return m


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "DesignKnowledge"
    (root / "notes").mkdir(parents=True)
    (root / "examples").mkdir()
    (root / "brand-guidelines.md").write_text(
        "# Brand\n- Always use generous whitespace around primary content\n"
        "- Avoid rounded cards beyond a 4px radius\n- Use Inter for all interface text\n", encoding="utf-8")
    (root / "tutorial-ui.md").write_text(
        "# Tutorial\n- Use rounded cards to make interfaces friendly\n"
        "- Keep one primary action per screen\n", encoding="utf-8")
    (root / "notes" / "design-preferences.txt").write_text(
        "- Never use more than two typefaces\n- Always use generous whitespace around primary content\n",
        encoding="utf-8")
    from PIL import Image
    Image.new("RGB", (64, 48), (240, 240, 240)).save(root / "examples" / "example1.png")
    (root / "examples" / "copy.png").write_bytes((root / "examples" / "example1.png").read_bytes())
    (root / "demo.mp4").write_bytes(b"\x00" * 10)
    (root / "empty.txt").write_text("", encoding="utf-8")
    (root / "archive.xyz").write_bytes(b"???")
    return root


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(svc_mod, "_listeners", [])
    return LearningService(KnowledgeStore(tmp_path / "data" / "knowledge"), index=False)


# ── inventory ────────────────────────────────────────────────────────────────
def test_inventory_classifies_and_explains_every_skip(folder):
    files = {f.rel: f for f in inventory.scan(str(folder))}
    assert files["brand-guidelines.md"].kind == "document" and files["brand-guidelines.md"].status == "pending"
    assert files["examples/example1.png"].kind == "image"
    pair = [files["examples/copy.png"], files["examples/example1.png"]]
    dup = [f for f in pair if f.reason == "duplicate"]
    assert len(dup) == 1 and dup[0].duplicate_of in ("examples/copy.png", "examples/example1.png")
    assert "not processed yet" in files["demo.mp4"].reason
    assert files["empty.txt"].reason == "empty file"
    assert files["archive.xyz"].reason.startswith("unsupported")
    with pytest.raises(FileNotFoundError):
        inventory.scan(str(folder / "nope"))


# ── a full learning session ──────────────────────────────────────────────────
def test_learning_builds_verified_source_grounded_knowledge(folder, svc, fake):
    s = svc.learn(str(folder), domain="Design", scope="personal", background=False)
    s = svc.status(s["id"])
    assert s["status"] == "done" and s["phase"] == "ready", s["message"]
    dom = svc.store.find_domain("Design")
    assert dom["status"] == "learned", dom.get("verification")
    k = svc.store.knowledge(dom["id"])
    active = [i for i in k["items"] if i["status"] == "active"]
    assert not any("Invented" in i["statement"] for i in active)       # no provenance, no knowledge
    ws = next(i for i in active if "whitespace" in i["statement"])
    assert {x["rel"] for x in ws["support"]} == {"brand-guidelines.md", "notes/design-preferences.txt"}
    assert ws["confidence"] > 0.65 and ws["support"][0]["quote_verified"] is True
    assert any(i["kind"] == "pattern" and i["support"][0]["rel"].startswith("examples/") for i in active)
    [c] = k["contradictions"]                                           # brand guide vs tutorial
    assert c["resolved_by"] == "source authority"
    loser = next(i for i in k["items"] if i["id"] in (c["a"], c["b"]) and i["id"] != c["winner"])
    assert loser["status"] == "superseded" and "tutorial" in loser["support"][0]["rel"]
    v = dom["verification"]
    assert v["total"] == 5 and v["verdict"] == "learned"
    assert next(t for t in v["tests"] if t["id"] == "D")["pass"]
    assert s["counts"]["discovered"] == 8 and s["counts"]["skipped"] == 4
    assert s["counts"]["documents_analyzed"] == 3 and s["counts"]["images_analyzed"] == 1


def test_knowledge_survives_a_restart_and_a_new_model(folder, svc, fake, tmp_path):
    svc.learn(str(folder), domain="Design", background=False)
    fresh = KnowledgeStore(tmp_path / "data" / "knowledge")          # a new process
    block = retrieve.context_block("a landing page with generous whitespace", store=fresh)
    assert "whitespace" in block and "brand-guidelines.md" in block
    assert "Design" in retrieve.brief(fresh)


def test_updating_the_folder_reprocesses_only_what_changed(folder, svc, fake):
    svc.learn(str(folder), domain="Design", background=False)
    first = fake.extract_calls
    (folder / "tutorial-ui.md").unlink()
    (folder / "notes" / "design-preferences.txt").write_text(
        "- Never use more than two typefaces\n- Prefer a 12-column grid\n", encoding="utf-8")
    (folder / "v2.md").write_text("- Keep line length under 75 characters\n", encoding="utf-8")
    s = svc.status(svc.learn(str(folder), domain="Design", background=False)["id"])
    c = s["counts"]
    assert (c["new"], c["changed"], c["deleted"], c["to_process"], c["unchanged"]) == (1, 1, 1, 2, 2)
    assert fake.extract_calls == first + 1                          # one batch, not the whole folder
    k = svc.store.knowledge(svc.store.find_domain("Design")["id"])
    by = {i["statement"]: i for i in k["items"]}
    assert by["Keep one primary action per screen"]["status"] == "retired"
    assert by["Prefer a 12-column grid"]["status"] == "active"
    ws = next(i for i in k["items"] if "whitespace" in i["statement"] and i["status"] == "active")
    assert {x["rel"] for x in ws["support"]} == {"brand-guidelines.md"}


def test_model_outage_stops_resumably_and_never_claims_success(folder, svc, fake):
    fake.fail_after = 0
    s = svc.status(svc.learn(str(folder), domain="Design", background=False)["id"])
    assert s["status"] == "waiting_for_model" and "Nothing is lost" in s["message"]
    assert svc.store.find_domain("Design")["status"] != "learned"
    fake.fail_after = None
    svc.resume(s["id"], background=False)
    assert svc.status(s["id"])["status"] == "done"
    assert svc.store.find_domain("Design")["status"] == "learned"


def test_pause_then_continue(folder, svc, fake):
    real_phase = svc._phase

    def pausing(s, phase, **kw):
        if phase == "extracting knowledge":
            svc._pause[s["id"]].set()
        real_phase(s, phase, **kw)
    svc._phase = pausing
    s = svc.learn(str(folder), domain="Design", background=False)
    assert svc.status(s["id"])["status"] == "paused"
    svc._phase = real_phase
    svc.resume(s["id"], background=False)
    assert svc.status(s["id"])["status"] == "done"


def test_a_corrupted_file_is_reported_and_the_rest_still_learned(folder, svc, fake):
    (folder / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
    s = svc.status(svc.learn(str(folder), domain="Design", background=False)["id"])
    assert "broken.pdf" in s["failed"]
    assert "1 file(s) failed" in s["message"]
    assert svc.store.find_domain("Design")["status"] == "learned"


def test_verification_failure_means_not_learned(folder, svc, fake, monkeypatch):
    def bad_grader(prompt, **kw):
        if "Grade an assistant" in prompt:
            return json.dumps({"pass": False, "reason": "generic"}), "fake"
        return fake(prompt, **kw)
    monkeypatch.setattr(model, "generate", bad_grader)
    s = svc.status(svc.learn(str(folder), domain="Design", background=False)["id"])
    assert svc.store.find_domain("Design")["status"] == "unverified"
    assert s["message"].startswith("Analyzed but NOT verified")


def test_scopes_and_intent_guards(folder, svc):
    with pytest.raises(ValueError):
        svc.learn(str(folder), domain="Design", scope="global")
    with pytest.raises(ValueError):
        svc.learn(str(folder), domain="Design", scope="project")
    with pytest.raises(FileNotFoundError):
        svc.learn(str(folder / "missing"), domain="Design")


# ── the model's door ─────────────────────────────────────────────────────────
def test_model_tool_learn_status_recall_why_forget(folder, svc, fake, monkeypatch):
    real_learn = svc.learn
    monkeypatch.setattr(svc, "learn", lambda *a, **k: real_learn(*a, **{**k, "background": False}))
    out = json.loads(execute("learn", {"path": str(folder), "domain": "Design"}, svc=svc))
    assert "NOT learned yet" in out["say"]
    assert json.loads(execute("status", {}, svc=svc))["verification"]["verdict"] == "learned"
    recall = json.loads(execute("recall", {"query": "whitespace", "domain": "Design"}, svc=svc))
    assert recall[0]["sources"] == ["brand-guidelines.md", "notes/design-preferences.txt"]
    why = json.loads(execute("why", {"statement": "generous whitespace around primary content"}, svc=svc))
    assert why[0]["evidence"][0]["file"] in ("brand-guidelines.md", "notes/design-preferences.txt")
    assert "can't trace" in execute("why", {"statement": "zebra stripes quantum"}, svc=svc)
    assert execute("forget", {"domain": "Design"}, svc=svc) == "Forgotten."
    assert execute("list", {}, svc=svc) == "Nothing has been learned yet."


def test_consolidate_merge_keeps_every_source():
    a = {"id": "k1", "kind": "principle", "statement": "Use generous whitespace around content",
         "confidence": 0.65, "support": [{"rel": "a.md", "rank": 40}]}
    b = {"id": "k2", "kind": "rule", "statement": "Always use generous whitespace around content",
         "confidence": 0.65, "support": [{"rel": "b.md", "rank": 60}]}
    [m] = consolidate.merge([a, b])
    assert m["kind"] == "rule" and m["source_count"] == 2 and m["confidence"] > 0.65
