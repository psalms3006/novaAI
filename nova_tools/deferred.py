"""nova_tools.deferred — keep the tool list small; keep tool output bounded.

Deferred loading. Every tool's full schema is sent to the model on every
turn. NOVA's own 19 tools are about 4k tokens and stay loaded -- a voice
assistant has to act on the first try. MCP servers are different: each one
the person connects can add dozens of tools, and past a budget they crowd out
the conversation in the voice model's 131k window. Beyond `BUDGET_CHARS`,
MCP tools are replaced by two small tools:

    find_tool {query}          up to five matching tools, with their parameters
    use_tool  {name, args}     call one of them

`use_tool` is unwrapped at the dispatcher into the real tool name *before*
hooks, permission checks and confirmation run, so a deferred tool is governed
exactly like a loaded one -- the proxy grants nothing.

Output cap. A tool result over `OUTPUT_LIMIT` characters is saved to a file
and the model gets the beginning plus the file's path, instead of an answer
that fills its context. Registered as a post-hook for every tool.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

BUDGET_CHARS = int(os.getenv("NOVA_MCP_TOOL_BUDGET_CHARS", "24000") or 24000)
OUTPUT_LIMIT = int(os.getenv("NOVA_TOOL_OUTPUT_LIMIT", "20000") or 20000)
MAX_RESULTS = 5

FIND = "find_tool"
USE = "use_tool"

_deferred: dict = {}          # name -> declaration


FIND_DECLARATION = {
    "name": FIND,
    "description": ("Search the extra tools from services the user connected (MCP) that are not "
                    "listed directly. Returns up to five matching tools with their parameters; call "
                    "one with use_tool. Use it when none of your listed tools fits the request."),
    "parameters": {"type": "OBJECT",
                   "properties": {"query": {"type": "STRING",
                                            "description": "What the tool should do, in a few words"}},
                   "required": ["query"]},
}

USE_DECLARATION = {
    "name": USE,
    "description": "Call a tool found with find_tool, by its exact name, with its arguments.",
    "parameters": {"type": "OBJECT",
                   "properties": {"name": {"type": "STRING", "description": "Exact tool name from find_tool"},
                                  "args": {"type": "OBJECT", "description": "The tool's arguments"}},
                   "required": ["name"]},
}


def _size(decls: list) -> int:
    return len(json.dumps(decls, ensure_ascii=False))


def arrange(mcp_decls: list, budget: int | None = None) -> list:
    """What to add to the model's tool list for these MCP tools."""
    budget = BUDGET_CHARS if budget is None else budget
    _deferred.clear()
    if not mcp_decls or _size(mcp_decls) <= budget:
        return list(mcp_decls or [])
    for d in mcp_decls:
        _deferred[d["name"]] = d
    return [FIND_DECLARATION, USE_DECLARATION]


def is_deferred(name: str) -> bool:
    return name in _deferred


def deferred_names() -> list:
    return sorted(_deferred)


_WORD = re.compile(r"[a-z0-9]+")


def find(query: str, limit: int = MAX_RESULTS) -> list:
    want = set(_WORD.findall((query or "").lower()))
    scored = []
    for name, d in _deferred.items():
        hay = set(_WORD.findall((name + " " + d.get("description", "")).lower().replace("_", " ")))
        score = len(want & hay) + (2 if any(w in name.lower() for w in want) else 0)
        if score:
            scored.append((score, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [_deferred[n] for _, n in scored[:limit]]


def find_text(query: str) -> str:
    hits = find(query)
    if not hits:
        return (f"No connected tool matches {query!r}. {len(_deferred)} deferred tools are "
                f"available; try other words.")
    return json.dumps([{"name": d["name"], "description": d.get("description", ""),
                        "parameters": d.get("parameters", {})} for d in hits], ensure_ascii=False)


def unwrap(tool: str, args: dict) -> tuple:
    """use_tool -> (real name, real args). Anything else passes through."""
    if tool != USE:
        return tool, args
    name = str((args or {}).get("name") or "").strip()
    inner = (args or {}).get("args") or {}
    if not isinstance(inner, dict):
        inner = {}
    return name, inner


def _outputs_dir() -> Path:
    base = os.getenv("NOVA_DATA_DIR", "").strip() or "."
    d = Path(base) / "tool_outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cap_output(tool: str, args: dict, result, meta: dict):
    """Post-hook: bound what goes back to the model."""
    if not isinstance(result, str) or len(result) <= OUTPUT_LIMIT:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool)[:40] or "tool"
    path = _outputs_dir() / f"{int(time.time() * 1000)}-{safe}.txt"
    try:
        path.write_text(result, encoding="utf-8")
        where = f"The full result ({len(result):,} characters) is saved at {path}."
    except OSError:
        where = f"The full result was {len(result):,} characters and could not be saved."
    head = result[: OUTPUT_LIMIT // 2]
    return f"{head}\n\n[... output cut here. {where} Ask for a specific part if you need more.]"


def install_hooks() -> None:
    from nova_core import hooks
    hooks.remove(cap_output)
    hooks.add_post(cap_output, "*", name="cap_output")


__all__ = ["arrange", "is_deferred", "deferred_names", "find", "find_text", "unwrap", "cap_output",
           "install_hooks", "FIND", "USE", "FIND_DECLARATION", "USE_DECLARATION"]
