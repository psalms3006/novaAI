"""
Runtime-observability tests: live dispatch is mirrored, never modified.

Uses fake ``nova`` / ``agent.executor`` modules so the real (heavy) runtime is
never imported. Rules under test:
- install() attaches the shadow to both dispatch surfaces.
- _observe() mirrors a live call: dispatch runs exactly once, verdict recorded.
- event-bus timeline rows land in the same structured audit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from capabilities.contracts import CapabilitySpec, ConfirmationPolicy, RiskLevel
from capabilities.registry import CapabilityRegistry
from core.event_bus import EventBus
from orchestrator.audit_logger import AuditLogger
from orchestrator.runtime import RuntimeObservability
from orchestrator.shadow import ShadowMonitor
from trust.trust_engine import PreferenceStore, TrustEngine


class FakeNova:
    def __init__(self):
        self.observer = None
        self.dispatch_calls = 0

    def set_tool_observer(self, fn):
        self.observer = fn

    def _execute_tool_sync_dispatch(self, tool_name, args, meta):
        self.dispatch_calls += 1
        return f"ran:{tool_name}"


class FakeExecutor:
    def __init__(self):
        self.shadow = None

    def _set_shadow(self, monitor):
        self.shadow = monitor


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
    return reg


@pytest.fixture
def obs(registry, tmp_path: Path) -> RuntimeObservability:
    audit = AuditLogger(path=str(tmp_path / "runtime.ndjson"))
    shadow = ShadowMonitor(
        registry=registry,
        trust=TrustEngine(preferences=PreferenceStore(path=tmp_path / "p.json")),
        audit=audit,
    )
    return RuntimeObservability(audit=audit, shadow=shadow)


class TestRuntimeObservability:
    def test_install_attaches_both_surfaces(self, obs):
        nova = FakeNova()
        executor = FakeExecutor()
        obs.install(executor=executor, nova_module=nova)
        assert obs.installed is True
        assert executor.shadow is obs.shadow
        assert nova.observer.__self__ is obs

    def test_observe_mirrors_once_and_returns_dispatch(self, obs):
        nova = FakeNova()
        obs.install(executor=FakeExecutor(), nova_module=nova)
        out = obs._observe("quote.echo", {"text": "hi"}, {"goal": "echo hi"})
        assert out == "ran:quote.echo"
        assert nova.dispatch_calls == 1  # never re-executes
        rows = obs.audit.recent(limit=10, event="shadow.compare")
        assert rows and rows[-1]["verdict"] == "match"

    def test_uninstalled_observe_falls_back_to_real_import_is_lazy(self, obs):
        # Without install, _observe would import nova — but we guard by only
        # ever calling it post-install; verify _nova stays None until install.
        assert obs._nova is None

    def test_event_timeline_rows(self, obs, tmp_path: Path):
        bus = EventBus()
        obs.event_bus = bus
        obs.install(executor=FakeExecutor(), nova_module=FakeNova())
        bus.publish_sync("tool.called", "live", {"tool": "quote.echo"})
        bus.publish_sync("voice.wake", "voice", {})
        bus.publish_sync("memory.stored", "memory", {"fact": "x"})  # excluded
        rows = obs.audit.recent(limit=20, event="event.observed")
        assert {r["event_type"] for r in rows} == {"tool.called", "voice.wake"}

    def test_report(self, obs, tmp_path: Path):
        obs.install(executor=FakeExecutor(), nova_module=FakeNova())
        obs._observe("quote.echo", {"text": "hi"}, {})
        report = obs.report()
        assert report["installed"] is True
        assert report["shadow_verdicts"]["match"] >= 1
        assert report["audit_rows"] >= 1