"""Drift checker for context/self/nova.md's hand-written prose.

Scans the hand-written (non-AUTO) blocks for backtick-quoted references
that look like a file path (`desk/live_session.py`) or a dotted symbol
(`nova_agents.get_all_agents`), and verifies each still exists in the
codebase — a file path via Path.exists(), a symbol via a lightweight AST
scan of the module file it names (no import, for the same reason the
generators avoid importing nova.py/agents_extra.py: cost and circular
imports).

References the checker should ignore -- future-tense promises, examples
from a third-party doc, deliberately approximate wording -- go in
context/self/.nova-allowlist.txt, one substring per line.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .parser import Block, parse

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "context" / "self" / ".nova-allowlist.txt"

_BACKTICK_RE = re.compile(r"`([^`]+)`")
# A bare word like `speaking` or `--refresh` is not a reference this
# checker can verify against anything; only check things shaped like a
# path (has a slash or a known code extension) or a dotted symbol
# (identifier.identifier, no spaces, no parens content to worry about).
_PATH_LIKE_RE = re.compile(r"^[\w\-./]+\.(py|js|md|json|html|css)(::\w+)?$")
_DOTTED_SYMBOL_RE = re.compile(
    r"^(?P<module>[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)*)"
    r"\.(?P<symbol>[a-zA-Z_][\w]*)(\(\))?$"
)

# Module dotted-path prefixes this repo actually uses, so `nova.py` (a
# path) is not confused with `nova.SOMETHING` (a module reference) --
# both share the "nova" prefix, and only the second should resolve
# through the module-file lookup below.
_KNOWN_MODULE_FILES = {
    "nova": "nova.py",
    "offline_extra": "offline_extra.py",
    "nova_agents": "nova_agents.py",
    "agents_extra": "agents_extra.py",
    "nova_voice": "nova_voice.py",
    "nova_confirm": "nova_confirm.py",
    "nova_safety": "nova_safety.py",
    "terminal_voice": "terminal_voice.py",
    "live_extra": "live_extra.py",
}


@dataclass
class DriftFinding:
    kind: str          # "missing_file" or "missing_symbol"
    reference: str      # the exact backtick-quoted text
    location_in_doc: str  # a short excerpt of the prose it appeared in
    reason: str


def _load_allowlist() -> list[str]:
    if not ALLOWLIST_PATH.exists():
        return []
    lines = []
    for line in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _is_allowlisted(reference: str, allowlist: list[str]) -> bool:
    return any(entry in reference for entry in allowlist)


def _resolve_repo_path(ref: str) -> Path:
    ref = ref.split("::")[0]
    return REPO_ROOT / ref


def _symbol_exists_in_module_file(module: str, symbol: str) -> bool | None:
    """True/False if resolvable, None if the module isn't one this checker
    knows how to locate (not a failure -- just not checkable, so callers
    should treat None as "skip, not a finding")."""
    filename = _KNOWN_MODULE_FILES.get(module)
    if filename is None:
        return None
    path = REPO_ROOT / filename
    if not path.exists():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return None  # can't parse it right now; not this checker's job to flag syntax errors
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets):
                return True
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == symbol:
                    return True
    return False


def check(doc_text: str) -> list[DriftFinding]:
    blocks = parse(doc_text)
    allowlist = _load_allowlist()
    findings: list[DriftFinding] = []

    for block in blocks:
        if block.kind != "prose":
            continue
        for match in _BACKTICK_RE.finditer(block.text):
            ref = match.group(1)
            if _is_allowlisted(ref, allowlist):
                continue

            excerpt_start = max(0, match.start() - 30)
            excerpt = block.text[excerpt_start:match.end() + 10].replace("\n", " ").strip()

            if _PATH_LIKE_RE.match(ref):
                path = _resolve_repo_path(ref)
                if not path.exists():
                    findings.append(DriftFinding(
                        kind="missing_file", reference=ref,
                        location_in_doc=excerpt,
                        reason=f"{path} does not exist"))
                continue

            m = _DOTTED_SYMBOL_RE.match(ref)
            if m:
                module, symbol = m.group("module"), m.group("symbol")
                exists = _symbol_exists_in_module_file(module, symbol)
                if exists is False:
                    findings.append(DriftFinding(
                        kind="missing_symbol", reference=ref,
                        location_in_doc=excerpt,
                        reason=f"{symbol!r} not found as a top-level def/class/"
                               f"assignment in {_KNOWN_MODULE_FILES[module]}"))
                continue
            # Not path-like or dotted-symbol-like: a flag, a bare word, a
            # short technical term. Not something this checker can verify
            # against a source of truth, so it is silently skipped rather
            # than guessed at.

    return findings
