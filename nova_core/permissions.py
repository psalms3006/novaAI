"""nova_core.permissions — capability-based authorisation for NOVA.

The question this module answers is narrow and specific:

    "May *this principal* use *this capability*, given where the request
     came from?"

It does not replace what already works. `nova_safety.safety_gate` still asks
the user to confirm consequential tools, and `desk/confirm.py` still drives
that from the UI settings. Those decide **whether the human agrees**. This
module decides **whether the caller was ever allowed to ask** -- which is a
different question, and the one that has to be answered before any agent can
be given autonomy.

Three ideas carry the design.

**Capabilities, not tool names.** A grant is expressed as `FILE_WRITE`, not
as a list of tool names, so adding a tool cannot silently widen an agent's
reach. A tool declares the capabilities it needs; an agent is granted a set;
the intersection is checked.

**Trust travels with the request.** Text that came from a web page, a PDF or
an email is `UNTRUSTED`. A request influenced by untrusted content can read,
but cannot act: `WRITE`-class capabilities are downgraded to "ask the human"
and the most dangerous ones are refused outright. This is the enforcement
half of prompt-injection defence -- detection alone (which nova_safety
already does) cannot stop an instruction that looks innocuous.

**Fail closed.** An unknown capability, an unknown principal, or a tool that
declares nothing is denied. Adding a capability without granting it breaks
loudly at development time rather than quietly widening access in production.
"""
from __future__ import annotations

import enum
import fnmatch
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

log = logging.getLogger("nova.permissions")


# -- capabilities ------------------------------------------------------------

class Capability(str, enum.Enum):
    """What a principal may do. Deliberately coarse: a list long enough to
    need a search box is a list nobody audits."""

    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    FILE_DELETE = "FILE_DELETE"
    CODE_EXECUTE = "CODE_EXECUTE"
    PROCESS_CONTROL = "PROCESS_CONTROL"
    #: Starting an application, as distinct from killing one. Launching
    #: Spotify because the user said "open Spotify" is not the same act as
    #: terminating a process, and charging both at the same rate meant NOVA
    #: asked permission to do the thing she had just been told to do.
    APP_LAUNCH = "APP_LAUNCH"
    NETWORK_READ = "NETWORK_READ"
    NETWORK_WRITE = "NETWORK_WRITE"
    BROWSER_READ = "BROWSER_READ"
    BROWSER_INTERACT = "BROWSER_INTERACT"
    SYSTEM_SETTINGS = "SYSTEM_SETTINGS"
    #: Observing the machine — battery, volume level, running windows. Reads
    #: nothing of the user's data and changes nothing.
    SYSTEM_READ = "SYSTEM_READ"
    #: Adjustments that are trivially observable and trivially reversible:
    #: volume, brightness. Granted without a prompt because the user asked for
    #: them, but still mutating, so untrusted content cannot reach them.
    SYSTEM_ADJUST = "SYSTEM_ADJUST"
    CREDENTIAL_ACCESS = "CREDENTIAL_ACCESS"
    USER_DATA = "USER_DATA"
    MEMORY_READ = "MEMORY_READ"
    MEMORY_WRITE = "MEMORY_WRITE"
    SCREEN_READ = "SCREEN_READ"
    AUDIO_READ = "AUDIO_READ"
    MIC_CONTROL = "MIC_CONTROL"
    CAMERA_ACCESS = "CAMERA_ACCESS"
    SELF_MODIFY = "SELF_MODIFY"


#: Capabilities that change the world rather than observe it. A request
#: influenced by untrusted content may never use these without a human
#: explicitly agreeing.
MUTATING = frozenset({
    Capability.FILE_WRITE, Capability.FILE_DELETE, Capability.CODE_EXECUTE,
    Capability.PROCESS_CONTROL, Capability.APP_LAUNCH,
    Capability.SYSTEM_ADJUST, Capability.NETWORK_WRITE,
    Capability.BROWSER_INTERACT, Capability.SYSTEM_SETTINGS,
    Capability.MEMORY_WRITE, Capability.SELF_MODIFY,
})

