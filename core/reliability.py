"""
NOVA Reliability — Circuit breaker, retry with backoff, watchdog, health monitor.

Inspired by VYREN's reliability.py. Provides defense-in-depth against transient failures.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, TypeVar

log = logging.getLogger("nova.reliability")

F = TypeVar("F", bound=Callable[..., Any])


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing — reject all calls
    HALF_OPEN = "half_open"  # Probing — allow one test call


@dataclass
class CircuitBreaker:
    """
    Circuit breaker to prevent cascading failures.

    States: CLOSED → OPEN → HALF_OPEN → CLOSED

    Usage:
        breaker = CircuitBreaker(name="gemini_api", failure_threshold=3, reset_timeout=30)
        with breaker:
            result = call_api()
    """
    name: str
    failure_threshold: int = 5
    reset_timeout: float = 30.0
    success_threshold: int = 1

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_failure_time >= self.reset_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    log.info(f"[{self.name}] Circuit → HALF_OPEN (probing)")
            return self._state

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    log.info(f"[{self.name}] Circuit → CLOSED (recovered)")
            else:
                self._failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                log.warning(f"[{self.name}] Circuit → OPEN (threshold={self.failure_threshold})")

    def __enter__(self) -> "CircuitBreaker":
        state = self.state
        if state == CircuitState.OPEN:
            raise RuntimeError(f"Circuit breaker '{self.name}' is OPEN — calls rejected.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self.record_success()
        else:
            self.record_failure()
        return False  # Don't suppress exceptions


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: tuple = (Exception,),
):
    """
    Retry decorator with exponential backoff and optional jitter.

    Usage:
        @with_retry(max_retries=3, base_delay=1.0)
        def fetch_data(): ...
    """
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (backoff_factor ** attempt)
                        if jitter:
                            delay *= (0.5 + random.random() * 0.5)
                        log.debug(f"Retry {attempt + 1}/{max_retries} for {func.__name__}: {e}")
                        time.sleep(delay)
            raise last_exc  # type: ignore
        return wrapper  # type: ignore
    return decorator


def with_retry_async(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
):
    """Async version of with_retry."""
    import asyncio

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (backoff_factor ** attempt)
                        if jitter:
                            delay *= (0.5 + random.random() * 0.5)
                        log.debug(f"Async retry {attempt + 1}/{max_retries}: {e}")
                        await asyncio.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


class Watchdog:
    """
    Monitors long-running operations and fires callbacks when they exceed timeout.

    Usage:
        wd = Watchdog()
        wd.start("gemini_call", timeout=30)
        # ... do work ...
        wd.stop("gemini_call")
    """

    def __init__(self, check_interval: float = 5.0) -> None:
        self._operations: Dict[str, dict] = {}
        self._callbacks: List[Callable[[str, float], None]] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._check_interval = check_interval

    def start(self, operation: str, timeout: float) -> None:
        """Begin tracking an operation."""
        with self._lock:
            self._operations[operation] = {
                "start_time": time.time(),
                "timeout": timeout,
            }

    def stop(self, operation: str) -> Optional[float]:
        """Stop tracking. Returns elapsed time, or None if not tracked."""
        with self._lock:
            entry = self._operations.pop(operation, None)
            if entry:
                return time.time() - entry["start_time"]
            return None

    def on_timeout(self, callback: Callable[[str, float], None]) -> None:
        """Register a timeout callback (operation_name, elapsed_seconds)."""
        self._callbacks.append(callback)

    def start_monitoring(self) -> None:
        """Start the background monitoring thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="Watchdog")
        self._thread.start()

    def stop_monitoring(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _monitor_loop(self) -> None:
        while self._running:
            time.sleep(self._check_interval)
            now = time.time()
            with self._lock:
                expired = []
                for name, entry in self._operations.items():
                    elapsed = now - entry["start_time"]
                    if elapsed > entry["timeout"]:
                        expired.append((name, elapsed))
            for name, elapsed in expired:
                log.warning(f"Watchdog: '{name}' exceeded timeout ({elapsed:.1f}s)")
                for cb in self._callbacks:
                    try:
                        cb(name, elapsed)
                    except Exception as e:
                        log.error(f"Watchdog callback error: {e}")


class HealthMonitor:
    """
    Registry of health check functions for subsystem monitoring.

    Usage:
        monitor = HealthMonitor()
        monitor.register("memory", lambda: os.path.exists("memory.index"))
        status = monitor.check_all()  # {"memory": "healthy", ...}
    """

    def __init__(self) -> None:
        self._checks: Dict[str, Callable[[], bool]] = {}

    def register(self, name: str, check_fn: Callable[[], bool]) -> None:
        self._checks[name] = check_fn

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)

    def check(self, name: str) -> str:
        """Check a single subsystem. Returns 'healthy', 'degraded', or 'down'."""
        fn = self._checks.get(name)
        if fn is None:
            return "unknown"
        try:
            return "healthy" if fn() else "down"
        except Exception:
            return "degraded"

    def check_all(self) -> Dict[str, str]:
        """Check all registered subsystems."""
        return {name: self.check(name) for name in self._checks}