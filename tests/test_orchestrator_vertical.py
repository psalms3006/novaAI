"""
Vertical-slice tests: plan -> trust -> confirm -> capability -> verify -> audit.

Key rule under test: the orchestrator never fabricates execution; a
capability with no metadata, an unmet confirmation, an offline-required
capability offline, or a failed verification is BLOCKED/FAILED — never
"silently succeeded".
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from capabilities.contracts import CapabilitySpec, ConfirmationPolicy, RiskLevel
from capabilities.registry import CapabilityRegistry
from core.connectivity import ConnectivityManager
from core.event_bus import EventBus
from core.verification_engine import Verification
from orchestrator.audit_logger import AuditLogger
from orchestrator.orchestrator import NOVAOrchestrator, StepOutcome
from orchestrator.plan import GoalPlan, GraphStep
from orchestrator.planner_adapter import heuristic_plan, plan_to_graph
from trust.confirmation import ConfirmationGate, MockConfirmationRequester
from trust.trust_engine import PreferenceStore, TrustEngine

# ── fixtures ────────────────────────────────────────────────────────────────


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
            verification_hint="deactivated",
        )
    )
    reg.register(
        CapabilitySpec(
            name="news.fetch",
            description="Fetch today's news headlines from the web.",
            handler=lambda args, ctx: "headlines: market up, rain expected",
            risk_level=RiskLevel.LOW,
            confirmation_policy=ConfirmationPolicy.NEVER,
            requires_internet=True,
        )
    )
    return reg


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(path=str(tmp_path / "audit.ndjson"))


@pytest.fixture
def offline() -> ConnectivityManager:
    conn = ConnectivityManager(offline_threshold=1)
    conn.record_failure()
    conn.record_failure()  # ONLINE -> DEGRADED -> OFFLINE
    return conn


def make_orchestrator(
    registry: CapabilityRegistry,
    audit: AuditLogger,
    tmp_path: Path,
    requester=None,
    verifier=None,
    plan: GoalPlan = None,
):
    gate = ConfirmationGate(requester=requester or MockConfirmationRequester("no"))
    trust = TrustEngine(preferences=PreferenceStore(path=tmp_path / "prefs.json"))
    bus = EventBus()
    events: list = []
    bus.subscribe("*", events.append)
    orch = NOVAOrchestrator(
        registry=registry,
        trust=trust,
        gate=gate,
        audit=audit,
        event_bus=bus,
        verifier=verifier,
        planner=(None if plan is None else (lambda g: plan)),
    )
    return orch, events


def single_step(tool: str, goal: str = "demo", **params) -> GoalPlan:
    return GoalPlan(
        goal=goal,
        steps=[GraphStep(step_id="s1", tool=tool, description=goal, parameters=params)],
    )


def run(orch: NOVAOrchestrator, *args, **kwargs):
    kwargs.setdefault("connectivity", ConnectivityManager())
    return orch.run("demo", *args, **kwargs)


# ── audit logger ────────────────────────────────────────────────────────────


class TestAuditLogger:
    def test_writes_ndjson_and_filters(self, tmp_path: Path):
        path = tmp_path / "a.ndjson"
        log = AuditLogger(path=str(path))
        log.record("a", tool="x")
        log.record("b", tool="y")
        assert len(list(path.read_text(encoding="utf-8").splitlines())) == 2
        assert [r["event"] for r in log.recent(limit=10, event="a")] == ["a"]

    def test_redaction_drops_secret_keys(self, tmp_path: Path):
        log = AuditLogger(path=str(tmp_path / "r.ndjson"))
        entry = log.record("x", api_key="sk-live-123", token="abc", url="https://example.com", ok=True)
        assert "api_key" not in entry and "token" not in entry
        assert entry.get("url")
        assert entry.get("ok") is True

    def test_redacts_secret_values_and_private_content(self, tmp_path: Path):
        log = AuditLogger(path=str(tmp_path / "r2.ndjson"))
        entry = log.record("x", message_text="hello world", value_0="sk-zzz", note="a" * 500)
        assert entry["message_text"] == "<private:11 chars>"
        assert entry["value_0"] == "<redacted>"
        assert entry["note"].endswith("...")
        assert len(entry["note"]) <= 200 + 3

    def test_redacts_nested_payloads(self, tmp_path: Path):
        log = AuditLogger(path=str(tmp_path / "r3.ndjson"))
        entry = log.record("x", data={"items": ["sk-live-a", "safe"], "credential": "pw"})
        assert entry["data"]["items"] == ["<redacted>", "safe"]
        assert "credential" not in entry["data"]

    def test_trims_to_max_lines(self, tmp_path: Path):
        log = AuditLogger(path=str(tmp_path / "t.ndjson"), max_lines=5)
        for i in range(20):
            log.record("n", i=i)
        assert len(log.recent(limit=100)) == 5


# ── orchestrator decision paths ─────────────────────────────────────────────


class TestOrchestrator:
    def test_low_risk_executes(self, registry, audit, tmp_path):
        orch, events = make_orchestrator(registry, audit, tmp_path,
                                         plan=single_step("quote.echo", text="hi"))
        result = run(orch)
        assert result.status == "SUCCEEDED"
        assert "quote: hi" in result.summary
        assert "orchestrator.goal.start" in [e.type for e in events]
        assert "orchestrator.goal.finish" in [e.type for e in events]

    def test_high_risk_declined_blocks(self, registry, audit, tmp_path):
        orch, _ = make_orchestrator(registry, audit, tmp_path,
                                    requester=MockConfirmationRequester("no"),
                                    plan=single_step("account.deactivate"))
        result = run(orch)
        assert result.status == "BLOCKED"
        assert result.steps[0].outcome is StepOutcome.BLOCKED
        assert "declined" in result.steps[0].error.lower()

    def test_high_risk_approved_executes(self, registry, audit, tmp_path):
        orch, _ = make_orchestrator(registry, audit, tmp_path,
                                    requester=MockConfirmationRequester("yes"),
                                    plan=single_step("account.deactivate"))
        result = run(orch)
        assert result.status == "SUCCEEDED"
        assert result.steps[0].outcome is StepOutcome.SUCCEEDED

    def test_high_risk_no_requester_blocks(self, registry, audit, tmp_path):
        gate = ConfirmationGate()  # no requester / no UI attached
        trust = TrustEngine(preferences=PreferenceStore(path=tmp_path / "p.json"))
        orch = NOVAOrchestrator(registry=registry, trust=trust, gate=gate, audit=audit,
                                planner=lambda g: single_step("account.deactivate"))
        result = run(orch)
        assert result.steps[0].outcome is StepOutcome.BLOCKED
        assert result.steps[0].error and "interface" in result.steps[0].error.lower()

    def test_unknown_capability_blocks(self, registry, audit, tmp_path):
        orch, _ = make_orchestrator(registry, audit, tmp_path,
                                    plan=single_step("nope.missing"))
        result = run(orch)
        assert result.status == "BLOCKED"
        assert result.steps[0].decision == "unknown-capability"

    def test_offline_blocks_internet_capability(self, registry, audit, tmp_path, offline):
        orch, _ = make_orchestrator(registry, audit, tmp_path,
                                    plan=single_step("news.fetch"))
        result = orch.run("demo", connectivity=offline)
        assert result.status == "BLOCKED"
        assert "offline" in result.steps[0].error.lower()

    def test_online_allows_internet_capability(self, registry, audit, tmp_path):
        orch, _ = make_orchestrator(registry, audit, tmp_path,
                                    plan=single_step("news.fetch"))
        result = run(orch)
        assert result.status == "SUCCEEDED"

    def test_handler_exception_becomes_failed(self, registry, audit, tmp_path):
        def boom(args, ctx):
            raise RuntimeError("disk on fire")

        registry.register(
            CapabilitySpec(name="tool.boom", description="Explodes.", handler=boom,
                           risk_level=RiskLevel.LOW, confirmation_policy=ConfirmationPolicy.NEVER)
        )
        orch, _ = make_orchestrator(registry, audit, tmp_path,
                                    plan=single_step("tool.boom"))
        result = run(orch)
        assert result.status == "FAILED"
        assert "disk on fire" in result.steps[0].error

    def test_verification_failure_blocks(self, registry, audit, tmp_path):
        class FailVerifier:
            def verify(self, step, raw_output, capability):
                return Verification("FAILURE", "empty result", 0.0)

        orch, _ = make_orchestrator(registry, audit, tmp_path, verifier=FailVerifier(),
                                    plan=single_step("quote.echo", text="hi"))
        result = run(orch)
        assert result.steps[0].outcome is StepOutcome.BLOCKED
        assert "verification failed" in result.steps[0].error.lower()

    def test_critical_step_failure_aborts(self, registry, audit, tmp_path):
        def bad(args, ctx):
            raise RuntimeError("boom")

        registry.register(
            CapabilitySpec(name="tool.bad", description="Fails.", handler=bad,
                           risk_level=RiskLevel.LOW, confirmation_policy=ConfirmationPolicy.NEVER)
        )
        plan = GoalPlan(goal="two", steps=[
            GraphStep(step_id="s1", tool="quote.echo", description="safe", parameters={"text": "a"}),
            GraphStep(step_id="s2", tool="tool.bad", description="critical", critical=True),
            GraphStep(step_id="s3", tool="quote.echo", description="never reached", parameters={"text": "b"}),
        ])
        orch, _ = make_orchestrator(registry, audit, tmp_path, plan=plan)
        result = run(orch)
        assert result.status == "PARTIAL"
        assert len(result.steps) == 2  # s3 never ran

    def test_cancel_event_halts(self, registry, audit, tmp_path):
        orch, _ = make_orchestrator(registry, audit, tmp_path,
                                    plan=single_step("quote.echo", text="hi"))
        cancel = threading.Event()
        cancel.set()
        result = orch.run("demo", connectivity=ConnectivityManager(), cancel=cancel)
        assert result.status == "CANCELLED"
        assert result.steps[0].outcome is StepOutcome.CANCELLED

    def test_event_bus_routes_step_results(self, registry, audit, tmp_path):
        orch, events = make_orchestrator(registry, audit, tmp_path,
                                         plan=single_step("quote.echo", text="hi"))
        run(orch)
        types = [e.type for e in events]
        assert "orchestrator.step" in types
        assert "orchestrator.step.result" in types

    def test_forced_confirmation_confirm_and_execute(self, registry, audit, tmp_path):
        echo_plan = single_step("quote.echo", text="hi")
        orch, _ = make_orchestrator(registry, audit, tmp_path,
                                    requester=MockConfirmationRequester("yes"), plan=echo_plan)
        result = orch.run("demo", connectivity=ConnectivityManager(), force_confirm=True)
        assert result.status == "SUCCEEDED"


# ── planner wiring ─────────────────────────────────────────────────────────


class TestPlannerWiring:
    def test_plan_to_graph_validates_and_caps(self, registry):
        blob = {
            "goal": "demo",
            "steps": [{"tool": "quote.echo", "parameters": {"text": "a"}}] * 25,
        }
        plan = plan_to_graph(blob, registry)
        assert len(plan.steps) == 20
        assert plan.steps[0].tool == "quote.echo"

    def test_heuristic_finds_location_intent(self):
        reg = CapabilityRegistry(platform="windows")
        from capabilities.location import get_spec

        reg.register(get_spec())
        plan = heuristic_plan("where am i", reg)
        assert plan.steps[0].tool == "location.get"

    def test_heuristic_defaults_to_explicit_capability(self):
        # Unknown intent -> heuristic defaults to web_search (an explicit,
        # metadata-backed capability), never an invented free-form command.
        reg = CapabilityRegistry(platform="windows")
        plan = heuristic_plan("ggaarbb nonsense", reg)
        assert plan.steps[0].tool == "web_search"
        assert plan.steps[0].parameters.get("query") == "ggaarbb nonsense"

    def test_find_for_intent_stopword_no_tie_break(self):
        reg = CapabilityRegistry(platform="windows")
        reg.register(
            CapabilitySpec(name="mem.store", description="Store a fact about the user for this session.",
                           risk_level=RiskLevel.LOW)
        )
        reg.register(
            CapabilitySpec(name="files.delete", description="Manage files and folders: list, read, write, create, delete, copy, move.",
                           risk_level=RiskLevel.MEDIUM)
        )
        best = reg.find_for_intent("delete the todos file")
        assert best is not None and best.name == "files.delete"