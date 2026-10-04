"""nova_skills.safety — what may be trusted, and what needs the person's yes.

Two jobs:

1. Screen anything NOVA read while researching (documentation, READMEs,
   search results, MCP server descriptions) for instructions that must never
   be followed on a page's say-so: run as administrator, disable security
   software, pipe a download into a shell, paste a key into a form, ignore
   previous instructions. Found text is evidence about the source, never an
   instruction to NOVA.

2. Decide what approval an option needs before it is connected. Autonomy is
   the default for research, comparison and safe local tests; the person is
   asked only where it matters -- money, their accounts, private data leaving
   the machine, code from the internet running here. Some things are refused
   outright: elevation, disabling protections, a provider that wants the whole
   disk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# (pattern, severity, what it is)
_RED_FLAGS = [
    (r"\b(run|launch|start)\b[^.\n]{0,40}\bas (an )?administrator\b|\brunas\b|\bsudo\b|\belevat(e|ed|ion)\b", "critical",
     "asks for administrator / elevated rights"),
    (r"\b(disable|turn off|deactivate)\b[^.\n]{0,30}\b(antivirus|defender|firewall|smartscreen|uac)\b", "critical",
     "asks to disable security protections"),
    (r"(curl|wget)[^|\n]{0,200}\|\s*(ba)?sh\b|\biwr\b[^|\n]{0,200}\|\s*iex\b|invoke-expression|powershell[^\n]{0,20}-e(nc(odedcommand)?)?\s", "critical",
     "pipes a download straight into a shell"),
    (r"\b(paste|enter|submit|send)\b[^.\n]{0,40}\b(api[ _-]?key|password|secret|token|private key|seed phrase)\b[^.\n]{0,40}\b(form|chat|here|below|comment|issue|public)\b", "critical",
     "asks for a credential to be pasted somewhere public"),
    (r"\bignore (all |any )?(previous|prior|above) instructions\b|\byou are now\b|\bsystem prompt\b", "high",
     "contains instructions aimed at an AI (prompt injection)"),
    (r"\b(download|run|execute|open)\b[^.\n]{0,60}\.(exe|msi|bat|cmd|scr|ps1|vbs)\b", "high",
     "asks for an executable to be downloaded and run"),
    (r"\brm\s+-rf\s+/|\bformat\s+[a-z]:|\bdel\s+/s\s+/q\s+[a-z]:\\|\breg(\.exe)?\s+add\s+hklm", "critical",
     "destructive system command"),
    (r"\bchmod\s+(-R\s+)?777\b", "high", "makes files writable by everyone"),
]
_COMPILED = [(re.compile(p, re.I), sev, what) for p, sev, what in _RED_FLAGS]


@dataclass
class Finding:
    severity: str
    what: str
    excerpt: str


def screen_text(text: str) -> list:
    """Red flags in text NOVA read. Evidence about the source, not orders."""
    out = []
    for rx, sev, what in _COMPILED:
        m = rx.search(text or "")
        if m:
            start = max(0, m.start() - 30)
            out.append(Finding(sev, what, (text[start:m.end() + 30]).replace("\n", " ").strip()))
    return out


@dataclass
class Evaluation:
    risk: str                                   # low | medium | high | critical
    blocked: bool = False
    needs_approval: list = field(default_factory=list)   # what the person must say yes to
    reasons: list = field(default_factory=list)

    def summary(self) -> str:
        if self.blocked:
            return "I won't use this: " + "; ".join(self.reasons)
        if self.needs_approval:
            return "Needs your OK for: " + ", ".join(self.needs_approval)
        return "Safe to set up without asking."


_BROAD_PATHS = re.compile(r"^([a-z]:\\?|/|~|%userprofile%|c:\\users\\?)$", re.I)


def evaluate_option(option: dict) -> Evaluation:
    """Decide how an integration may be adopted.

    option keys (all optional): cost, requires_account, data_leaves_device,
    runs_code_locally, elevated, source_text, mcp_roots, license_commercial.
    """
    ev = Evaluation(risk="low")
    findings = screen_text(option.get("source_text", ""))
    crit = [f for f in findings if f.severity == "critical"]
    if crit or option.get("elevated"):
        ev.blocked, ev.risk = True, "critical"
        ev.reasons += [f.what for f in crit] or ["needs administrator rights"]
        return ev
    for f in findings:
        ev.reasons.append(f"its documentation {f.what}")
        ev.risk = "high"
    roots = [str(r).strip() for r in option.get("mcp_roots", []) or []]
    if any(_BROAD_PATHS.match(r) for r in roots):
        ev.blocked, ev.risk = True, "critical"
        ev.reasons.append("it asks for access to your whole disk; a narrower folder is needed")
        return ev
    cost = str(option.get("cost", "") or "").lower()
    if cost and cost not in ("free", "open source", "open-source", "local"):
        ev.needs_approval.append(f"spending money ({option.get('cost')})")
        ev.risk = max(ev.risk, "medium", key=_rank)
    if option.get("requires_account"):
        ev.needs_approval.append("connecting your account")
        ev.risk = max(ev.risk, "medium", key=_rank)
    if option.get("data_leaves_device"):
        ev.needs_approval.append("sending your content to an outside service")
        ev.risk = max(ev.risk, "medium", key=_rank)
    if option.get("runs_code_locally"):
        ev.needs_approval.append("running third-party code on this computer (inspected and trialled first)")
        ev.risk = max(ev.risk, "high", key=_rank)
    if option.get("license_commercial") is False:
        ev.reasons.append("its licence does not allow commercial use of the output")
    return ev


def _rank(r: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(r, 0)


__all__ = ["screen_text", "evaluate_option", "Evaluation", "Finding"]
