"""
reliability.py — Production reliability patterns for NOVA.

Implements:
  - Retry policies with exponential backoff and jitter
  - Circuit breakers (fail-fast after repeated failures)
  - Watchdog (detects stuck operations)
  - Structured logging configuration
  - Health monitoring
  - Graceful degradation
"""

import functools
import logging
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

logger = logging.getLogger("NOVA.reliability")

F = TypeVar("F", bound=Callable)


# ---------------------------------------------------------------------------
# Structured Logging Setup
# ---------------------------------------------------------------------------

def setup_logging(level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    """Configure structured logging for all NOVA modules."""
    from pathlib import Path

    root = logging.getLogger("nova")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    root.addHandler(handler)

    if log_file:
        from pathlib import Path
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    return root


# ---------------------------------------------------------------------------
# Retry Policy
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    """Configuration for retry behavior."""
    max_retries: int = 3
    base_delay: float = 1.0        # seconds
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter: bool = True             # Add randomness to prevent thundering herd
    retryable_exceptions: tuple = (Exception,)


def with_retry(policy: RetryPolicy | None = None):
    """Decorator that retries a function on failure."""
    _policy = policy or RetryPolicy()

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(_policy.max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except _policy.retryable_exceptions as e:
                    last_exc = e
                    if attempt == _policy.max_retries:
                        break
                    delay = min(
                        _policy.base_delay * (_policy.backoff_factor ** attempt),
                        _policy.max_delay,
                    )
                    if _policy.jitter:
                        delay *= (0.5 + random.random() * 0.5)
                    logger.warning(
                        f"Retry {attempt + 1}/{_policy.max_retries} "
                        f"for {fn.__name__}: {e}. Waiting {delay:.1f}s"
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore
        return wrapper  # type: ignore
    return decorator


def with_retry_async(policy: RetryPolicy | None = None):
    """Async version of with_retry."""
    _policy = policy or RetryPolicy()
    import asyncio

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(_policy.max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except _policy.retryable_exceptions as e:
                    last_exc = e
                    if attempt == _policy.max_retries:
                        break
                    delay = min(
                        _policy.base_delay * (_policy.backoff_factor ** attempt),
                        _policy.max_delay,
                    )
                    if _policy.jitter:
                        delay *= (0.5 + random.random() * 0.5)
                    logger.warning(
                        f"Retry {attempt + 1}/{_policy.max_retries} "
                        f"for {fn.__name__}: {e}. Waiting {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitState(str, Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject all calls
    HALF_OPEN = "half_open" # Testing if recovered


class CircuitBreaker:
    """
    Prevents cascading failures by failing fast after repeated errors.

    Usage:
        breaker = CircuitBreaker("gemini_api", failure_threshold=5, recovery_timeout=60)

        with breaker:
            result = call_external_api()

        # Or:
        if breaker.allow():
            try:
                result = call_external_api()
                breaker.record_success()
            except Exception:
                breaker.record_failure()
    """

    def __init__(self, name: str, failure_threshold: int = 5,
                 recovery_timeout: float = 60.0):
        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._success_count = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has elapsed
                if time.time() - self._last_failure_time > self._recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
            return self._state

    def allow(self) -> bool:
        """Should we attempt the call?"""
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.HALF_OPEN:
            return True  # Allow one test call
        return False  # OPEN: reject

    def record_success(self):
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                logger.info(f"Circuit breaker '{self.name}' recovered → CLOSED")
            self._failure_count = 0
            self._success_count += 1

    def record_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit breaker '{self.name}' opened after "
                    f"{self._failure_count} failures"
                )

    def __enter__(self):
        if not self.allow():
            raise RuntimeError(f"Circuit breaker '{self.name}' is OPEN")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.record_failure()
        else:
            self.record_success()

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "threshold": self._failure_threshold,
        }


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

class Watchdog:
    """
    Detects stuck or hung operations.

    Usage:
        watchdog = Watchdog(timeout=30)

        # Start monitoring
        watchdog.start("tool_execution")

        # ... long operation ...

        watchdog.stop("tool_execution")  # Completed normally

        # If stop() isn't called within timeout, the watchdog fires
    """

    def __init__(self, default_timeout: float = 60.0):
        self._default_timeout = default_timeout
        self._operations: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._callbacks: list[Callable] = []

    def on_timeout(self, callback: Callable):
        """Register a callback for when an operation times out."""
        self._callbacks.append(callback)

    def start(self, operation: str, timeout: float | None = None):
        """Start monitoring an operation."""
        with self._lock:
            self._operations[operation] = {
                "started": time.time(),
                "timeout": timeout or self._default_timeout,
            }

    def stop(self, operation: str):
        """Mark an operation as completed."""
        with self._lock:
            self._operations.pop(operation, None)

    def check(self) -> list[dict]:
        """Check for timed-out operations. Returns list of stuck ops."""
        now = time.time()
        stuck = []
        with self._lock:
            for name, op in list(self._operations.items()):
                elapsed = now - op["started"]
                if elapsed > op["timeout"]:
                    stuck.append({
                        "operation": name,
                        "elapsed_seconds": round(elapsed, 1),
                        "timeout": op["timeout"],
                    })
                    # Remove the stuck operation
                    del self._operations[name]

        for s in stuck:
            for cb in self._callbacks:
                try:
                    cb(s)
                except Exception as e:
                    logger.error(f"Watchdog callback error: {e}")

        return stuck

    def start_monitoring(self, interval: float = 10.0):
        """Start a background thread that checks for stuck operations."""
        def _loop():
            import time
            while True:
                self.check()
                time.sleep(interval)

        t = threading.Thread(target=_loop, name="watchdog", daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# Health Monitor
# ---------------------------------------------------------------------------

class HealthMonitor:
    """Tracks subsystem health for diagnostics."""

    def __init__(self):
        self._checks: dict[str, Callable[[], bool]] = {}
        self._status: dict[str, str] = {}  # name -> "healthy" | "degraded" | "down"
        self._last_check: dict[str, float] = {}

    def register(self, name: str, check_fn: Callable[[], bool]):
        """Register a health check. Returns True if healthy."""
        self._checks[name] = check_fn

    def check_all(self) -> dict:
        """Run all health checks. Returns status dict."""
        for name, check_fn in self._checks.items():
            try:
                healthy = check_fn()
                self._status[name] = "healthy" if healthy else "degraded"
            except Exception:
                self._status[name] = "down"
            self._last_check[name] = time.time()
        return dict(self._status)

    def is_healthy(self) -> bool:
        """Check if all subsystems are healthy."""
        self.check_all()
        return all(s == "healthy" for s in self._status.values())

    def get_status(self) -> dict:
        return {
            "checks": len(self._checks),
            "status": dict(self._status),
            "healthy": all(s == "healthy" for s in self._status.values()),
        }