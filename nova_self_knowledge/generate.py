"""CLI for context/self/nova.md.

    python -m nova_self_knowledge.generate --render          # print, don't write
    python -m nova_self_knowledge.generate --refresh         # write to disk
    python -m nova_self_knowledge.generate --check           # warn, exit 0
    python -m nova_self_knowledge.generate --check --strict  # exit 1 on any finding
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .drift import check as drift_check
from .generators import GENERATORS
from .parser import parse, render

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "context" / "self" / "nova.md"


def _run_generators() -> dict[str, str]:
    """Run every generator, isolated -- one crashing must not take the
    others down with it, or a single flaky generator blocks every commit."""
    out: dict[str, str] = {}
    for name, fn in GENERATORS.items():
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = f"_unavailable, regenerate manually: {name} raised {e}_"
    return out


def rendered_doc(doc_path: Path = DOC_PATH) -> str:
    if not doc_path.exists():
        raise FileNotFoundError(f"{doc_path} does not exist — nothing to render")
    original = doc_path.read_text(encoding="utf-8")
    blocks = parse(original)
    overrides = _run_generators()
    from .parser import detect_line_ending
    return render(blocks, overrides, line_ending=detect_line_ending(original))


def slim_summary() -> str:
    """~200-400 token grounding block for every turn's system prompt.

    Not identity or principles -- NOVA_CORE already covers those
    exhaustively, and restating them here would just be the same prose
    twice. What NOVA_CORE cannot cover, because it is static text that
    does not change when the registries do, is *which* sub-agents and
    integrations actually exist right now. Tool names are deliberately
    left out: they already reach the model as real function-calling
    schemas (TOOL_DECLARATIONS passed to the API), which is a stronger
    grounding than prose repeating the same names could ever be -- listing
    them again here would be redundant, not additional safety.

    Falls back to a short, honest "unavailable" note rather than raising:
    a broken self-knowledge summary must never be the reason NOVA fails
    to start a turn.
    """
    try:
        agents_md = GENERATORS["subagents"]()
        integrations_md = GENERATORS["integrations"]()
    except Exception as e:
        return (f"[Self-knowledge summary unavailable this turn: {e}. "
                f"Do not guess at sub-agents or integrations; say you are "
                f"not sure rather than inventing one.]")

    def _names_from_table(md: str, col: int) -> list[str]:
        names = []
        for line in md.splitlines():
            if not line.startswith("|") or line.startswith("| ---"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) > col and cells[col] not in ("Agent", "Integration"):
                names.append(cells[col].strip("`"))
        return names

    agent_names = _names_from_table(agents_md, 0)
    integration_rows = [
        line for line in integrations_md.splitlines()
        if line.startswith("|") and not line.startswith("| ---")
        and "Integration" not in line
    ]
    configured = [
        row.split("|")[1].strip() for row in integration_rows
        if "configured" in row.lower() and "not installed" not in row.lower()
    ]

    return (
        "Self-knowledge (generated from the current codebase, not "
        "memorised -- trust this over any prior assumption about your "
        "own capabilities):\n"
        f"- Sub-agents that actually exist: {', '.join(agent_names) or 'none'}.\n"
        f"- Integrations currently configured: {', '.join(configured) or 'none'}.\n"
        "- Your callable tools are provided to you directly as function "
        "declarations -- consult those, not this summary, for what you "
        "can invoke."
    )


def cmd_render() -> int:
    sys.stdout.write(rendered_doc())
    return 0


def cmd_refresh() -> int:
    new_text = rendered_doc()
    old_text = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
    if new_text == old_text:
        print(f"{DOC_PATH}: already up to date")
        return 0
    DOC_PATH.write_text(new_text, encoding="utf-8")
    print(f"{DOC_PATH}: refreshed")
    return 0


def cmd_check(strict: bool) -> int:
    if not DOC_PATH.exists():
        print(f"{DOC_PATH}: does not exist")
        return 1 if strict else 0

    # Auto blocks: is what's on disk what the generators would produce
    # right now? A stale AUTO block is drift too, just not the prose kind.
    current = DOC_PATH.read_text(encoding="utf-8")
    fresh = rendered_doc()
    auto_stale = current != fresh

    findings = drift_check(current)

    if not findings and not auto_stale:
        print("context/self/nova.md: no drift found")
        return 0

    if auto_stale:
        print("STALE: AUTO blocks do not match what the generators produce "
              "right now. Run `python -m nova_self_knowledge.generate --refresh`.")

    for f in findings:
        print(f"{f.kind}: {f.reference!r} — {f.reason}\n  near: ...{f.location_in_doc}...")

    if strict and (findings or auto_stale):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nova_self_knowledge.generate")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--render", action="store_true")
    group.add_argument("--refresh", action="store_true")
    group.add_argument("--check", action="store_true")
    p.add_argument("--strict", action="store_true",
                   help="with --check, exit non-zero on any finding")
    args = p.parse_args(argv)

    if args.render:
        return cmd_render()
    if args.refresh:
        return cmd_refresh()
    if args.check:
        return cmd_check(strict=args.strict)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
