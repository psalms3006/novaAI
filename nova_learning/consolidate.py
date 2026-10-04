"""nova_learning.consolidate — cross-reference, patterns, contradictions.

Items from different batches that say the same thing become one item with
all its sources; how many independent sources support an item, and how
authoritative they are, sets its confidence.

Contradictions are never silently settled (§101). A conflict is resolved
only when there is a reason: one side comes from a clearly more
authoritative source (§102), or is clearly more recent. Otherwise both stay,
marked contested, and NOVA says the material disagrees.
"""
from __future__ import annotations

import re
import uuid

from . import model

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "is", "are", "be",
         "use", "using", "should", "it", "its", "that", "this", "as", "by", "at", "from", "than"}
_DIRECTIVE = {"principle", "preference", "rule"}


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 2}


def similar(a: str, b: str, threshold: float = 0.6) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def confidence(item: dict) -> float:
    files = {s["rel"] for s in item.get("support", [])}
    base = max(item.get("confidence", 0.5), 0.45)
    rank = max((s.get("rank", 35) for s in item.get("support", [])), default=35)
    boost = 0.1 * max(0, len(files) - 1) + (0.05 if rank >= 60 else 0)
    return round(min(0.97, base + boost), 2)


def _same_family(a: str, b: str) -> bool:
    return a == b or ({a, b} <= _DIRECTIVE)


def merge(items: list) -> list:
    """Collapse near-duplicate statements, keeping every source."""
    out: list = []
    for it in items:
        twin = None
        for o in out:
            if _same_family(o["kind"], it["kind"]) and similar(o["statement"], it["statement"]):
                twin = o
                break
        if twin is None:
            out.append(dict(it, support=list(it.get("support", []))))
            continue
        seen = {(s["rel"], s.get("quote", "")) for s in twin["support"]}
        twin["support"] += [s for s in it.get("support", []) if (s["rel"], s.get("quote", "")) not in seen]
        twin["confidence"] = max(twin.get("confidence", 0.5), it.get("confidence", 0.5))
        if it["kind"] in ("rule", "preference") and twin["kind"] == "principle":
            twin["kind"] = it["kind"]                  # the stronger reading wins
    for it in out:
        it["confidence"] = confidence(it)
        it["source_count"] = len({s["rel"] for s in it["support"]})
    return out


CONFLICT_PROMPT = """Below are knowledge items learned from the user's material for the domain "{domain}".
List only pairs of items that DIRECTLY CONTRADICT each other: following one means breaking the other (opposite instructions, or incompatible values for the same thing). Items about different subjects never conflict, and neither do items that merely differ in emphasis or add detail.
{listing}
Return JSON only: {{"conflicts":[{{"a":"<id>","b":"<id>","note":"what conflicts, in one sentence"}}]}}"""


def find_conflicts(domain: str, items: list) -> list:
    active = [i for i in items if i.get("status") == "active"]
    if len(active) < 2:
        return []
    listing = "\n".join(f"{i['id']}: [{i['kind']}] {i['statement']}" for i in active[:120])
    data, _ = model.ask_json(CONFLICT_PROMPT.format(domain=domain, listing=listing))
    ids = {i["id"] for i in active}
    out = []
    for c in (data.get("conflicts") if isinstance(data, dict) else None) or []:
        if isinstance(c, dict) and c.get("a") in ids and c.get("b") in ids and c["a"] != c["b"]:
            a = next(i for i in active if i["id"] == c["a"])
            b = next(i for i in active if i["id"] == c["b"])
            if {s["rel"] for s in a["support"]} == {s["rel"] for s in b["support"]}:
                continue            # one document rarely contradicts itself; this is a misread
            out.append({"a": c["a"], "b": c["b"], "note": str(c.get("note") or "")[:300]})
    return out


def _authority(item: dict) -> tuple:
    sup = item.get("support", [])
    top = max(sup, key=lambda s: s.get("rank", 35)) if sup else {}
    return (top.get("rank", 35), max((s.get("mtime", 0) for s in sup), default=0),
            top.get("authority", ""))


def resolve(items: list, conflicts: list, extracted: list) -> list:
    """Attach extraction-time contradictions, then decide what can be decided."""
    by_id = {i["id"]: i for i in items}
    for c in extracted:
        a = next((i for i in items if similar(i["statement"], c["a"], 0.45)), None)
        b = next((i for i in items if similar(i["statement"], c["b"], 0.45)), None)
        if a and b and a is not b and not any({x["a"], x["b"]} == {a["id"], b["id"]} for x in conflicts):
            conflicts.append({"a": a["id"], "b": b["id"], "note": c.get("note", "")})
    out, done = [], set()
    for c in conflicts:
        a, b = by_id.get(c["a"]), by_id.get(c["b"])
        key = frozenset((c["a"], c["b"]))
        if not a or not b or key in done:
            continue
        done.add(key)
        (ra, ta, la), (rb, tb, lb) = _authority(a), _authority(b)
        rec = {"id": "c_" + uuid.uuid4().hex[:8], "a": a["id"], "b": b["id"],
               "a_statement": a["statement"], "b_statement": b["statement"],
               "a_sources": sorted({s["rel"] for s in a["support"]}),
               "b_sources": sorted({s["rel"] for s in b["support"]}),
               "note": c.get("note", ""), "resolved_by": "", "winner": ""}
        # The source hierarchy (§102) is the policy: any difference in rank
        # decides it -- the person's own material outranks a generic tutorial.
        if ra != rb:
            win, lose, why = (a, b, la) if ra > rb else (b, a, lb)
            rec.update(resolved_by="source authority", winner=win["id"],
                       explanation=f"the {why} outranks the other source")
            lose["status"] = "superseded"
            lose["superseded_by"] = win["id"]
        elif abs(ta - tb) > 7 * 86400:
            win, lose = (a, b) if ta > tb else (b, a)
            rec.update(resolved_by="recency", winner=win["id"],
                       explanation="the more recent source is followed")
            lose["status"] = "superseded"
            lose["superseded_by"] = win["id"]
        else:
            a["contested"] = b["contested"] = True
            rec["explanation"] = "the material disagrees and gives no basis to choose"
        out.append(rec)
    return out


__all__ = ["merge", "similar", "find_conflicts", "resolve", "confidence"]
