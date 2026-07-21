"""
NOVA Boot Manager — Ordered, dependency-aware service initialization.

Inspired by VYREN's boot/manager.py pattern.
Provides phased initialization with auto-restart, critical/fatal distinction,
and reverse-order shutdown.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("nova.boot")


class Phase(enum.IntEnum):
    """Boot phases in initialization order."""
    CONFIG = 1
    LOGGING = 2
    EVENT_BUS = 3
    MEMORY = 4
    KNOWLEDGE_GRAPH = 5
    RELIABILITY = 6
    CONNECTIVITY = 7
    TOOLS = 8
    AGENTS = 9
    PLANNER = 10
    HEARTBEAT = 11
    VOICE = 12
    SERVER = 13
    UI = 14
    SERVICE = 15


@dataclass
class ServiceDescriptor:
    """Describes a service to be initialized during boot."""
    name: str
    phase: Phase
    init_fn: Callable[[Dict[str, Any]], Any]
    shutdown_fn: Optional[Callable[[Any], None]] = None
    critical: bool = False
    dependencies: List[str] = field(default_factory=list)
    restart_on_failure: bool = False
    max_restarts: int = 2

    # Internal state
    state: str = "stopped"
    instance: Any = None
    restart_count: int = 0
    error: Optional[str] = None


class BootManager:
    """
    Manages ordered, dependency-aware initialization of NOVA subsystems.

    Usage:
        boot = BootManager()
        boot.register(ServiceDescriptor(name="event_bus", phase=Phase.EVENT_BUS, init_fn=init_events))
        ctx = boot.boot()
        # ctx["event_bus"] -> the initialized event bus instance
    """

    def __init__(self) -> None:
        self._services: Dict[str, ServiceDescriptor] = {}
        self._phase_order = sorted(Phase, key=lambda p: p.value)
        self._lock = threading.Lock()

    def register(self, svc: ServiceDescriptor) -> None:
        """Register a service for boot."""
        with self._lock:
            if svc.name in self._services:
                log.warning(f"Service '{svc.name}' re-registered (overwriting).")
            self._services[svc.name] = svc

    def unregister(self, name: str) -> None:
        """Remove a registered service."""
        with self._lock:
            self._services.pop(name, None)

    def _validate_dependencies(self) -> List[str]:
        """Check all dependency references exist. Returns list of errors."""
        errors = []
        for name, svc in self._services.items():
            for dep in svc.dependencies:
                if dep not in self._services:
                    errors.append(f"Service '{name}' depends on '{dep}' which is not registered.")
        return errors

    def boot(self) -> Dict[str, Any]:
        """
        Execute the full boot sequence. Returns shared context dict.
        Raises RuntimeError if a critical service fails.
        """
        ctx: Dict[str, Any] = {}

        # Validate dependencies first
        dep_errors = self._validate_dependencies()
        if dep_errors:
            raise RuntimeError("Dependency validation failed:\n" + "\n".join(dep_errors))

        for phase in self._phase_order:
            phase_services = [
                svc for svc in self._services.values()
                if svc.phase == phase
            ]
            # Sort by dependency order within phase
            phase_services = self._sort_by_deps(phase_services)
            for svc in phase_services:
                self._init_service(svc, ctx)

        log.info(f"Boot complete — {len(ctx)} subsystems initialized.")
        return ctx

    def _sort_by_deps(self, services: List[ServiceDescriptor]) -> List[ServiceDescriptor]:
        """Topological sort of services within a phase by dependencies."""
        name_to_svc = {s.name: s for s in services}
        sorted_list: List[ServiceDescriptor] = []
        visited = set()

        def visit(name: str) -> None:
            if name in visited or name not in name_to_svc:
                return
            visited.add(name)
            for dep in name_to_svc[name].dependencies:
                if dep in name_to_svc:
                    visit(dep)
            sorted_list.append(name_to_svc[name])

        for svc in services:
            visit(svc.name)
        return sorted_list

    def _init_service(self, svc: ServiceDescriptor, ctx: Dict[str, Any]) -> None:
        """Initialize a single service with retry support."""
        svc.state = "starting"
        log.info(f"[Phase {svc.phase.value:02d}] Initializing: {svc.name}")

        try:
            instance = svc.init_fn(ctx)
            svc.instance = instance
            svc.state = "running"
            svc.error = None
            ctx[svc.name] = instance
            log.info(f"  ✓ {svc.name} ready.")
        except Exception as e:
            svc.state = "failed"
            svc.error = str(e)
            log.error(f"  ✗ {svc.name} failed: {e}")

            if svc.critical:
                raise RuntimeError(f"Critical service '{svc.name}' failed: {e}")
            elif svc.restart_on_failure and svc.restart_count < svc.max_restarts:
                svc.restart_count += 1
                delay = min(2 ** svc.restart_count, 16)
                log.info(f"  ↻ Retrying {svc.name} in {delay}s (attempt {svc.restart_count}/{svc.max_restarts})")
                time.sleep(delay)
                self._init_service(svc, ctx)
            else:
                log.warning(f"  ! {svc.name} skipped (non-critical).")

    def shutdown(self, ctx: Dict[str, Any]) -> None:
        """Shutdown all services in reverse phase order."""
        for phase in reversed(self._phase_order):
            phase_services = [
                svc for svc in self._services.values()
                if svc.phase == phase and svc.shutdown_fn is not None
            ]
            for svc in reversed(phase_services):
                name = svc.name
                instance = ctx.get(name)
                if instance is not None:
                    try:
                        log.info(f"Shutting down: {name}")
                        svc.shutdown_fn(instance)
                        svc.state = "stopped"
                    except Exception as e:
                        log.error(f"Error shutting down {name}: {e}")

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """Return status of all registered services."""
        return {
            name: {
                "phase": svc.phase.name,
                "state": svc.state,
                "critical": svc.critical,
                "error": svc.error,
                "restarts": svc.restart_count,
            }
            for name, svc in self._services.items()
        }