#: Capabilities no untrusted-influenced request may reach at all, with or
#: without confirmation. A human clicking "yes" on a prompt they did not write
#: is not meaningful consent.
NEVER_WHEN_UNTRUSTED = frozenset({
    Capability.CREDENTIAL_ACCESS, Capability.SELF_MODIFY,
    Capability.CAMERA_ACCESS,
})


# -- trust -------------------------------------------------------------------

class Trust(str, enum.Enum):
    """Where the instruction driving this request came from."""

    USER = "user"              # typed or spoken by the human
    SYSTEM = "system"          # NOVA's own scheduled or internal work
    UNTRUSTED = "untrusted"    # web page, document, email, tool output


# -- decisions ---------------------------------------------------------------

class Effect(str, enum.Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"        # permitted only if the human agrees now
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    effect: Effect
    capability: Capability | None
    principal: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.effect is Effect.CONFIRM

    def __bool__(self) -> bool:      # `if decision:` reads as "may proceed"
        return self.effect is Effect.ALLOW


# -- principals --------------------------------------------------------------

@dataclass
class Grant:
    """What one principal (an agent, or NOVA itself) may do."""

    principal: str
    capabilities: frozenset[Capability]
    #: Capabilities this principal holds but must always confirm with the
    #: human, even on a fully trusted request.
    confirm: frozenset[Capability] = field(default_factory=frozenset)
    description: str = ""

    def has(self, cap: Capability) -> bool:
        return cap in self.capabilities


def _caps(*names: Capability) -> frozenset[Capability]:
    return frozenset(names)


C = Capability

#: The default roster. Each agent gets what its job needs and nothing else --
#: the point of the exercise. RESEARCH can read the web but cannot click in
#: it; REVIEWER can read code but cannot write it, which is what makes its
#: verdict worth anything.
DEFAULT_GRANTS: dict[str, Grant] = {
    "nova": Grant(
        "nova",
        _caps(C.FILE_READ, C.FILE_WRITE, C.FILE_DELETE, C.CODE_EXECUTE,
              C.PROCESS_CONTROL, C.APP_LAUNCH, C.NETWORK_READ, C.NETWORK_WRITE,
              C.BROWSER_READ, C.BROWSER_INTERACT, C.SYSTEM_SETTINGS,
              C.SYSTEM_READ, C.SYSTEM_ADJUST,
              C.USER_DATA, C.MEMORY_READ, C.MEMORY_WRITE, C.SCREEN_READ,
              C.AUDIO_READ, C.MIC_CONTROL,
              # NOVA could already edit its own source behind a confirmation
              # prompt before this engine existed. Withholding the capability
              # would have removed a working feature, so it is granted here and
              # always confirmed -- and it stays in NEVER_WHEN_UNTRUSTED, so a
              # web page can never reach it however it phrases the request.
              C.SELF_MODIFY),
        confirm=_caps(C.FILE_DELETE, C.SYSTEM_SETTINGS, C.PROCESS_CONTROL,
                      C.NETWORK_WRITE, C.CODE_EXECUTE, C.BROWSER_INTERACT,
                      C.SELF_MODIFY),
        description="The orchestrator, acting directly for the user.",
    ),
    "CODE": Grant(
        "CODE",
        _caps(C.FILE_READ, C.FILE_WRITE, C.CODE_EXECUTE, C.MEMORY_READ),
        confirm=_caps(C.CODE_EXECUTE),
        description="Writes and runs code. No system settings, no network writes.",
    ),
    "REVIEWER": Grant(
        "REVIEWER",
        _caps(C.FILE_READ, C.MEMORY_READ),
        description="Reads and judges. Cannot write, so its verdict is independent.",
    ),
    "RESEARCH": Grant(
        "RESEARCH",
        _caps(C.NETWORK_READ, C.BROWSER_READ, C.FILE_READ, C.MEMORY_READ,
              C.MEMORY_WRITE, C.USER_DATA),
        description="Reads the web and the user's library. Cannot interact "
                    "with the web.",
    ),
    "BROWSER": Grant(
        "BROWSER",
        _caps(C.NETWORK_READ, C.BROWSER_READ, C.BROWSER_INTERACT,
              C.FILE_READ, C.FILE_WRITE),
        confirm=_caps(C.BROWSER_INTERACT, C.FILE_WRITE),
        description="Drives a browser. Every interaction is confirmable.",
    ),
    "VISUAL": Grant(
        "VISUAL",
        _caps(C.SCREEN_READ, C.FILE_READ, C.FILE_WRITE, C.MEMORY_READ),
        description="Looks at screens and images; writes only its own output.",
    ),
    "CONTROL": Grant(
        "CONTROL",
        _caps(C.FILE_READ, C.FILE_WRITE, C.FILE_DELETE, C.PROCESS_CONTROL,
              C.APP_LAUNCH, C.SYSTEM_SETTINGS, C.SYSTEM_READ, C.SYSTEM_ADJUST),
        confirm=_caps(C.FILE_DELETE, C.PROCESS_CONTROL, C.SYSTEM_SETTINGS),
        description="The privileged one. Everything dangerous is confirmable.",
    ),
    "CREATION": Grant(
        "CREATION",
        _caps(C.FILE_READ, C.FILE_WRITE, C.NETWORK_READ, C.MEMORY_READ),
        description="Produces documents and media.",
    ),
    "MEETING": Grant(
        "MEETING",
        _caps(C.AUDIO_READ, C.FILE_READ, C.FILE_WRITE, C.MEMORY_WRITE),
        confirm=_caps(C.AUDIO_READ),
        description="Listens to meetings only with explicit agreement.",
    ),
    "SECURITY": Grant(
        "SECURITY",
        _caps(C.FILE_READ, C.NETWORK_READ, C.MEMORY_READ, C.SCREEN_READ),
        description="Defensive and read-only by design; reports, never acts.",
    ),
    "MEMORY": Grant(
        "MEMORY",
        _caps(C.MEMORY_READ, C.MEMORY_WRITE, C.USER_DATA, C.FILE_READ),
        description="Maintains user memory and knowledge.",
    ),
    "SCHEDULER": Grant(
        "SCHEDULER",
        _caps(C.MEMORY_READ, C.MEMORY_WRITE),
        description="Reminders and recurring work. Deliberately tiny.",
    ),
}


# -- tool capability declarations --------------------------------------------

#: What each tool needs. Patterns are matched with fnmatch so a family of
#: tools can be declared once. A tool that matches nothing is denied, which
#: is what makes adding one without thinking about it fail loudly.
TOOL_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "vision": _caps(C.SCREEN_READ),
    "screen*": _caps(C.SCREEN_READ),
    "ocr": _caps(C.SCREEN_READ, C.FILE_READ),
    "web_search": _caps(C.NETWORK_READ),
    "fetch_url": _caps(C.NETWORK_READ),
    "browser_read": _caps(C.BROWSER_READ, C.NETWORK_READ),
    "browser_control": _caps(C.BROWSER_READ, C.NETWORK_READ),
    "browser_interact": _caps(C.BROWSER_INTERACT, C.NETWORK_READ),
    "open_app": _caps(C.APP_LAUNCH),
    "close_app": _caps(C.PROCESS_CONTROL),
    "file_controller": _caps(C.FILE_READ, C.FILE_WRITE),
    # Writes a new file where the user asked for one. Creating a document is
    # not a destructive act, so it does not stop to confirm; it still declares
    # FILE_WRITE, so untrusted content cannot make NOVA write files.
    "generate_document": _caps(C.FILE_WRITE),
    # Drives an application the user asked to be driven. Looking at a window
    # is a read; clicking and typing inside it is not, so it declares
    # PROCESS_CONTROL and the action-aware resolver lets inspection through
    # while interaction is still governed.
    "app_control": _caps(C.PROCESS_CONTROL),
    # Reads and parses a file the user pointed at; it does not write back.
    "file_processor": _caps(C.FILE_READ),
    # The desktop automation surface: launches things and changes settings.
    "computer_control": _caps(C.PROCESS_CONTROL, C.SYSTEM_SETTINGS),
    "remember_fact": _caps(C.MEMORY_WRITE),
    "nova_memory": _caps(C.MEMORY_READ, C.MEMORY_WRITE),
    "file_read": _caps(C.FILE_READ),
    "file_write": _caps(C.FILE_WRITE),
    "file_delete": _caps(C.FILE_DELETE),
    "computer_settings": _caps(C.SYSTEM_SETTINGS),
    "desktop_control": _caps(C.SYSTEM_SETTINGS, C.FILE_WRITE),
    "autostart": _caps(C.SYSTEM_SETTINGS),
    "run_command": _caps(C.CODE_EXECUTE),
    "run_python": _caps(C.CODE_EXECUTE),
    "self_editor": _caps(C.SELF_MODIFY, C.FILE_WRITE),
    # Reading a package the user pointed at. It never installs or runs
    # anything in NOVA's process -- the trial is a separate, scrubbed
    # subprocess -- but pointing NOVA at arbitrary code is a decision a
    # web page must not be able to make, so it is CONFIRM under taint.
    "learn_resource": _caps(C.FILE_READ, C.CODE_EXECUTE),
    "send_message": _caps(C.NETWORK_WRITE),
    "remember": _caps(C.MEMORY_WRITE),
    "recall": _caps(C.MEMORY_READ),
    "memory*": _caps(C.MEMORY_READ),
    # Searching the user's own documents reads their data; adding one writes
    # to the library.
    "rag_search": _caps(C.MEMORY_READ, C.USER_DATA),
    "rag_add": _caps(C.MEMORY_WRITE, C.USER_DATA, C.FILE_READ),
    "document*": _caps(C.MEMORY_READ, C.USER_DATA),
    "knowledge*": _caps(C.MEMORY_READ),
    "nova_task": _caps(C.MEMORY_READ, C.MEMORY_WRITE),
    # NOVA's learned skills. Running one can call an outside provider the
    # person connected, so it declares NETWORK_WRITE; each tool step inside
    # a skill is dispatched again and checked on its own.
    "nova_capability": _caps(C.MEMORY_READ, C.MEMORY_WRITE, C.NETWORK_READ, C.NETWORK_WRITE),
    # Learning reads the folder the person named and sends its content to the
    # model to study; knowledge is written to this account's store.
    "nova_learning": _caps(C.FILE_READ, C.MEMORY_READ, C.MEMORY_WRITE, C.NETWORK_WRITE),
    "planner": _caps(C.MEMORY_READ),
    "game_updater": _caps(C.NETWORK_READ, C.FILE_WRITE, C.PROCESS_CONTROL),
}

