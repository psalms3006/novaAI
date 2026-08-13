"""Integration tests for NOVA-Hermes bridge."""
from __future__ import annotations

import os
import sys
import time
import threading
from pathlib import Path

import pytest

# Ensure Hermes install path is importable even when Hermes itself
# doesn't add its own venv to sys.path during test execution.
_HERMES_DIR = Path(r"C:\Users\Lenovo\Downloads\hermes-agent")
if str(_HERMES_DIR) not in sys.path:
    sys.path.insert(0, str(_HERMES_DIR))

os.environ.setdefault("HERMES_ENABLED", "1")
os.environ.setdefault("HERMES_RUNTIME", "subprocess")
os.environ.setdefault("HERMES_TASK_TIMEOUT", "30")


def _make_event_bus():
    from core.event_bus import EventBus

    return EventBus()


def _make_bridge(event_bus=None):
    from integrations.hermes import HermesBridge

    return HermesBridge(event_bus=event_bus)


def _wait_for_terminal(task, deadline=15):
    end = time.time() + deadline
    while time.time() < end:
        status = task.status
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return status
        time.sleep(0.05)
    return task.status


class TestHermesLifecycle:
    def test_start_and_health(self):
        bus = _make_event_bus()
        bridge = _make_bridge(bus)
        start_result = bridge.start()
        assert start_result["status"] == "started"
        health = bridge.health()
        assert health["started"] is True
        stop_result = bridge.stop()
        assert stop_result["status"] == "stopped"
        health_after = bridge.health()
        assert health_after["started"] is False

    def test_double_start_is_idempotent(self):
        bus = _make_event_bus()
        bridge = _make_bridge(bus)
        assert bridge.start()["status"] == "started"
        assert bridge.start()["status"] == "already_started"
        bridge.stop()

    def test_events_are_published(self):
        bus = _make_event_bus()
        received: list = []

        def handler(event):
            received.append(event)

        bus.subscribe("hermes.*", handler)
        bridge = _make_bridge(bus)
        bridge.start()
        # small delay for threaded publish
        time.sleep(0.05)
        bridge.stop()
        types = [e.type for e in received]
        assert "hermes.started" in types
        assert "hermes.stopped" in types


class TestHermesTasks:
    def test_submit_and_status(self):
        bus = _make_event_bus()
        bridge = _make_bridge(bus)
        bridge.start()
        try:
            task = bridge.submit_task("Create a text file containing Hermes integration works.")
            assert task.task_id
            assert task.status in {"QUEUED", "RUNNING"}
            status = _wait_for_terminal(task, deadline=15)
            assert status in {"COMPLETED", "FAILED"}
            if status == "COMPLETED":
                assert task.result is not None
                assert task.artifacts, "expected at least one artifact"
                artifact_path = Path(task.artifacts[0]["path"])
                assert artifact_path.exists()
                content = artifact_path.read_text(encoding="utf-8")
                assert "Hermes integration works" in content
        finally:
            bridge.stop()

    def test_event_stream_contains_lifecycle(self):
        bus = _make_event_bus()
        bridge = _make_bridge(bus)
        bridge.start()
        try:
            task = bridge.submit_task("run diagnostics")
            events = bridge.stream_events(task.task_id, timeout=15)
            statuses = [e["status"] for e in events]
            assert "RUNNING" in statuses
            assert "COMPLETED" in statuses
        finally:
            bridge.stop()

    def test_cancel(self):
        bus = _make_event_bus()
        bridge = _make_bridge(bus)
        bridge.start()
        try:
            task = bridge.submit_task("background work")
            time.sleep(0.1)
            cancelled = bridge.cancel_task(task.task_id)
            assert cancelled is not None
            assert cancelled.status == "CANCELLED"
            assert cancelled.cancelled_at is not None
        finally:
            bridge.stop()

    def test_failure_does_not_break_bridge(self):
        bus = _make_event_bus()
        bridge = _make_bridge(bus)
        bridge.start()
        try:
            bad = bridge.submit_task("")
            status = _wait_for_terminal(bad, deadline=15)
            assert status in {"COMPLETED", "FAILED"}
            # Bridge should still accept another task.
            second = bridge.submit_task("post-failure probe")
            assert second.task_id
        finally:
            bridge.stop()


class TestHermesCapabilities:
    def test_research_delegation_creates_report(self):
        bus = _make_event_bus()
        bridge = _make_bridge(bus)
        bridge.start()
        try:
            task = bridge.submit_task("research python programming and save a report")
            status = _wait_for_terminal(task, deadline=20)
            assert status == "COMPLETED"
            paths = [Path(a["path"]) for a in task.artifacts if a.get("path")]
            assert any(p.exists() for p in paths), "expected report artifact"
        finally:
            bridge.stop()

    def test_browser_delegation_creates_report(self):
        bus = _make_event_bus()
        bridge = _make_bridge(bus)
        bridge.start()
        try:
            task = bridge.submit_task("browser https://example.com and save a report")
            status = _wait_for_terminal(task, deadline=20)
            assert status == "COMPLETED"
            paths = [Path(a["path"]) for a in task.artifacts if a.get("path")]
            assert any(p.exists() for p in paths), "expected browser report artifact"
        finally:
            bridge.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
