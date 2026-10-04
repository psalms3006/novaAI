"""
NOVA Capability Registry — canonical metadata + uniform dispatch.

Consolidates the previous surfaces:

- ``core/capability_bus.py``  (capability metadata)
- ``agent/tools/registry.py`` (typed tool handlers)
- ``nova.py:TOOL_DECLARATIONS`` (Gemini tool schema)
- ``tools/__init__.py``       (safety tiering)

The registry adds a single ``metadata_all()`` / ``schemas_*()`` surface for
the planner and adds a uniform ``execute()`` dispatch so callers stop owning
long ``if/elif`` chains. It is non-destructive: an optional ``fallback_fn``
(name, args, meta) -> str routes capabilities this registry does not own to
the existing live runtime (``nova._execute_tool_sync``).
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from capabilities.adapters import resolve_dynamic_handler
from capabilities.contracts import (
    CapabilityContext,
    CapabilitySpec,
    CapabilityStatus,
    ConfirmationPolicy,
    RiskLevel,
)

log = logging.getLogger("nova.capabilities.registry")

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "this", "that", "with",
    "on", "in", "at", "is", "are", "my", "me", "i", "you", "your", "it", "do",
    "from", "into", "about", "use", "when", "asked", "then",
}

FallbackFn = Callable[[str, Dict[str, Any], Dict[str, Any]], str]


class CapabilityNotFoundError(LookupError):
    """Raised when a capability cannot be resolved for execution."""


class CapabilityUnavailableError(RuntimeError):
    """Raised when a resolved capability cannot run on this platform."""


@dataclass
class CapabilityRegistry:
    """Registry of capability specs with metadata + dispatch services."""

    fallback_fn: Optional[FallbackFn] = None
    platform: str = "windows"
    _specs: Dict[str, CapabilitySpec] = field(default_factory=dict, init=False)

    # ── registration ──────────────────────────────────────────────────

    def register(self, spec: CapabilitySpec) -> None:
        if spec.name in self._specs:
            log.warning("Capability '%s' re-registered (overwriting).", spec.name)
        self._specs[spec.name] = spec

    def get(self, name: str) -> Optional[CapabilitySpec]:
        return self._specs.get(name)

    def names(self) -> List[str]:
        return list(self._specs.keys())

    # ── metadata services ─────────────────────────────────────────────

    def metadata_all(self) -> List[Dict[str, Any]]:
        return [spec.to_metadata() for spec in self._specs.values()]

    def schemas_gemini(self) -> List[Dict[str, Any]]:
        """Tool declarations in the Gemini/nova.py ``TOOL_DECLARATIONS`` shape."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters or {"type": "object", "properties": {}},
            }
            for spec in self._specs.values()
        ]

    def schemas_openai(self) -> List[Dict[str, Any]]:
        """Tool schema in OpenAI function-calling shape."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": self._to_openai_schema(spec.parameters),
                },
            }
            for spec in self._specs.values()
        ]

    @staticmethod
    def _to_openai_schema(parameters: Dict[str, Any]) -> Dict[str, Any]:
        props = parameters.get("properties", {})
        return {
            "type": "object",
            "properties": {
                k: {"type": v.get("type", "string").lower(), "description": v.get("description", "")}
                for k, v in props.items()
            },
            "required": parameters.get("required", []),
            "additionalProperties": False,
        }

    # ── intent matching ───────────────────────────────────────────────

    def find_for_intent(self, text: str, platform: Optional[str] = None) -> Optional[CapabilitySpec]:
        """Best-effort capability match for a natural-language intent.

        Used by the heuristic planner fallback — keyword/name matching only,
        never a substitute for the LLM planner when one is configured. Scores
        candidates on word overlap + quoted-phrase hits (e.g. "where am i").
        """
        lower = (text or "").lower()

        # Name matches dominate: "where am i" should hit location.get, not
        # whatever shares a generic word.
        for spec in self._specs.values():
            short = spec.name.split(".")[-1]
            if spec.name in lower or (short and short in lower):
                return spec

        best: Optional[CapabilitySpec] = None
        best_score = 0
        for spec in self._specs.values():
            desc_tokens = re.sub(r"[^a-z0-9 ]", " ", spec.description.lower()).split()
            desc_words = {t for t in desc_tokens if len(t) > 2 and t not in _STOPWORDS}
            text_words = {
                t for t in re.sub(r"[^a-z0-9 ]", " ", lower).split()
                if len(t) > 2 and t not in _STOPWORDS
            }
            overlap = len(text_words & desc_words)

            bigrams = {" ".join(desc_tokens[i:i + 2]) for i in range(len(desc_tokens) - 1)}
            phrase_hits = sum(1 for p in bigrams if p in lower)

            score = 3 * overlap + 2 * phrase_hits
            if score > best_score:
                best, best_score = spec, score

        return best if best_score > 0 else None

    # ── availability ──────────────────────────────────────────────────

    def available_names(self, is_online: bool = True) -> List[str]:
        out = []
        for name, spec in self._specs.items():
            if spec.requires_internet and not is_online:
                continue
            if not spec.is_available_on(self.platform):
                continue
            out.append(name)
        return out

    def platform_status(self, name: str) -> CapabilityStatus:
        spec = self.get(name)
        if spec is None:
            return CapabilityStatus.UNAVAILABLE
        if spec.is_available_on(self.platform):
            return CapabilityStatus.AVAILABLE
        return CapabilityStatus.UNAVAILABLE

    # ── dispatch ──────────────────────────────────────────────────────

    def execute(self, name: str, args: Dict[str, Any], ctx: Optional[CapabilityContext] = None) -> str:
        """Dispatch a capability. Raises when resolution fails (never fabricates)."""
        ctx = ctx or CapabilityContext(platform=self.platform)
        spec = self.get(name)

        if spec is not None and spec.handler is not None:
            return str(spec.handler(args, ctx))

        if spec is None:
            if self.fallback_fn is not None:
                return str(self.fallback_fn(name, args, ctx.meta or {}))
            dynamic = resolve_dynamic_handler(name)
            if dynamic is not None:
                return str(dynamic(args, ctx))
            raise CapabilityNotFoundError(
                f"Unknown capability '{name}'. Available: {', '.join(self.names()) or '(none)'}."
            )

        if not spec.is_available_on(ctx.platform or self.platform):
            raise CapabilityUnavailableError(
                f"Capability '{name}' is not available on platform '{ctx.platform or self.platform}'."
            )

        # The configured dispatcher comes first, and the dynamic `actions/`
        # adapter is only the last resort.
        #
        # It used to be the other way round. The adapter imports
        # `actions/<tool>.py` and calls its `execute()` directly, while the
        # authorisation check and the confirmation prompt live in the
        # dispatcher (`nova._execute_tool_sync`). So on the typed-chat
        # surface, every tool that happened to have an `actions/` module --
        # `file_controller` and `computer_settings` among them, both
        # consequential -- reached the action without anyone being asked.
        # The voice surface, which calls the dispatcher directly, refused the
        # same call correctly.
        #
        # Nothing is lost by the reordering: the dispatcher's own final branch
        # does the same `importlib.import_module(f"actions.{tool}")` and adds
        # the audit line the adapter never wrote.
        if self.fallback_fn is not None:
            return str(self.fallback_fn(name, args, ctx.meta or {}))
        dynamic = resolve_dynamic_handler(name)
        if dynamic is not None:
            return str(dynamic(args, ctx))

        raise CapabilityNotFoundError(
            f"Capability '{name}' has no handler and no fallback dispatcher is configured."
        )

    # ── iteration ─────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self._specs)

    def __iter__(self):
        return iter(self._specs.values())


# ── Canonical metadata table ───────────────────────────────────────────────
# Risk and confirmation levels follow NOVA's Trust/Risk spec (§8).
#
# Tools whose execution path stays on the live runtime (nova.py) are
# metadata-only here: they resolve through ``fallback_fn`` when supplied.

def _sessions_specs() -> List[CapabilitySpec]:
    def _remember_fact(args: Dict[str, Any], ctx: CapabilityContext) -> str:
        """Persist a fact the user asked NOVA to remember.

        This used to append to ``ctx.session_facts`` and nothing else. In the
        desktop runtime a fresh CapabilityContext is built for every tool call,
        so that list was discarded the moment the call returned: NOVA replied
        "Remembered: ..." and stored nothing, and the fact was gone from the
        next turn onward. Writing through to the real memory subsystems is what
        makes "remember this" actually mean something across sessions.
        """
        fact = str(args.get("fact", "")).strip()
        if not fact:
            return "No fact provided."

        ctx.session_facts.append(fact)

        stored_anywhere = False
        meta = getattr(ctx, "meta", None) or {}

        # Structured/living memory (survives restarts, supports supersession).
        try:
            import nova_state
            living = getattr(nova_state, "_living_memory", None)
            if living is not None:
                living.remember(fact, source="user", confirmed=True, importance=0.8)
                stored_anywhere = True
        except Exception as e:  # pragma: no cover - defensive
            log.warning("living memory write failed for remember_fact: %s", e)

        # Flat semantic fact store (used to build the prompt's MEMORY block).
        try:
            import nova as _nova
            add_fact = getattr(_nova, "add_memory_fact", None)
            if callable(add_fact):
                add_fact(fact, meta if isinstance(meta, dict) else {})
                stored_anywhere = True
        except Exception as e:  # pragma: no cover - defensive
            log.warning("semantic memory write failed for remember_fact: %s", e)

        if not stored_anywhere:
            # Never claim a durable write that did not happen.
            return (f"Noted for this session only: {fact} "
                    "(persistent memory is unavailable right now).")
        return f"Remembered: {fact}"

    def _list_facts(_args: Dict[str, Any], ctx: CapabilityContext) -> str:
        if not ctx.session_facts:
            return "No facts remembered this session yet."
        return "Session facts:\n" + "\n".join(
            f"{i}. {fact}" for i, fact in enumerate(ctx.session_facts, 1)
        )

    return [
        CapabilitySpec(
            name="remember_fact",
            description="Store a fact about the user for this session.",
            handler=_remember_fact,
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string", "description": "One clear statement to remember."}},
                "required": ["fact"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.NEVER,
            reversible=True,
            verification_hint="Remembered",
            agent="orchestrator",
        ),
        CapabilitySpec(
            name="list_session_facts",
            description="List facts remembered about the user during this session.",
            handler=_list_facts,
            parameters={"type": "object", "properties": {}},
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.NEVER,
            reversible=True,
            verification_hint="Session facts",
            agent="orchestrator",
        ),
    ]


def _actions_specs() -> List[CapabilitySpec]:
    """Metadata for the ``actions.*``-backed tools (dynamic handlers)."""
    return [
        CapabilitySpec(
            name="web_search",
            description=(
                "Search the web for current information, facts, news, or anything "
                "that needs an internet lookup."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query — be specific."}},
                "required": ["query"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            requires_internet=True,
            verification_hint="found|results|sources",
            agent="research",
        ),
        CapabilitySpec(
            name="open_app",
            description="Open or launch an application on this device.",
            parameters={
                "type": "object",
                "properties": {"app_name": {"type": "string", "description": "Application name, e.g. 'Notepad'."}},
                "required": ["app_name"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            verification_hint="Opened",
            agent="computer",
        ),
        CapabilitySpec(
            name="close_app",
            description="Close a running application on this device.",
            parameters={
                "type": "object",
                "properties": {"app_name": {"type": "string", "description": "Application name or process."}},
                "required": ["app_name"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            verification_hint="closed|Closed",
            agent="computer",
        ),
        CapabilitySpec(
            name="file_controller",
            description="Manage files and folders: list, read, write, create, delete, find, copy, move.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "list | read | write | create_file | delete | find | info | copy | move"},
                    "path": {"type": "string", "description": "File or folder path (shortcuts: desktop, downloads, home)."},
                    "content": {"type": "string", "description": "Content for write/create_file."},
                    "name": {"type": "string", "description": "File name to find."},
                    "destination": {"type": "string", "description": "Destination for copy/move."},
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.MEDIUM,
            confirmation_policy=ConfirmationPolicy.AUTO,
            action_risk_overrides={
                "delete": RiskLevel.HIGH,
                "move": RiskLevel.HIGH,
                "write": RiskLevel.HIGH,
                "create_file": RiskLevel.MEDIUM,
            },
            safe_actions=["list", "read", "find", "info"],
            reversible=False,
            verification_hint="saved|created|deleted|list|content",
            agent="code",
        ),
        CapabilitySpec(
            name="browser_control",
            description="Control web browser navigation and interaction.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "open_url | search | youtube | github | maps | refresh | go_back | new_tab."},
                    "url": {"type": "string", "description": "URL for open_url."},
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.MEDIUM,
            confirmation_policy=ConfirmationPolicy.AUTO,
            requires_internet=True,
            verification_hint="browser|opened|loaded|search",
            agent="browser",
        ),
        CapabilitySpec(
            name="computer_settings",
            description="Read or change system settings (volume, brightness, screenshot, power).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "screenshot | volume_up | volume_down | volume_mute | brightness_up | brightness_down | lock | shutdown | restart | sleep | type | hotkey."},
                    "value": {"type": "string", "description": "Optional value/text/keys."},
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.HIGH,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            action_risk_overrides={
                "shutdown": RiskLevel.CRITICAL,
                "restart": RiskLevel.HIGH,
                "lock": RiskLevel.MEDIUM,
                "sleep": RiskLevel.MEDIUM,
            },
            safe_actions=["screenshot"],
            reversible=False,
            verification_hint="current value|changed|setting",
            agent="computer",
        ),
        CapabilitySpec(
            name="file_processor",
            description="Process files: images, PDFs, documents, spreadsheets, code, audio, video.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Full path to the file."},
                    "action": {"type": "string", "description": "describe | summarize | extract_text | analyze | stats | explain | review | convert."},
                },
                "required": ["file_path"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            reversible=True,
            verification_hint="File|content|summary|analysis",
            agent="code",
        ),
        CapabilitySpec(
            name="vision",
            description="Capture and analyze screen or camera image input.",
            parameters={
                "type": "object",
                "properties": {
                    "angle": {"type": "string", "description": "screen | camera."},
                    "question": {"type": "string", "description": "What to ask about the image."},
                },
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            reversible=False,
            verification_hint="see|describe|detected|text",
            agent="vision",
        ),
        CapabilitySpec(
            name="screen_process",
            description="Analyze or extract text from the current screen.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "What to analyze or ask about the screen."}},
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            reversible=False,
            verification_hint="screen|visible",
            agent="vision",
        ),
        CapabilitySpec(
            name="dev_agent",
            description="Plan, scaffold, write, and run a software project on disk.",
            parameters={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What to build."},
                    "language": {"type": "string", "description": "Target language (optional)."},
                },
                "required": ["description"],
            },
            risk_level=RiskLevel.HIGH,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            reversible=False,
            verification_hint="project|created|built|codespace",
            agent="code",
        ),
    ]


def _fallback_specs() -> List[CapabilitySpec]:
    """Metadata-only specs that route to the live runtime via ``fallback_fn``."""
    return [
        CapabilitySpec(
            name="self_editor",
            description="Read, edit, patch, or restore NOVA's own source code with backups.",
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string", "description": "read | patch | restart | list_backups | restore_backup."}},
                "required": ["action"],
            },
            risk_level=RiskLevel.HIGH,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            safe_actions=["read", "list_backups"],
            reversible=True,
            verification_hint="patched|backup|read",
            agent="code",
        ),
        CapabilitySpec(
            name="autostart",
            description="Control whether NOVA starts automatically at Windows sign-in.",
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string", "description": "setup | remove | status."}},
                "required": ["action"],
            },
            risk_level=RiskLevel.HIGH,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            safe_actions=["status"],
            reversible=True,
            verification_hint="startup|registered",
            agent="system",
        ),
        CapabilitySpec(
            name="planner",
            description="Manage reminders and scheduled tasks.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "add | list | cancel | clear_done."},
                    "description": {"type": "string", "description": "What to remind about."},
                    "time": {"type": "string", "description": "When."},
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            reversible=True,
            verification_hint="reminder|task|scheduled",
            agent="orchestrator",
        ),
        CapabilitySpec(
            name="computer_control",
            description="Direct mouse/keyboard control and window management.",
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string", "description": "click | type | hotkey | press | scroll | screenshot | focus_window."}},
                "required": ["action"],
            },
            risk_level=RiskLevel.HIGH,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            reversible=False,
            verification_hint="clicked|typed|focused|screenshot",
            agent="computer",
        ),
        CapabilitySpec(
            name="send_message",
            description="Send a message on the user's behalf on a messaging platform.",
            parameters={
                "type": "object",
                "properties": {
                    "receiver": {"type": "string"},
                    "message_text": {"type": "string"},
                    "platform": {"type": "string"},
                },
                "required": ["receiver", "message_text", "platform"],
            },
            risk_level=RiskLevel.MEDIUM,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            reversible=False,
            verification_hint="sent|delivered",
            agent="computer",
        ),
    ]


def _extended_specs() -> List[CapabilitySpec]:
    """Metadata for the remaining tools the live runtime dispatches.

    These are metadata-only: execution still resolves through the legacy
    dispatcher (``nova._execute_tool_sync`` / ``execute_extra_tool``) or the
    dynamic adapters. Registering them closes the shadow-monitor's
    ``no-metadata`` blind spot so every live call gets a risk-tiered verdict.
    Risk/confirmation align with ``nova_safety``'s consequential-tool gate.
    """
    return [
        CapabilitySpec(
            name="game_updater",
            description="Install or update games, or list installed game downloads.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "install | update | list | download_status."},
                    "game": {"type": "string", "description": "Game name."},
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.MEDIUM,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            safe_actions=["list", "download_status"],
            requires_internet=True,
            reversible=True,
            verification_hint="game|updated|installed|download",
            agent="computer",
        ),
        CapabilitySpec(
            name="cmd_control",
            description="Run a command line or script on this device.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The command or script to run."}},
                "required": ["command"],
            },
            risk_level=RiskLevel.HIGH,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            reversible=False,
            verification_hint="executed|ran|command|completed",
            agent="computer",
        ),
        CapabilitySpec(
            name="code_helper",
            description="Write, explain, or generate code in a project folder.",
            parameters={
                "type": "object",
                "properties": {"description": {"type": "string", "description": "What the code should do."}},
                "required": ["description"],
            },
            risk_level=RiskLevel.MEDIUM,
            confirmation_policy=ConfirmationPolicy.AUTO,
            reversible=True,
            verification_hint="code|helper|generated|written",
            agent="code",
        ),
        CapabilitySpec(
            name="desktop_control",
            description="Change desktop wallpaper or organise desktop files.",
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string", "description": "wallpaper | organize | clean."}},
                "required": ["action"],
            },
            risk_level=RiskLevel.MEDIUM,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            reversible=True,
            verification_hint="wallpaper|organized|cleaned",
            agent="computer",
        ),
        CapabilitySpec(
            name="flight_finder",
            description="Search for flights between airports or cities.",
            parameters={
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["from", "to"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            requires_internet=True,
            reversible=True,
            verification_hint="flight|results|found",
            agent="research",
        ),
        CapabilitySpec(
            name="generated_code",
            description=(
                "Internal-only: generate and run arbitrary Python code for a task. "
                "Never emitted by the planner — appears only via error recovery."
            ),
            parameters={
                "type": "object",
                "properties": {"description": {"type": "string", "description": "What the code should do."}},
                "required": ["description"],
            },
            risk_level=RiskLevel.CRITICAL,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            reversible=False,
            verification_hint="completed|output",
            agent="code",
        ),
        CapabilitySpec(
            name="reminder",
            description="Add, list, or cancel reminders and scheduled tasks.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "add | list | cancel | clear_done."},
                    "description": {"type": "string"},
                    "time": {"type": "string"},
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            reversible=True,
            verification_hint="reminder|scheduled|cancelled",
            agent="orchestrator",
        ),
        CapabilitySpec(
            name="weather_report",
            description="Report the current weather for a city or area.",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City or area name."}},
                "required": ["city"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            requires_internet=True,
            reversible=True,
            verification_hint="weather|temperature|forecast",
            agent="research",
        ),
        CapabilitySpec(
            name="youtube_video",
            description="Open or play a YouTube video by search or URL.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Video title or URL."}},
                "required": ["query"],
            },
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.AUTO,
            requires_internet=True,
            reversible=True,
            verification_hint="video|youtube|playing|opened",
            agent="browser",
        ),
    ]


def build_core_registry(
    fallback_fn: Optional[FallbackFn] = None,
    platform: str = "windows",
) -> CapabilityRegistry:
    """Build the canonical registry with all known capability metadata.

    ``fallback_fn`` — when provided, capabilities without a native handler
    (self_editor, autostart, planner, computer_control, send_message) dispatch
    through it, e.g. ``nova._execute_tool_sync``.
    """
    registry = CapabilityRegistry(fallback_fn=fallback_fn, platform=platform)

    for spec in _sessions_specs():
        registry.register(spec)
    for spec in _actions_specs():
        registry.register(spec)
    for spec in _fallback_specs():
        registry.register(spec)
    for spec in _extended_specs():
        registry.register(spec)

    from capabilities.location import get_spec as get_location_spec

    registry.register(get_location_spec())

    log.info("Capability registry built: %d capabilities.", registry.count)
    return registry