#: Tools that read nothing and change nothing -- pure computation. Declared
#: explicitly so "denied by default" does not make trivial helpers unusable.
HARMLESS_TOOLS: frozenset[str] = frozenset({
    "get_time", "get_date", "calculator", "noop", "echo",
})

#: Tools whose actions span a wide range of risk, and which are therefore
#: judged by the action rather than by the tool alone.
_ACTION_AWARE_TOOLS: frozenset[str] = frozenset({
    "computer_settings", "computer_control", "desktop_control",
    "file_controller", "nova_memory", "app_control",
})


#: Actions that only look. A tool is not one risk level — `computer_settings`
#: covers both "what is the battery at" and "restart the machine", and
#: charging the whole tool at the higher rate meant NOVA asked "should I
#: proceed?" before reading a battery percentage. In a voice assistant that is
#: not caution, it is an assistant that cannot be used: every observation
#: becomes a negotiation.
READ_ONLY_ACTIONS: frozenset[str] = frozenset({
    "battery", "get_volume", "get_brightness", "get_settings", "status",
    "info", "list", "read", "find", "search", "inspect", "get", "screenshot",
    "active_window", "list_windows", "system_info", "processes", "list_apps",
})

#: Adjustments the user is plainly authorising by asking for them. "Turn the
#: volume up" is not a request that benefits from "are you sure?" — it is
#: trivially observable and trivially reversible.
LOW_RISK_ACTIONS: frozenset[str] = frozenset({
    "volume_up", "volume_down", "volume_mute", "set_volume",
    "brightness_up", "brightness_down", "set_brightness",
    # Driving an application the user just asked to be driven. Clicking a
    # button inside Spotify is not a system-level act, and confirming each
    # one would make a multi-step task unusable by voice.
    "focus", "click", "type", "type_text", "press", "hotkey", "scroll", "open_settings",
})

