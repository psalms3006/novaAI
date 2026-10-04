"""nova_learning.verify — does NOVA actually know it? (§95-96)

Opening files, building an index or getting a summary back proves nothing.
After extraction NOVA is examined on the learned domain:

    A  retrieval     "what does this material emphasise about <topic>?"
    B  application   a scenario: which learned principles apply, and how?
    C  conflict      a proposal that breaks a learned rule: is it caught?
    D  grounding     "where did you learn <item>?" -- must cite a real source
    E  novel task    a new task that was not in the material

The questions are generated from the learned items (not hardcoded), the
answers come from the same retrieval-and-answer path NOVA uses in
conversation, and they are graded against the learned items. D is checked
deterministically: the answer must name at least one file the item actually
came from. A domain is "learned" only if D and C pass and at most one of A, B,
E fails; otherwise it stays "unverified" and NOVA says so.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import PurePosixPath
from typing import Optional

from . import model, retrieve
from .store import KnowledgeStore

GEN_PROMPT = """You are writing an exam that checks whether an assistant has really learned the domain "{domain}" from the knowledge items below.
{listing}

Write exactly these five tests as JSON, using ONLY what the items support:
- "A": a question asking what the material emphasises about one topic several items cover; "expect": the ids of the items a good answer must reflect.
- "B": a short realistic scenario (a task someone would bring) asking which learned principles apply and how; "expect": relevant item ids.
- "C": a concrete proposal that clearly BREAKS item {violate_id} ("{violate}"), phrased neutrally as a plan, asking "Does this follow the learned guidance?"; "expect": ["{violate_id}"].
- "E": a NEW task in this domain that is not described in the material, asking the assistant to approach it using what it learned; "expect": the item ids that should shape it.
Return JSON only: {{"A":{{"question":"...","expect":["id"]}},"B":{{...}},"C":{{...}},"E":{{...}}}}"""

JUDGE_PROMPT = """Grade an assistant's answer against knowledge it was supposed to have learned.
TEST TYPE: {kind}
QUESTION: {question}
KNOWLEDGE THE ANSWER SHOULD REFLECT:
{expected}
ANSWER:
{answer}

Criteria:
- retrieval/application/novel: PASS if the answer reflects most of the expected knowledge (paraphrase is fine), applies it sensibly, and does not contradict it. FAIL if it is generic advice that ignores the knowledge.
- conflict: PASS only if the answer clearly identifies that the proposal conflicts with the expected knowledge.
Return JSON only: {{"pass": true|false, "reason": "one sentence"}}"""


def _listing(items: list) -> str:
    return "\n".join(f"{i['id']}: [{i['kind']}] {i['statement']}" for i in items)


def _expected(items_by_id: dict, ids: list) -> str:
    return "\n".join(f"- {items_by_id[i]['statement']}" for i in ids if i in items_by_id) or "(none)"


def _judge(kind: str, question: str, expected: str, answer: str) -> dict:
    data, _ = model.ask_json(JUDGE_PROMPT.format(kind=kind, question=question,
                                                 expected=expected, answer=answer[:4000]))
    return {"pass": bool(isinstance(data, dict) and data.get("pass") is True),
            "reason": str((data or {}).get("reason", ""))[:300] if isinstance(data, dict) else ""}


def _grounded(answer: str, item: dict) -> tuple:
    names = {s["rel"] for s in item.get("support", [])}
    names |= {PurePosixPath(n).name for n in names}
    low = answer.lower()
    cited = sorted(n for n in names if n.lower() in low)
    return bool(cited), cited


def run(domain_id: str, *, store: Optional[KnowledgeStore] = None, seed: Optional[int] = None,
        progress=None) -> dict:
    store = store or KnowledgeStore()
    dom = store.domain(domain_id) or {"name": domain_id}
    items = [i for i in store.knowledge(domain_id).get("items", []) if i.get("status") == "active"]
    if len(items) < 3:
        return {"verdict": "failed", "passed": 0, "total": 0, "tests": [],
                "reason": "too little knowledge was extracted to test"}
    by_id = {i["id"]: i for i in items}
    rng = random.Random(seed)
    strong = sorted(items, key=lambda i: -i.get("confidence", 0))
    rules = [i for i in strong if i["kind"] in ("rule", "preference", "principle")] or strong
    violate = rng.choice(rules[:max(3, len(rules) // 3)])
    ground = rng.choice([i for i in strong[:max(4, len(strong) // 2)] if i is not violate] or strong)

    if progress:
        progress("writing the test")
    spec, _ = model.ask_json(GEN_PROMPT.format(domain=dom["name"], listing=_listing(items[:80]),
                                               violate_id=violate["id"], violate=violate["statement"]))
    spec = spec if isinstance(spec, dict) else {}
    spec["D"] = {"question": f"Why do you believe this: \"{ground['statement']}\"? "
                             f"Where did you learn it? Name the source.", "expect": [ground["id"]]}
    tests = []
    for key, kind in (("A", "retrieval"), ("B", "application"), ("C", "conflict"),
                      ("D", "grounding"), ("E", "novel")):
        t = spec.get(key) if isinstance(spec.get(key), dict) else None
        if not t or not t.get("question"):
            tests.append({"id": key, "kind": kind, "pass": False, "reason": "the test could not be written"})
            continue
        expect = [e for e in (t.get("expect") or []) if e in by_id] or ([violate["id"]] if key == "C" else [])
        if progress:
            progress(f"test {key} ({kind})")
        ans, used = retrieve.answer(domain_id, t["question"], store=store)
        if key == "D":
            ok, cited = _grounded(ans, ground)
            verdict = {"pass": ok, "reason": (f"cited {', '.join(cited)}" if ok else
                                              "the answer did not name a source this was learned from")}
        else:
            verdict = _judge(kind, t["question"], _expected(by_id, expect), ans)
        tests.append({"id": key, "kind": kind, "question": t["question"], "expect": expect,
                      "answer": ans[:2000], "model": used, **verdict})
    passed = sum(1 for t in tests if t["pass"])
    must = all(t["pass"] for t in tests if t["id"] in ("C", "D"))
    verdict = "learned" if must and passed >= 4 else "unverified"
    return {"verdict": verdict, "passed": passed, "total": len(tests), "tests": tests,
            "at": time.time()}


__all__ = ["run"]
