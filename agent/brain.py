"""Tier 2 conversation loop — text in, tools, streamed text out."""

from __future__ import annotations

import sys
from typing import Callable, TextIO

from agent.prompts import build_system_prompt
from agent.identity import (
    get_assistant_name,
    is_identity_query,
    build_identity_response,
)
from agent.provider import LLMProvider, ProviderError, is_mock_provider
from agent.tools.registry import ToolContext, ToolRegistry, build_default_registry
from agent.types import GenerateResult, TurnMessage


class AgentBrain:
    """In-session conversation brain with tool registry (Tier 2)."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry or build_default_registry(mock=is_mock_provider(provider))
        self._ctx = ToolContext()
        self._history: list[TurnMessage] = []
        self._system = build_system_prompt()
        self._assistant_name = get_assistant_name()

    @property
    def history(self) -> list[TurnMessage]:
        return list(self._history)

    @property
    def tool_context(self) -> ToolContext:
        return self._ctx

    def _run_tool_loop(self, sink: TextIO) -> GenerateResult:
        """Call the model until it returns text instead of tool requests."""
        tool_schemas = self._registry.schemas()
        max_rounds = 8

        for _ in range(max_rounds):
            result = self._provider.complete(self._history, self._system, tool_schemas)

            if not result.has_tools:
                return result

            assistant_msg: TurnMessage = {
                "role": "assistant",
                "content": result.text or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in result.tool_calls
                ],
            }
            self._history.append(assistant_msg)

            for tc in result.tool_calls:
                args_preview = ", ".join(f"{k}={v!r}" for k, v in tc.arguments.items())
                sink.write(f"  -> {tc.name}({args_preview})\n")
                sink.flush()

                tool_result = self._registry.run(tc.name, tc.arguments, self._ctx)
                preview = tool_result.replace("\n", " ")[:120]
                sink.write(f"  <- {preview}{'...' if len(tool_result) > 120 else ''}\n")
                sink.flush()

                self._history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": tool_result,
                    }
                )

        return GenerateResult(
            text="I got stuck in a tool loop. Try rephrasing your request.",
            tool_calls=[],
        )

    def _stream_text(self, text: str, sink: TextIO) -> None:
        for word in text.split():
            sink.write(word + " ")
            sink.flush()

    def turn(self, user_text: str, out: TextIO | None = None) -> str:
        """Process one user message; run tools if needed; stream the final reply."""
        user_text = user_text.strip()
        if not user_text:
            return ""

        self._history.append({"role": "user", "content": user_text})
        sink = out or sys.stdout

        try:
            result = self._run_tool_loop(sink)

            if result.text:
                sink.write(f"{self._assistant_name}: ")
                sink.flush()
                self._stream_text(result.text, sink)
                sink.write("\n")
                sink.flush()
                reply = result.text.strip()
            else:
                reply = "I didn't get a response from the model. Try again."
                sink.write(f"{self._assistant_name}: {reply}\n")
                sink.flush()
                self._history.pop()
                return reply

        except ProviderError as exc:
            reply = str(exc)
            sink.write(f"{ASSISTANT_NAME}: {reply}\n")
            sink.flush()
            self._history.pop()
            return reply

        if reply:
            self._history.append({"role": "assistant", "content": reply})
        else:
            reply = "I didn't get a response from the model. Try again."
            sink.write(f"{reply}\n")
            sink.flush()
            self._history.pop()

        return reply

    def run_repl(
        self,
        *,
        read_line: Callable[[], str] | None = None,
        out: TextIO | None = None,
    ) -> None:
        """Interactive text loop until the user exits."""
        sink = out or sys.stdout
        reader = read_line or (lambda: input("You: "))

        tool_names = ", ".join(t["name"] for t in self._registry.schemas())
        sink.write(f"\n{self._assistant_name} (text mode — Tier 2)\n")
        sink.write(f"Tools: {tool_names}\n")
        sink.write("Type a message and press Enter. Commands: /quit, /clear, /facts\n\n")

        while True:
            try:
                raw = reader()
            except (EOFError, KeyboardInterrupt):
                sink.write("\n")
                break

            text = raw.strip()
            if not text:
                continue
            if text.lower() in ("/quit", "/exit", "quit", "exit"):
                break
            if text.lower() == "/clear":
                self._history.clear()
                self._ctx.session_facts.clear()
                sink.write("(conversation and session facts cleared)\n\n")
                continue
            if text.lower() == "/facts":
                sink.write(self._registry.run("list_session_facts", {}, self._ctx) + "\n\n")
                continue

            self.turn(text, out=sink)
            sink.write("\n")
