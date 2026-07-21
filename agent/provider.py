"""Thin seam between the harness and LLM providers."""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Iterator, Protocol

from dotenv import load_dotenv

from agent.types import GenerateResult, ToolCall, TurnMessage

load_dotenv()


class LLMProvider(Protocol):
    """Send a conversation; optionally with tools."""

    def complete(
        self,
        messages: list[TurnMessage],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerateResult:
        ...

    def stream_reply(
        self,
        messages: list[TurnMessage],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[str]:
        ...


class ProviderError(Exception):
    """Raised when the model is unreachable or misconfigured."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


def _new_tool_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:12]}"


class ClaudeProvider:
    def __init__(self, model: str | None = None) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ProviderError(
                "ANTHROPIC_API_KEY is not set. Add it to .env or set AGENT_PROVIDER=gemini."
            )
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ProviderError(
                "anthropic package not installed. Run: pip install anthropic"
            ) from exc

        self._client = Anthropic(api_key=api_key)
        self._model = model or os.getenv("AGENT_MODEL", "claude-sonnet-4-20250514")

    def _to_claude_messages(self, messages: list[TurnMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "content": msg["content"]})
            elif role == "assistant":
                content: list[dict[str, Any]] = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for tc in msg.get("tool_calls") or []:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["arguments"],
                        }
                    )
                out.append({"role": "assistant", "content": content or ""})
            elif role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg["tool_call_id"],
                                "content": msg["content"],
                            }
                        ],
                    }
                )
        return out

    def _parse_response(self, response: Any) -> GenerateResult:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )
        text = "".join(text_parts).strip() or None
        return GenerateResult(text=text, tool_calls=tool_calls)

    def complete(
        self,
        messages: list[TurnMessage],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerateResult:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system,
            "messages": self._to_claude_messages(messages),
        }
        if tools:
            kwargs["tools"] = tools
        try:
            response = self._client.messages.create(**kwargs)
            return self._parse_response(response)
        except Exception as exc:
            raise ProviderError(f"Claude request failed: {exc}") from exc

    def stream_reply(
        self,
        messages: list[TurnMessage],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[str]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system,
            "messages": self._to_claude_messages(messages),
        }
        if tools:
            kwargs["tools"] = tools
        try:
            with self._client.messages.stream(**kwargs) as stream:
                yield from stream.text_stream
        except Exception as exc:
            raise ProviderError(f"Claude request failed: {exc}") from exc


class GeminiProvider:
    """Evolution path: reuse existing Nova Gemini credentials."""

    def __init__(self, model: str | None = None) -> None:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ProviderError(
                "GEMINI_API_KEY is not set. Add it to .env or set AGENT_PROVIDER=claude."
            )
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ProviderError(
                "google-genai not installed. Run: pip install google-genai"
            ) from exc

        self._client = genai.Client(api_key=api_key)
        self._types = types
        self._model = model or os.getenv("AGENT_MODEL", "gemini-2.0-flash")

    def _to_gemini_tools(self, tools: list[dict[str, Any]] | None) -> list[Any] | None:
        if not tools:
            return None
        declarations = []
        for tool in tools:
            props = {}
            for key, val in tool.get("input_schema", {}).get("properties", {}).items():
                props[key] = self._types.Schema(
                    type=self._types.Type.STRING,
                    description=val.get("description", ""),
                )
            declarations.append(
                self._types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool["description"],
                    parameters=self._types.Schema(
                        type=self._types.Type.OBJECT,
                        properties=props,
                        required=tool.get("input_schema", {}).get("required", []),
                    ),
                )
            )
        return [self._types.Tool(function_declarations=declarations)]

    def _to_gemini_contents(self, messages: list[TurnMessage]) -> list[Any]:
        contents: list[Any] = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                contents.append(
                    self._types.Content(
                        role="user", parts=[self._types.Part(text=msg["content"])]
                    )
                )
            elif role == "assistant":
        try:
                parts: list[Any] = []
        except ImportError as exc:
            raise ProviderError(
                "google-generativeai package not installed. Run: pip install google-generativeai"
            ) from exc
                if msg.get("content"):
                    parts.append(self._types.Part(text=msg["content"]))
                for tc in msg.get("tool_calls") or []:
                    parts.append(
                        self._types.Part(
                            function_call=self._types.FunctionCall(
                                name=tc["name"],
                                args=tc["arguments"],
                            )
                        )
                    )
                contents.append(self._types.Content(role="model", parts=parts))
            elif role == "tool":
                contents.append(
                    self._types.Content(
                        role="user",
                        parts=[
                            self._types.Part(
                                function_response=self._types.FunctionResponse(
                                    name=msg["name"],
                                    response={"output": msg["content"]},
                                )
                            )
                        ],
                    )
                )
        return contents

    def _parse_response(self, response: Any) -> GenerateResult:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return GenerateResult(text=None, tool_calls=[])

        parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
        for part in parts:
            if getattr(part, "text", None):
                text_parts.append(part.text)
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                tool_calls.append(
                    ToolCall(
                        id=_new_tool_id(),
                        name=fc.name,
                        arguments=dict(getattr(fc, "args", None) or {}),
                    )
                )

        text = "".join(text_parts).strip() or None
        return GenerateResult(text=text, tool_calls=tool_calls)

    def complete(
        self,
        messages: list[TurnMessage],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerateResult:
        gemini_tools = self._to_gemini_tools(tools)
        config = self._types.GenerateContentConfig(system_instruction=system)
        if gemini_tools:
            config.tools = gemini_tools
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=self._to_gemini_contents(messages),
                config=config,
            )
            return self._parse_response(response)
        except Exception as exc:
            raise ProviderError(f"Gemini request failed: {exc}") from exc

    def stream_reply(
        self,
        messages: list[TurnMessage],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[str]:
        gemini_tools = self._to_gemini_tools(tools)
        config = self._types.GenerateContentConfig(system_instruction=system)
        if gemini_tools:
            config.tools = gemini_tools
        try:
            stream = self._client.models.generate_content_stream(
                model=self._model,
                contents=self._to_gemini_contents(messages),
                config=config,
            )
            for chunk in stream:
                if chunk.text:
                    yield chunk.text
        except Exception as exc:
            raise ProviderError(f"Gemini request failed: {exc}") from exc


def _last_user_index(messages: list[TurnMessage]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return -1


def _has_tool_results_this_turn(messages: list[TurnMessage]) -> bool:
    start = _last_user_index(messages)
    if start < 0:
        return False
    return any(msg.get("role") == "tool" for msg in messages[start + 1 :])


class MockProvider:
    """Offline stub that simulates tool selection and multi-step replies."""

    def complete(
        self,
        messages: list[TurnMessage],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerateResult:
        del system, tools
        last_user = _last_user_text(messages)
        if _has_tool_results_this_turn(messages):
            return GenerateResult(text=_mock_answer_after_tools(messages, last_user))

        tool_call = _mock_pick_tool(last_user)
        if tool_call:
            return GenerateResult(text=None, tool_calls=[tool_call])

        return GenerateResult(text=_mock_plain_reply(messages, last_user))

    def stream_reply(
        self,
        messages: list[TurnMessage],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[str]:
        result = self.complete(messages, system, tools)
        if result.tool_calls:
            return iter(())
        text = result.text or ""
        for word in text.split():
            yield word + " "


def _last_user_text(messages: list[TurnMessage]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return msg["content"]
    return ""


def _tool_results_text(messages: list[TurnMessage]) -> str:
    start = _last_user_index(messages)
    scope = messages[start + 1 :] if start >= 0 else messages
    return " ".join(
        msg.get("content", "") for msg in scope if msg.get("role") == "tool"
    )


def _mock_pick_tool(user_text: str) -> ToolCall | None:
    lower = user_text.lower()

    if any(p in lower for p in ("what do you remember", "what's on my list", "my list for", "remember about me")):
        return ToolCall(id=_new_tool_id(), name="list_session_facts", arguments={})

    remember_match = re.match(
        r"^(?:remember(?:\s+that)?|don't forget(?:\s+that)?|note that)\s+(.+)",
        user_text.strip(),
        re.IGNORECASE,
    )
    if remember_match:
        return ToolCall(
            id=_new_tool_id(),
            name="remember_fact",
            arguments={"fact": remember_match.group(1).strip()},
        )

    search_match = re.search(
        r"(?:search for|look up|find out about|what is the)\s+(.+)",
        user_text,
        re.IGNORECASE,
    )
    if search_match or lower.startswith("search "):
        query = (search_match.group(1) if search_match else user_text).strip(" ?.")
        return ToolCall(
            id=_new_tool_id(), name="web_search", arguments={"query": query}
        )

    open_match = re.search(
        r"(?:open|launch|start)\s+(.+)",
        user_text,
        re.IGNORECASE,
    )
    if open_match:
        app = open_match.group(1).strip(" ?.")
        return ToolCall(
            id=_new_tool_id(), name="open_app", arguments={"app_name": app}
        )

    return None


def _mock_answer_after_tools(messages: list[TurnMessage], last_user: str) -> str:
    del last_user
    results = _tool_results_text(messages)
    start = _last_user_index(messages)
    scope = messages[start + 1 :] if start >= 0 else messages
    last_tool = next(
        (msg.get("name") for msg in reversed(scope) if msg.get("role") == "tool"),
        "",
    )

    if last_tool == "list_session_facts":
        if "No facts remembered" in results:
            return "I don't have anything on your list yet this session."
        return f"Here's what I have noted: {results.replace('Session facts:', '').strip()}"

    if "Tool '" in results and "failed" in results:
        return (
            "I tried that but hit a problem — "
            + results.split("failed:", 1)[-1].strip()
            + " Want to try another way?"
        )

    if last_tool == "web_search":
        return f"Here's what I found: {results}"

    if last_tool == "open_app":
        return results.replace("[mock] ", "")

    if last_tool == "remember_fact":
        return "Got it — I'll keep that in mind for this session."

    return f"Done. {results}"


def _mock_plain_reply(messages: list[TurnMessage], last_user: str) -> str:
    prior = " ".join(
        m["content"]
        for m in messages[:-1]
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ).lower()

    if "what color" in last_user.lower() and "teal" in prior:
        return "You said teal."
    if "favorite color" in last_user.lower():
        return "Got it — I'll remember teal for this session."

    return f"You said: {last_user}"


def create_provider() -> LLMProvider:
    """Pick provider from AGENT_PROVIDER or whichever API key is available."""
    choice = os.getenv("AGENT_PROVIDER", "").strip().lower()

    if choice == "mock":
        return MockProvider()
    if choice == "claude":
        return ClaudeProvider()
    if choice == "gemini":
        return GeminiProvider()

    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return ClaudeProvider()
    if os.getenv("GEMINI_API_KEY", "").strip():
        return GeminiProvider()

    raise ProviderError(
        "No model API key found. Set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env, "
        "or set AGENT_PROVIDER=claude|gemini|mock."
    )

def is_mock_provider(provider: LLMProvider) -> bool:
    return isinstance(provider, MockProvider)
