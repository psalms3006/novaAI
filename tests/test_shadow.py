"""
Shadow-monitor tests: the legacy path is mirrored, never modified.

Rules under test:
- The real call's result/exception is preserved exactly.
- The capability is never executed twice (handler side-effect counter == 1).
- Verdicts: match / match-but-failed / mismatch-unsafe / no-metadata.
- Each comparison lands in the audit as a redacted ``shadow.compare`` row.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from capabilities.contracts import CapabilitySpec, ConfirmationPolicy, RiskLevel
from capabilities.registry import CapabilityRegistry
from orchestrator.audit_logger import AuditLogger
from orchestrator.shadow import ShadowMonitor
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


@pytest.fixture
def monitor(registry, tmp_path: Path) -> ShadowMonitor:
    return ShadowMonitor(
        registry=registry,
        trust=TrustEngine(preferences=PreferenceStore(path=tmp_path / "p.json")),
        audit=AuditLogger(path=str(tmp_path / "shadow.ndjson")),
    )


def last_compare(monitor: ShadowMonitor) -> dict:
    rows = monitor.audit.recent(limit=50, event="shadow.compare")
    assert rows, "expected at least one shadow.compare audit row"
    return rows[-1]


class TestShadowMonitor:
    def test_match_records_and_returns_output(self, monitor):
        out = monitor.watch("quote.echo", {"text": "hi"}, lambda: "quote: hi")
        assert out == "quote: hi"
        row = last_compare(monitor)
        assert row["verdict"] == "match"
        assert row["legacy_outcome"] == "success"
        assert row["shadow_decision"] == "execute"

    def test_never_executes_twice(self, monitor):
        calls = []

        def real():
            calls.append(1)
            return "quote: done"

        monitor.watch("quote.echo", {"text": "x"}, real)
        assert len(calls) == 1

    def test_failure_re_raised_and_verdict(self, monitor):
        def real():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            monitor.watch("quote.echo", {}, real)
        row = last_compare(monitor)
        assert row["verdict"] == "match-but-failed"
        assert row["legacy_outcome"] == "failure"
        assert "boom" in row["error"]

    def test_legacy_ran_what_shadow_would_confirm(self, monitor):
        out = monitor.watch("account.deactivate", {}, lambda: "account deactivated")
        assert out == "account deactivated"  # behavior unchanged
        row = last_compare(monitor)
        assert row["verdict"] == "mismatch-unsafe"
        assert row["shadow_decision"] == "confirm"
        assert row["warning"] is True

    def test_unknown_tool_is_no_metadata(self, monitor):
        monitor.watch("ghost.tool", {}, lambda: "ran")
        row = last_compare(monitor)
        assert row["verdict"] == "no-metadata"
        assert row["shadow_decision"] == "unknown"

    def test_report_aggregates(self, monitor):
        monitor.watch("quote.echo", {"text": "a"}, lambda: "quote: a")
        monitor.watch("account.deactivate", {}, lambda: "account deactivated")
        monitor.watch("ghost.tool", {}, lambda: "ran")
        report = monitor.report()
        assert report["match"] == 1
        assert report["mismatch-unsafe"] == 1
        assert report["no-metadata"] == 1

    def test_audit_redacts_params(self, monitor, tmp_path: Path):
        monitor.watch("quote.echo", {"text": "hello", "api_key": "sk-top-secret"}, lambda: "quote: hello")
        row = last_compare(monitor)
        assert "api_key" not in row
        raw = monitor.audit.path.read_text(encoding="utf-8")
        assert "sk-top-secret" not in raw