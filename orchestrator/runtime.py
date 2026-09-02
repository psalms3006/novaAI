"""
Runtime observability — wires the structured audit + shadow monitor into the
live NOVA runtime.

The live loop (Gemini Live voice, phone/server, offline, MCP bridge) all route
tool calls through ``nova._execute_tool_sync``. This module:

- installs a ``ShadowMonitor`` on that dispatch AND on ``agent.executor``,
- mirrors every call through the trust/verification rules (decision-only,
  never re-executes, never changes behavior),
- records ``shadow.compare`` rows + event-bus timeline rows into one
  structured audit file (``data/nova_audit.ndjson``).

Enable it with ``NOVA_SHADOW=1`` (auto-installs on ``nova.py`` import) or call
``install_runtime_observability()`` explicitly. Inspect with
``python -m orchestrator.runtime --report``.
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from orchestrator.audit_logger import AuditLogger
from orchestrator.gate import GatedToolRunner
from orchestrator.shadow import ShadowMonitor
from trust.confirmation import ConfirmationGate, TextConfirmationRequester

log = logging.getLogger("nova.orchestrator.runtime")

_DEFAULT_AUDIT_PATH = os.environ.get("NOVA_AUDIT_PATH") or None
_EVENT_PREFIXES = ("tool.", "voice.", "connectivity.")


def _build_gate(obs: "RuntimeObservability", speak=None) -> GatedToolRunner:
    """A gate sharing the observability's registry/trust/audit/verifier."""
    return GatedToolRunner(
        registry=obs.shadow.registry,
        trust=obs.shadow.trust,
        gate=ConfirmationGate(requester=TextConfirmationRequester(), speak=speak),
        audit=obs.audit,
        verifier=obs.shadow.verifier,
    )


@dataclass
class RuntimeObservability:
    audit: AuditLogger
    shadow: ShadowMonitor
    event_bus: Any = None
    installed: bool = False
    gate: Any = None
    _nova: Any = field(default=None, init=False, repr=False)

    def install(
        self,
        *,
        executor=None,
        nova_module=None,
        gate: Optional[GatedToolRunner] = None,
    ) -> "RuntimeObservability":
        """Attach observability (and optionally enforcement) to the runtime.

        ``executor`` / ``nova_module`` are injectable for tests; defaults to
        the real modules when omitted. With ``gate`` set, live+executor calls
        run through the orchestrator rules instead of unconditionally.
        """
        if executor is None:
            from agent import executor as executor
        if nova_module is None:
            import nova as nova_module

        self._nova = nova_module
        self.gate = gate
        if gate is not None:
            executor._set_gate(gate)
        else:
            executor._set_shadow(self.shadow)
        nova_module.set_tool_observer(self._observe)
        if self.event_bus is not None:
            self.event_bus.subscribe("*", self._on_event)

        self.installed = True
        mode = "gated" if gate is not None else "shadow"
        self.audit.record("observability.install", mode=mode, target="executor+live-dispatch")
        log.info("Runtime observability installed (%s).", mode)
        return self

    def _observe(self, tool_name: str, args: dict, meta: dict) -> str:
        """Observer closure routed into ``nova._execute_tool_sync``."""
        n = self._nova
        goal = str(meta.get("goal") or meta.get("user_input") or "")
        dispatch = lambda: n._execute_tool_sync_dispatch(tool_name, args, meta)
        if self.gate is not None:
            return self.gate.run(tool_name, args, dispatch, goal=goal)
        return self.shadow.watch(tool_name, args, dispatch, goal=goal)

    def _on_event(self, event: Any) -> None:
        etype = getattr(event, "type", "") or ""
        if not etype.startswith(_EVENT_PREFIXES):
            return
        self.audit.record(
            "event.observed",
            event_type=etype,
            source=getattr(event, "source", ""),
            data=getattr(event, "data", {}) or {},
        )

    def report(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "audit_path": str(self.audit.path),
            "audit_rows": self.audit.count(),
            "shadow_verdicts": self.shadow.report(),
        }


def build_runtime_observability(
    event_bus: Any = None,
    audit_path: Optional[str] = None,
    shadow_confidence: float = 0.7,
) -> RuntimeObservability:
    audit = AuditLogger(path=audit_path)
    return RuntimeObservability(
        audit=audit,
        shadow=ShadowMonitor(audit=audit, confidence=shadow_confidence),
        event_bus=event_bus,
    )


def install_runtime_observability(
    event_bus: Any = None,
    audit_path: Optional[str] = None,
    gated: bool = False,
    **kw: Any,
) -> RuntimeObservability:
    obs = build_runtime_observability(event_bus=event_bus, audit_path=audit_path)
    gate = _build_gate(obs) if gated else None
    obs.install(gate=gate, **kw)
    _ACTIVE.update(obs)
    return obs


_ACTIVE: Dict[str, RuntimeObservability] = {}


def get_observability() -> RuntimeObservability:
    """Return the installed observability (or a passive, uninstalled one)."""
    for obs in _ACTIVE.values():
        return obs
    obs = build_runtime_observability()
    _ACTIVE["passive"] = obs
    return obs


# ── CLI ──────────────────────────────────────────────────────────────────────


def _load_audit(path: Optional[str]) -> AuditLogger:
    if path:
        return AuditLogger(path=path)
    for obs in _ACTIVE.values():
        if obs.audit.path.exists():
            return obs.audit
    return AuditLogger()


def run_report(args: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="nova-runtime-observability")
    parser.add_argument("--report", "--status", action="store_true",
                        help="Observability status + shadow verdict summary.")
    parser.add_argument("--audit", metavar="EVENT", nargs="?", const="", default=None,
                        help="Dump recent audit rows (optionally filtered by event).")
    parser.add_argument("--audit-path", default=None, help="Override audit file path.")
    opts = parser.parse_args(args)

    audit = _load_audit(opts.audit_path)

    if opts.audit is not None:
        rows = audit.recent(limit=30, event=opts.audit or None)
        if not rows:
            print(f"no audit rows for event={opts.audit or '(any)'}")
            return 0
        for r in rows:
            print(r)
        return 0

    obs = get_observability()
    print("observability status:")
    for key, value in obs.report().items():
        print(f"  {key}: {value}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return run_report(argv if argv is not None else [])


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))