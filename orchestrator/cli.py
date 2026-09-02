"""
Vertical-slice CLI — run a goal through the NOVA orchestrator end to end.

Example
-------
    python -m orchestrator.cli "where am i"
    python -m orchestrator.cli "open notepad" --yes
    python -m orchestrator.cli "where am i" --json --offline

This exercises the foundation (capability registry, trust engine,
confirmation gate, structured audit) through a complete, real path without
requiring the full Gemini Live voice runtime.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Optional

from capabilities.registry import build_core_registry
from core.connectivity import ConnectivityManager
from orchestrator.audit_logger import AuditLogger
from orchestrator.orchestrator import NOVAOrchestrator
from trust.confirmation import (
    ConfirmationGate,
    MockConfirmationRequester,
    TextConfirmationRequester,
)
from trust.trust_engine import TrustEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nova-orchestrator", description="NOVA orchestration CLI")
    parser.add_argument("goal", nargs="*", help="Goal to execute (e.g. 'where am i').")
    parser.add_argument("--platform", default="windows", help="Target platform (windows/linux/macos).")
    parser.add_argument("--yes", action="store_true", help="Auto-approve confirmations (tests/automation).")
    parser.add_argument("--offline", action="store_true", help="Simulate offline connectivity.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON result.")
    parser.add_argument("--audit", default=None, help="Audit log path (default: data/nova_audit.ndjson).")
    return parser


def _offline_connectivity(offline: bool) -> ConnectivityManager:
    if not offline:
        return ConnectivityManager()
    # Use only the public API: threshold 1 makes the second failure flip
    # ONLINE → DEGRADED → OFFLINE deterministically.
    conn = ConnectivityManager(offline_threshold=1)
    conn.record_failure()
    conn.record_failure()
    return conn


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    goal = " ".join(args.goal).strip()
    if not goal:
        print("error: a goal is required (e.g. python -m orchestrator.cli 'where am i')")
        return 2

    registry = build_core_registry(platform=args.platform)
    requester = MockConfirmationRequester("yes") if args.yes else TextConfirmationRequester()
    gate = ConfirmationGate(requester=requester, speak=lambda t: print(f"[NOVA] {t}"))
    connectivity = _offline_connectivity(args.offline)

    orchestrator = NOVAOrchestrator(
        registry=registry,
        trust=TrustEngine(),
        gate=gate,
        audit=AuditLogger(path=args.audit) if args.audit else AuditLogger(),
    )

    started = time.time()
    result = orchestrator.run(
        goal,
        platform=args.platform,
        connectivity=connectivity,
        speak=lambda t: print(f"[NOVA] {t}"),
    )
    elapsed = time.time() - started

    if args.json:
        payload = result.to_dict()
        payload["latency_s"] = round(elapsed, 2)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"\n[NOVA] {result.summary}")
        if result.status != "SUCCEEDED":
            for step in result.steps:
                if step.outcome.value in ("blocked", "failed", "cancelled"):
                    print(f"  x {step.tool}: {step.error or step.outcome.value}")
        print(f"[NOVA] status={result.status} steps={len(result.steps)} in {elapsed:.2f}s")
    return 0 if result.status == "SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())