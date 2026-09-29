"""nova_skills.discovery — "I can't do that yet" is the start of an investigation.

Given something the person wants done, in their words, discovery answers in
order and stops at the first that works:

  1. existing   Is there a learned, healthy capability for it already?
  2. compose    Can NOVA's current tools do it as a workflow? (planner)
  3. catalog    What does the NOVA backend's shared catalog know -- providers
                other installations have validated for this kind of work
                (technical metadata only, never anyone's credentials)?
  4. research   What does the web suggest? Leads are screened for red flags
                and marked unread: NOVA must read their documentation before
                treating a lead as an option.

The report is honest about which stage produced what. Nothing here connects,
installs, spends or stores anything.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .registry import CapabilityRegistry
from .safety import evaluate_option, screen_text

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "to", "for", "of", "and", "or", "my", "me", "i", "want", "make",
         "create", "do", "can", "you", "please", "nova", "some", "this", "that", "with", "it"}


def _terms(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


@dataclass
class Report:
    need: str
    existing: list = field(default_factory=list)      # [{id, name, health}]
    composed: Optional[dict] = None                    # {"workflow": [...], "tools": [...]}
    catalog: list = field(default_factory=list)       # evaluated catalog options
    leads: list = field(default_factory=list)         # screened web leads (unread)
    stage: str = "none"                                # existing|compose|catalog|research|none

    def to_dict(self) -> dict:
        return asdict(self)

    def say(self) -> str:
        """What NOVA can honestly tell the person at this point."""
        if self.stage == "existing":
            c = self.existing[0]
            return f"I can already do that ({c['name']})."
        if self.stage == "compose":
            tools = ", ".join(self.composed["tools"])
            return (f"I can do that with what I have ({tools}). I haven't tested it as a "
                    f"reusable workflow yet; I'll test it before relying on it.")
        if self.stage == "catalog":
            best = self.catalog[0]
            return (f"I don't have that yet, but I know a route: {best['name']}. "
                    f"{best['evaluation']['summary']}")
        if self.stage == "research":
            return (f"I don't have a reliable way to do that yet. I found {len(self.leads)} "
                    f"possible routes that I still need to read up on before I can say which works.")
        return "I checked, and I couldn't find a way to do that with anything I can reach."


def match_existing(need: str, registry: CapabilityRegistry, min_overlap: int = 2) -> list:
    want = _terms(need)
    out = []
    for cap in registry.usable():
        have = _terms(cap.name + " " + cap.description)
        overlap = len(want & have)
        if overlap >= min(min_overlap, len(want)) and overlap > 0:
            out.append((overlap, {"id": cap.id, "name": cap.name, "health": cap.health}))
    return [c for _, c in sorted(out, key=lambda x: -x[0])]


def compose(need: str, declarations: list, planner: Callable) -> Optional[dict]:
    """A workflow over NOVA's own tools, or None if her tools don't cover it."""
    try:
        plan = planner(need, tool_declarations=declarations) or {}
    except Exception:
        return None
    known = {d.get("name") for d in declarations or []}
    steps = []
    for st in plan.get("steps") or []:
        tool = st.get("tool")
        if not tool or tool not in known:
            return None                      # the plan leans on something she doesn't have
        steps.append({"tool": tool, "args": dict(st.get("parameters") or st.get("args") or {})})
    # A single web search is the planner's fallback for "no idea", not a workflow.
    if not steps or (len(steps) == 1 and steps[0]["tool"] == "web_search"):
        return None
    return {"workflow": steps, "tools": sorted({s["tool"] for s in steps})}


def rank_catalog(need: str, catalog: list) -> list:
    want = _terms(need)
    out = []
    for entry in catalog or []:
        tags = _terms(" ".join([entry.get("name", ""), entry.get("description", ""),
                                " ".join(entry.get("capabilities", []))]))
        overlap = len(want & tags)
        if not overlap:
            continue
        ev = evaluate_option(entry)
        item = {k: entry.get(k) for k in ("id", "name", "description", "kind", "interface",
                                           "cost", "requires_account", "docs_url", "setup")}
        item["evaluation"] = {"risk": ev.risk, "blocked": ev.blocked,
                              "needs_approval": ev.needs_approval, "reasons": ev.reasons,
                              "summary": ev.summary()}
        out.append((overlap, ev.blocked, item))
    # Usable before blocked, then by relevance.
    return [i for _, _, i in sorted(out, key=lambda x: (x[1], -x[0]))]


def screen_leads(results: list) -> list:
    """Web results as leads: kept with their red flags, never acted on."""
    leads = []
    for r in results or []:
        text = " ".join(str(r.get(k, "")) for k in ("title", "snippet", "text"))
        flags = [f.what for f in screen_text(text)]
        leads.append({"title": r.get("title", "")[:160], "url": r.get("url", ""),
                      "red_flags": flags, "status": "unread"})
    return leads


def discover(need: str, registry: CapabilityRegistry, *, declarations: list | None = None,
             planner: Optional[Callable] = None, catalog: Optional[list] = None,
             search: Optional[Callable[[str], list]] = None) -> Report:
    rep = Report(need=need)
    rep.existing = match_existing(need, registry)
    if rep.existing:
        rep.stage = "existing"
        return rep
    if planner and declarations:
        rep.composed = compose(need, declarations, planner)
        if rep.composed:
            rep.stage = "compose"
            return rep
    rep.catalog = rank_catalog(need, catalog or [])
    if rep.catalog and not rep.catalog[0]["evaluation"]["blocked"]:
        rep.stage = "catalog"
        return rep
    if search:
        try:
            rep.leads = screen_leads(search(need))
        except Exception:
            rep.leads = []
        if rep.leads:
            rep.stage = "research"
    return rep


__all__ = ["discover", "Report", "match_existing", "compose", "rank_catalog", "screen_leads"]
