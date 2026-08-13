"""NOVA <-> Hermes agentic bridge.

This integration lets NOVA delegate real agentic tasks to the installed Hermes
runtime while keeping NOVA as the user-facing assistant. Design goals:

- No file-by-file Hermes duplication.
- Prefer subprocess isolation by default for reliability and clean teardown.
- Optional embedded support when Hermes is already importable.
- Surface Hermes capabilities through NOVA's event bus with task lifecycle events.
- Keep a small, clean public API for NOVA to call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.event_bus import Event, EventBus

log = logging.getLogger("nova.integrations.hermes")


@dataclass
class HermesTask:
    """Represents one delegated Hermes task."""

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_request: str = ""
    status: str = "CREATED"
    execution_mode: str = "agent"
    priority: int = 5
    progress: float = 0.0
    current_action: str = ""
    result: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    completed_at: Optional[str] = None
    cancelled_at: Optional[str] = None


class HermesBridgeError(Exception):
    """Raised when the Hermes bridge itself cannot operate."""


class HermesTaskError(Exception):
    """Raised for task-level failures returned as structured errors."""


class HermesBridge:
    """Minimal bridge between NOVA and the Hermes runtime."""

    def __init__(self, event_bus: Optional[EventBus] = None) -> None:
        self._event_bus = event_bus
        self._tasks: Dict[str, HermesTask] = {}
        self._lock = threading.RLock()
        self._started = False
        self._start_time: Optional[float] = None
        self._impl = _HermesSubprocessBridge(self)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._started:
                return {"status": "already_started"}
            self._start_time = time.time()
            self._started = True
        try:
            result = self._impl.start()
        except Exception as exc:
            with self._lock:
                self._started = False
            self._publish("hermes.startup_failed", {"error": str(exc)})
            raise HermesBridgeError(str(exc)) from exc
        self._publish("hermes.started", {"elapsed_ms": int(self.elapsed_ms)})
        return {"status": "started", "impl": result.get("impl", "unknown")}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if not self._started:
                return {"status": "already_stopped"}
        try:
            result = self._impl.stop()
        except Exception as exc:
            self._publish("hermes.shutdown_failed", {"error": str(exc)})
            raise HermesBridgeError(str(exc)) from exc
        with self._lock:
            self._started = False
        self._publish("hermes.stopped", {})
        return {"status": "stopped", **result}

    def health(self) -> Dict[str, Any]:
        try:
            impl_health = self._impl.health()
        except Exception as exc:
            return {"status": "unhealthy", "error": str(exc)}
        with self._lock:
            started = self._started
        return {"status": "healthy" if started else "stopped", "started": started, "impl": impl_health}

    # ── task execution ─────────────────────────────────────────────────────

    def submit_task(self, user_request: str, **kwargs: Any) -> HermesTask:
        task = HermesTask(user_request=user_request, **kwargs)
        self._tasks[task.task_id] = task
        self._publish_task(task, "task.created")
        task.status = "QUEUED"
        self._publish_task(task, "task.queued")
        runner = threading.Thread(target=self._run_task, args=(task,), daemon=True)
        runner.start()
        return task

    def get_task_status(self, task_id: str) -> Optional[HermesTask]:
        return self._tasks.get(task_id)

    def cancel_task(self, task_id: str) -> Optional[HermesTask]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        with self._lock:
            if task.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return task
            task.status = "CANCELLED"
            task.cancelled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            task.updated_at = task.cancelled_at
            task.current_action = "cancelled"
        self._publish_task(task, "task.cancelled")
        try:
            self._impl.cancel(task_id, task)
        except Exception as exc:
            log.debug("cancel impl error: %s", exc)
        return task

    def stream_events(self, task_id: str, timeout: float = 5.0) -> List[Dict[str, Any]]:
        return self._impl.stream_events(task_id, timeout=timeout)

    # ── internals ──────────────────────────────────────────────────────────

    def _run_task(self, task: HermesTask) -> None:
        try:
            with self._lock:
                task.status = "RUNNING"
                task.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._publish_task(task, "task.started")
            result = self._impl.run_task(task)
            with self._lock:
                task.status = "COMPLETED"
                task.result = result
                task.progress = 1.0
                task.current_action = "completed"
                task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                task.updated_at = task.completed_at
            self._publish_task(task, "task.completed", result=result)
        except HermesTaskError as exc:
            with self._lock:
                task.status = "FAILED"
                task.errors.append(str(exc))
                task.current_action = "failed"
                task.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._publish_task(task, "task.failed", error=str(exc))
        except Exception as exc:
            with self._lock:
                task.status = "FAILED"
                task.errors.append(str(exc))
                task.current_action = "failed"
                task.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._publish_task(task, "task.failed", error=str(exc))

    def _publish(self, event_type: str, data: Dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(Event(type=event_type, source="hermes", data=data))
        except Exception as exc:
            log.debug("event bus publish failed for %s: %s", event_type, exc)

    def _publish_task(self, task: HermesTask, event_type: str, **extra: Any) -> None:
        payload = {
            "task_id": task.task_id,
            "status": task.status,
            "progress": task.progress,
            "current_action": task.current_action,
            "updated_at": task.updated_at,
        }
        payload.update(extra)
        self._publish(event_type, payload)

    @property
    def elapsed_ms(self) -> int:
        if self._start_time is None:
            return 0
        return int((time.time() - self._start_time) * 1000)


class _HermesSubprocessBridge:
    """Isolated Hermes bridge via subprocess."""

    def __init__(self, parent: HermesBridge) -> None:
        self._parent = parent
        self._processes: Dict[str, subprocess.Popen] = {}

    def _hermes_python(self) -> str:
        return sys.executable or str(Path(r"C:\Users\Lenovo\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"))

    def _hermes_entry(self) -> str:
        candidates = [
            Path(r"C:\Users\Lenovo\Downloads\hermes-agent\hermes"),
            Path(r"C:\Users\Lenovo\Downloads\hermes-agent\cli.py"),
            Path(r"C:\Users\Lenovo\Downloads\hermes-agent\run_agent.py"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return str(candidates[0])

    def start(self) -> Dict[str, Any]:
        return {"impl": "subprocess", "entry": self._hermes_entry()}

    def stop(self) -> Dict[str, Any]:
        for proc in list(self._processes.values()):
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
        self._processes.clear()
        return {"impl": "subprocess"}

    def health(self) -> Dict[str, Any]:
        return {
            "impl": "subprocess",
            "python": self._hermes_python(),
            "entry": self._hermes_entry(),
            "alive": len(self._processes) > 0,
        }

    def run_task(self, task: HermesTask) -> str:
        request = (task.user_request or "").strip() or "Report current system status briefly."
        out_dir = Path(tempfile.gettempdir())
        out_file = out_dir / f"hermes_task_{task.task_id}.txt"
        payload_path = out_dir / f"hermes_payload_{task.task_id}.json"
        payload_path.write_text(
            json.dumps({"task_id": task.task_id, "request": request}, ensure_ascii=False),
            encoding="utf-8",
        )
        mode = HermesConfig.runtime()
        if mode == "embedded":
            return self._run_embedded(task, request, out_file)

        probe = self._try_hermes_subprocess(task, request, out_file)
        if probe is not None:
            return probe
        fallback = self._run_nova_side(task, request, out_file)
        return fallback

    def _try_hermes_subprocess(self, task, request, out_file):
        hermes_dir = str(_HERMES_DIR)
        script = textwrap.dedent(
            f"""
            import json, sys
            from pathlib import Path
            sys.path.insert(0, {hermes_dir!r})
            request = {request!r}
            out_file_path = {str(out_file)!r}
            result = {{'ok': False, 'error': 'no handler'}}
            try:
                tools_mod = __import__('tools.registry', fromlist=['discover_builtin_tools', 'registry'])
                registry = getattr(tools_mod, 'registry', None)
                if registry is None:
                    raise ImportError('tools.registry.registry is unavailable')
                if hasattr(registry, 'discover_builtin_tools'):
                    registry.discover_builtin_tools()
                elif hasattr(tools_mod, 'discover_builtin_tools'):
                    tools_mod.discover_builtin_tools()
                out = {{'request': request}}
                get_tool = getattr(registry, 'get_entry', None) or getattr(registry, 'get', None)
                if callable(get_tool):
                    tool = get_tool('web_search')
                    if tool is not None:
                        handler = getattr(tool, 'handler', None)
                        if callable(handler):
                            out['search'] = handler({{'query': request}}, None)
                        else:
                            out['search'] = 'web_search tool has no executable handler'
                else:
                    out['search'] = 'registry entry lookup unavailable'
                Path(out_file_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
                result = {{'ok': True, 'out': out_file_path}}
            except Exception as exc:
                result = {{'ok': False, 'error': str(exc)}}
            print(json.dumps(result, ensure_ascii=False))
            """
        ).lstrip()
        runner = out_file.parent / f"hermes_run_{task.task_id}.py"
        runner.write_text(script, encoding="utf-8")
        proc = subprocess.Popen(
            [self._hermes_python(), str(runner)],
            cwd=hermes_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._processes[task.task_id] = proc
        try:
            stdout, stderr = proc.communicate(timeout=HermesConfig.task_timeout())
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            self._processes.pop(task.task_id, None)
            raise HermesTaskError("Hermes task timed out.")
        self._processes.pop(task.task_id, None)
        try:
            payload = json.loads(stdout)
        except Exception as exc:
            payload = {"ok": False, "error": f"invalid output: {exc}"}
        if not payload.get("ok"):
            log.debug("hermes subprocess failed: %s", payload)
            return None
        if out_file.exists():
            task.artifacts.append({"path": str(out_file), "type": "document", "created": True})
        return json.dumps(payload, ensure_ascii=False)

    def _run_embedded(self, task, request, out_file):
        try:
            from run_agent import AIAgent

            agent = AIAgent(max_iterations=1)
            result = agent.run_conversation(request)
            text = str(result.get("final_response", result))
        except Exception as exc:
            raise HermesTaskError(f"embedded Hermes agent failed: {exc}") from exc
        out_file.write_text(text, encoding="utf-8")
        task.artifacts.append({"path": str(out_file), "type": "document", "created": True})
        return text

    def _run_nova_side(self, task, request, out_file):
        try:
            from actions.web_search import web_search as _web_search

            search_result = _web_search({"query": request})
        except Exception as exc:
            search_result = f"Web search unavailable: {exc}"
        try:
            from actions.browser_control import browser_control as _browser_control

            browser_text = _browser_control({"action": "go_to", "url": "https://example.com"})
        except Exception as exc:
            browser_text = f"Browser unavailable: {exc}"
        report = {
            "request": request,
            "search": search_result,
            "browser": browser_text,
            "mode": "nova_side",
        }
        out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        task.artifacts.append({"path": str(out_file), "type": "document", "created": True})
        return json.dumps(report, ensure_ascii=False)

    def stream_events(self, task_id: str, timeout: float = 5.0) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        task = self._parent.get_task_status(task_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self._parent.get_task_status(task_id)
            if task:
                events.append({
                    "task_id": task.task_id,
                    "status": task.status,
                    "progress": task.progress,
                    "current_action": task.current_action,
                    "updated_at": task.updated_at,
                })
            if task and task.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.05)
        return events

    def cancel(self, task_id: str, task: HermesTask) -> None:
        proc = self._processes.get(task_id)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


class HermesConfig:
    HERMES_ENABLED_ENV = "HERMES_ENABLED"
    HERMES_RUNTIME_ENV = "HERMES_RUNTIME"
    HERMES_TASK_TIMEOUT_ENV = "HERMES_TASK_TIMEOUT"

    @classmethod
    def enabled(cls) -> bool:
        val = os.getenv(cls.HERMES_ENABLED_ENV, "1").strip().lower()
        return val not in {"0", "false", "no", "off"}

    @classmethod
    def runtime(cls) -> str:
        return os.getenv(cls.HERMES_RUNTIME_ENV, "subprocess").strip().lower()

    @classmethod
    def task_timeout(cls) -> int:
        try:
            return max(1, int(os.getenv(cls.HERMES_TASK_TIMEOUT_ENV, "120")))
        except Exception:
            return 120


_HERMES_DIR = Path(r"C:\Users\Lenovo\Downloads\hermes-agent")

__all__ = [
    "HermesBridge",
    "HermesBridgeError",
    "HermesTask",
    "HermesTaskError",
    "HermesConfig",
]
