"""
GatedToolRunner tests — opt-in switchover enforcement on single tool calls.

Rules under test:
- No metadata -> blocked (never fabricated).
- HIGH/consequential tool without approval -> blocked, dispatch never runs.
- Approving requester -> dispatch runs exactly once, result returned.
- Verification FAILURE after dispatch -> blocked.
- Dispatch exceptions propagate unchanged (legacy error recovery intact).
- Every outcome is audited as gate.blocked / gate.executed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from capabilities.contracts import CapabilitySpec, ConfirmationPolicy, RiskLevel
from capabilities.registry import CapabilityRegistry
from core.verification_engine import Verification
from orchestrator.audit_logger import AuditLogger
from orchestrator.gate import GatedToolRunner
from trust.confirmation import ConfirmationGate, MockConfirmationRequester
from trust.trust_engine import PreferenceStore, TrustEngine


@pytest.fixture
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry(platform="windows")
    reg.register(
        CapabilitySpec(
            name="quote.echo",
            description="Echo a short quote back to the user.",
            handler=lambda args, ctx: f"quote: {args.get('text', '')}",
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.NEVER,
            reversible=True,
            verification_hint="quote",
        )
    )
    reg.register(
        CapabilitySpec(
            name="account.deactivate",
            description="Deactivate the user account forever. irreversible.",
            handler=lambda args, ctx: "account deactivated",
            risk_level=RiskLevel.HIGH,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            reversible=False,
        )
    )
    return reg


def make_runner(registry, tmp_path: Path, requester=None):
    gate = ConfirmationGate(requester=requester or MockConfirmationRequester("no"))
    return GatedToolRunner(
        registry=registry,
        trust=TrustEngine(preferences=PreferenceStore(path=tmp_path / "p.json")),
        gate=gate,
        audit=AuditLogger(path=str(tmp_path / "gate.ndjson")),
    )


def last_gate_row(runner: GatedToolRunner, event: str) -> dict:
    rows = runner.audit.recent(limit=20, event=event)
    assert rows, f"expected a '{event}' audit row"
    return rows[-1]


class TestGatedToolRunner:
    def test_low_risk_passes_through(self, registry, tmp_path):
        runner = make_runner(registry, tmp_path)
        calls = []
        out = runner.run("quote.echo", {"text": "hi"}, lambda: (calls.append(1), "quote: hi")[1])
        assert out == "quote: hi"
        assert len(calls) == 1
        row = last_gate_row(runner, "gate.executed")
        assert row["tool"] == "quote.echo"
        assert row["outcome"] == "success"  # UNKNOWN verification still passes; only FAILURE blocks

    def test_no_metadata_blocks_without_dispatch(self, registry, tmp_path):
        runner = make_runner(registry, tmp_path)
        calls = []
        out = runner.run("ghost.tool", {}, lambda: (calls.append(1), "ran")[1])
        assert "no risk metadata" in out
        assert calls == []
        assert last_gate_row(runner, "gate.blocked")["reason"] == "no-metadata"

    def test_high_risk_declined_never_dispatches(self, registry, tmp_path):
        runner = make_runner(registry, tmp_path, requester=MockConfirmationRequester("no"))
        calls = []
        out = runner.run("account.deactivate", {}, lambda: (calls.append(1), "deactivated")[1])
        assert "declined" in out
        assert calls == []
        assert last_gate_row(runner, "gate.blocked")["decision"] == "confirm"

    def test_high_risk_approved_dispatches_once(self, registry, tmp_path):
        runner = make_runner(registry, tmp_path, requester=MockConfirmationRequester("yes"))
        calls = []
        out = runner.run("account.deactivate", {}, lambda: (calls.append(1), "account deactivated")[1])
        assert out == "account deactivated"
        assert len(calls) == 1
        assert last_gate_row(runner, "gate.executed")["tool"] == "account.deactivate"

    def test_no_requester_blocks_safely(self, registry, tmp_path):
        runner = GatedToolRunner(
            registry=registry,
            trust=TrustEngine(preferences=PreferenceStore(path=tmp_path / "p.json")),
            gate=ConfirmationGate(),  # no requester -> human can never answer
            audit=AuditLogger(path=str(tmp_path / "gate2.ndjson")),
        )
        calls = []
        out = runner.run("account.deactivate", {}, lambda: (calls.append(1), "deactivated")[1])
        assert "confirmation channel" in out
        assert calls == []

    def test_verification_failure_blocks_after_dispatch(self, registry, tmp_path):
        class FailVerifier:
            def verify(self, step, raw_output, capability):
                return Verification("FAILURE", "empty result", 0.0)

        runner = make_runner(registry, tmp_path)
        runner.verifier = FailVerifier()
        calls = []
        out = runner.run("quote.echo", {"text": "hi"}, lambda: (calls.append(1), "quote: hi")[1])
        assert "failed verification" in out
        assert len(calls) == 1  # dispatched, then blocked on the result
        assert last_gate_row(runner, "gate.executed")["outcome"] == "blocked"

    def test_dispatch_exception_propagates(self, registry, tmp_path):
        runner = make_runner(registry, tmp_path)

        def boom():
            raise RuntimeError("tool exploded")

        with pytest.raises(RuntimeError, match="tool exploded"):
            runner.run("quote.echo", {"text": "hi"}, boom)