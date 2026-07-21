"""Tool registry — extend by registering self-contained tools, not editing the brain loop."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any], "ToolContext"], str]


@dataclass
class ToolContext:
    """Mutable per-session state tools may read or write."""

    session_facts: list[str] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    requires_confirmation: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.parameters,
            }
            for spec in self._tools.values()
        ]

    def run(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
        spec = self._tools.get(name)
        if spec is None:
            return f"Unknown tool '{name}'. Available: {', '.join(self._tools)}."

        try:
            return spec.handler(arguments, ctx)
        except Exception as exc:
            return f"Tool '{name}' failed: {exc}"


def _web_search_handler(args: dict[str, Any], _ctx: ToolContext) -> str:
    if os.getenv("AGENT_MOCK_FAIL_WEB_SEARCH", "").strip().lower() in ("1", "true", "yes"):
        raise RuntimeError("Mock search backend unavailable (AGENT_MOCK_FAIL_WEB_SEARCH).")

    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: query is required."

    from actions.web_search import web_search

    return web_search({"query": query})


def _open_app_handler(args: dict[str, Any], _ctx: ToolContext) -> str:
    app_name = str(args.get("app_name", "")).strip()
    if not app_name:
        return "Error: app_name is required."

    from actions.open_app import execute

    return execute({"app_name": app_name})


def _remember_fact_handler(args: dict[str, Any], ctx: ToolContext) -> str:
    fact = str(args.get("fact", "")).strip()
    if not fact:
        return "Error: fact is required."

    ctx.session_facts.append(fact)
    return f"Remembered for this session: {fact}"


def _list_session_facts_handler(_args: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.session_facts:
        return "No facts remembered this session yet."
    lines = [f"{i}. {fact}" for i, fact in enumerate(ctx.session_facts, 1)]
    return "Session facts:\n" + "\n".join(lines)


def _mock_web_search_handler(args: dict[str, Any], _ctx: ToolContext) -> str:
    if os.getenv("AGENT_MOCK_FAIL_WEB_SEARCH", "").strip().lower() in ("1", "true", "yes"):
        raise RuntimeError("Mock search backend unavailable.")

    query = str(args.get("query", "")).strip()
    canned = {
        "capital of france": "Paris is the capital of France.",
        "python": "Python is a popular programming language.",
    }
    key = query.lower()
    for fragment, answer in canned.items():
        if fragment in key:
            return answer
    return f"[mock search] Top result for '{query}': sample answer with relevant details."


def _mock_open_app_handler(args: dict[str, Any], _ctx: ToolContext) -> str:
    app_name = str(args.get("app_name", "")).strip()
    return f"[mock] Opened {app_name}."


def build_default_registry(*, mock: bool = False) -> ToolRegistry:
    registry = ToolRegistry()

    search_handler = _mock_web_search_handler if mock else _web_search_handler
    open_handler = _mock_open_app_handler if mock else _open_app_handler

    registry.register(
        ToolSpec(
            name="web_search",
            description=(
                "Search the web for current information, facts, news, or anything "
                "that needs an internet lookup. Use when the user asks about recent "
                "events or facts you are not sure of."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query — be specific.",
                    }
                },
                "required": ["query"],
            },
            handler=search_handler,
            requires_confirmation=False,
        )
    )

    registry.register(
        ToolSpec(
            name="open_app",
            description=(
                "Open or launch an application on this Windows computer. Use when "
                "the user asks to open, start, or launch a program (Chrome, Notepad, "
                "VS Code, Spotify, etc.)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Application name, e.g. 'Notepad', 'Chrome', 'VS Code'.",
                    }
                },
                "required": ["app_name"],
            },
            handler=open_handler,
            requires_confirmation=False,
        )
    )

    registry.register(
        ToolSpec(
            name="remember_fact",
            description=(
                "Store a fact about the user for this session (preferences, name, "
                "habits). Use when they say 'remember that…' or ask you to note something."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "One clear statement to remember, e.g. 'Prefers morning meetings'.",
                    }
                },
                "required": ["fact"],
            },
            handler=_remember_fact_handler,
            requires_confirmation=False,
        )
    )

    registry.register(
        ToolSpec(
            name="list_session_facts",
            description=(
                "List facts remembered about the user during this session. Use when "
                "they ask what you remember or what's on their list of notes."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_list_session_facts_handler,
            requires_confirmation=False,
        )
    )

    return registry