#: ...and the ones that genuinely warrant stopping to ask, whatever the user
#: just said, mapped to the capability that actually describes them.
#:
#: Mapping matters rather than merely listing. Falling back to the tool's
#: declared set let `file_controller` with action="delete" through on
#: FILE_WRITE, which is not the permission being exercised — deleting a file
#: is FILE_DELETE, and that is the one the policy stops to confirm.
ALWAYS_CONFIRM_ACTIONS: dict[str, Capability] = {
    "shutdown": Capability.SYSTEM_SETTINGS,
    "restart": Capability.SYSTEM_SETTINGS,
    "sleep": Capability.SYSTEM_SETTINGS,
    "hibernate": Capability.SYSTEM_SETTINGS,
    "logoff": Capability.SYSTEM_SETTINGS,
    "lock": Capability.SYSTEM_SETTINGS,
    "registry": Capability.SYSTEM_SETTINGS,
    "delete": Capability.FILE_DELETE,
    "rmdir": Capability.FILE_DELETE,
    "format": Capability.FILE_DELETE,
    "kill": Capability.PROCESS_CONTROL,
    "terminate": Capability.PROCESS_CONTROL,
    "uninstall_app": Capability.SYSTEM_SETTINGS,
}


def _action_of(args: dict | None) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("action", "cmd", "command", "op"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


def capabilities_for_tool(tool: str,
                          args: dict | None = None) -> frozenset[Capability] | None:
    """What a tool needs, or None if it declares nothing (and is denied).

    `args` lets a tool be judged by what it is actually being asked to do.
    Without it every action of a multi-purpose tool is charged at the rate of
    its most dangerous one.
    """
    name = (tool or "").strip()
    if not name:
        return None
    if name in HARMLESS_TOOLS:
        return frozenset()

    action = _action_of(args)
    if action and name in _ACTION_AWARE_TOOLS:
        strict = ALWAYS_CONFIRM_ACTIONS.get(action)
        if strict is not None:
            return _caps(strict)
        if action in READ_ONLY_ACTIONS:
            # A real capability, not an empty set. "Needs no capabilities"
            # means allowed unconditionally — including for a request shaped
            # by a web page — and that is not what "read the battery level"
            # should mean.
            if action == "screenshot":
                return _caps(Capability.SCREEN_READ)
            if name == "file_controller":
                return _caps(Capability.FILE_READ)
            return _caps(Capability.SYSTEM_READ)
        if action in LOW_RISK_ACTIONS:
            return _caps(Capability.SYSTEM_ADJUST)

    exact = TOOL_CAPABILITIES.get(name)
    if exact is not None:
        return exact
    for pattern, caps in TOOL_CAPABILITIES.items():
        if "*" in pattern and fnmatch.fnmatch(name, pattern):
            return caps
    return None


# -- the engine --------------------------------------------------------------

@dataclass
class AuditEntry:
    ts: float
    principal: str
    tool: str
    capability: str | None
    trust: str
    effect: str
    reason: str


class PermissionEngine:
    """Decides, records, and never guesses.

    Deliberately synchronous and in-process: an authorisation check that can
    block on IO is an authorisation check people route around.
    """

    def __init__(self, grants: dict[str, Grant] | None = None,
                 audit_limit: int = 2000):
        self._grants = dict(grants if grants is not None else DEFAULT_GRANTS)
        self._lock = threading.RLock()
        self._audit: list[AuditEntry] = []
        self._audit_limit = audit_limit
        self._sink = None            # optional callable(AuditEntry)

    # -- grants ------------------------------------------------------------

    def grant_for(self, principal: str) -> Grant | None:
        with self._lock:
            return self._grants.get(principal)

    def set_grant(self, grant: Grant) -> None:
        with self._lock:
            self._grants[grant.principal] = grant

    def revoke(self, principal: str) -> None:
        with self._lock:
            self._grants.pop(principal, None)

    def principals(self) -> list[str]:
        with self._lock:
            return sorted(self._grants)

    def set_audit_sink(self, fn) -> None:
        """Route audit entries somewhere durable (the event bus, a log)."""
        self._sink = fn

    # -- the decision ------------------------------------------------------

    def check(self, principal: str, capability: Capability, *,
              trust: Trust = Trust.USER, tool: str = "") -> Decision:
        """May `principal` use `capability` on a request of this `trust`?"""
        grant = self.grant_for(principal)

        if grant is None:
            return self._record(Decision(
                Effect.DENY, capability, principal,
                f"no grant exists for principal {principal!r}"), tool, trust)

        if not grant.has(capability):
            return self._record(Decision(
                Effect.DENY, capability, principal,
                f"{principal} was not granted {capability.value}"), tool, trust)

        if trust is Trust.UNTRUSTED:
            if capability in NEVER_WHEN_UNTRUSTED:
                return self._record(Decision(
                    Effect.DENY, capability, principal,
                    f"{capability.value} is never reachable from untrusted "
                    "content"), tool, trust)
            if capability in MUTATING:
                return self._record(Decision(
                    Effect.CONFIRM, capability, principal,
                    "the request was influenced by untrusted content, so a "
                    "change to the system needs your agreement"), tool, trust)

        if capability in grant.confirm:
            return self._record(Decision(
                Effect.CONFIRM, capability, principal,
                f"{capability.value} always requires your agreement"),
                tool, trust)

        return self._record(Decision(
            Effect.ALLOW, capability, principal, "granted"), tool, trust)

    def check_tool(self, principal: str, tool: str, *,
                   trust: Trust = Trust.USER,
                   args: dict | None = None) -> Decision:
        """Check every capability a tool needs. The strictest answer wins."""
        needed = capabilities_for_tool(tool, args)
        if needed is None:
            return self._record(Decision(
                Effect.DENY, None, principal,
                f"tool {tool!r} declares no capabilities, so it cannot be "
                "authorised"), tool, trust)
        if not needed:
            return self._record(Decision(
                Effect.ALLOW, None, principal, "tool needs no capabilities"),
                tool, trust)

        worst = Decision(Effect.ALLOW, None, principal, "granted")
        for cap in sorted(needed, key=lambda c: c.value):
            d = self.check(principal, cap, trust=trust, tool=tool)
            if d.effect is Effect.DENY:
                return d
            if d.effect is Effect.CONFIRM and worst.effect is Effect.ALLOW:
                worst = d
        return worst

    # -- audit -------------------------------------------------------------

    def _record(self, decision: Decision, tool: str, trust: Trust) -> Decision:
        entry = AuditEntry(
            ts=time.time(), principal=decision.principal, tool=tool,
            capability=decision.capability.value if decision.capability else None,
            trust=trust.value, effect=decision.effect.value,
            reason=decision.reason,
        )
        with self._lock:
            self._audit.append(entry)
            if len(self._audit) > self._audit_limit:
                del self._audit[:len(self._audit) - self._audit_limit]
        if decision.effect is not Effect.ALLOW:
            log.info("[PERM] %s %s %s -> %s (%s)", decision.principal,
                     tool or "-", entry.capability or "-",
                     decision.effect.value, decision.reason)
        if self._sink is not None:
            try:
                self._sink(entry)
            except Exception:
                pass                 # auditing must never break the decision
        return decision

    def audit(self, limit: int = 100) -> list[AuditEntry]:
        with self._lock:
            return list(self._audit[-limit:])

    def denials(self, limit: int = 50) -> list[AuditEntry]:
        with self._lock:
            return [e for e in self._audit if e.effect != "allow"][-limit:]


_engine: PermissionEngine | None = None
_engine_lock = threading.Lock()


def engine() -> PermissionEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = PermissionEngine()
        return _engine


def reset_engine() -> None:
    """Test hook."""
    global _engine
    with _engine_lock:
        _engine = None


def check_tool(principal: str, tool: str, *, trust: Trust = Trust.USER,
               args: dict | None = None) -> Decision:
    return engine().check_tool(principal, tool, trust=trust, args=args)


__all__ = [
    "Capability", "Trust", "Effect", "Decision", "Grant", "PermissionEngine",
    "AuditEntry", "DEFAULT_GRANTS", "TOOL_CAPABILITIES", "HARMLESS_TOOLS",
    "MUTATING", "NEVER_WHEN_UNTRUSTED", "capabilities_for_tool",
    "engine", "reset_engine", "check_tool",
]
