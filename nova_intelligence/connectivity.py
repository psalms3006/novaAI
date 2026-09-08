"""nova_intelligence.connectivity — Network and API health monitoring.

Continuously tracks whether the network is usable and whether the Gemini API
is reachable. Provides state to the router so it can make fast fallback
decisions without waiting for individual request timeouts.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


class ConnectivityState(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class ConnectivityManager:
    """Monitors network and API health.

    States:
        ONLINE   — internet reachable, API healthy
        DEGRADED — internet reachable but API slow/unhealthy
        OFFLINE  — no internet or API confirmed unreachable

    Checks are lightweight: DNS probe + optional API health endpoint.
    Run in a background thread; do not call from hot paths.
    """

    def __init__(
        self,
        dns_hosts: tuple[str, ...] = ("8.8.8.8", "1.1.1.1"),
        dns_timeout: float = 3.0,
        api_check_fn: Optional[callable] = None,
        check_interval: float = 15.0,
        degraded_threshold: float = 8.0,
    ):
        self._dns_hosts = dns_hosts
        self._dns_timeout = dns_timeout
        self._api_check_fn = api_check_fn
        self._check_interval = check_interval
        self._degraded_threshold = degraded_threshold

        self._state = ConnectivityState.OFFLINE
        self._last_check = 0.0
        self._last_dns_latency = 0.0
        self._last_api_latency = 0.0
        self._api_healthy = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def state(self) -> ConnectivityState:
        with self._lock:
            return self._state

    @property
    def is_online(self) -> bool:
        return self.state == ConnectivityState.ONLINE

    @property
    def is_degraded(self) -> bool:
        return self.state == ConnectivityState.DEGRADED

    @property
    def is_offline(self) -> bool:
        return self.state == ConnectivityState.OFFLINE

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "dns_latency_ms": round(self._last_dns_latency * 1000),
                "api_latency_ms": round(self._last_api_latency * 1000),
                "api_healthy": self._api_healthy,
                "last_check": self._last_check,
            }

    def check_now(self) -> ConnectivityState:
        """Perform an immediate connectivity check and return the new state."""
        t0 = time.time()
        dns_ok = self._check_dns()
        dns_latency = time.time() - t0

        # Run the API reachability check independently of the raw DNS probe.
        # The DNS probe (port 53 to 8.8.8.8) is frequently blocked even on a
        # perfectly good connection, so a reachable API host must be able to
        # declare the connection ONLINE on its own.
        api_ok = False
        api_latency = 0.0
        if self._api_check_fn:
            t1 = time.time()
            try:
                api_ok = self._api_check_fn()
            except Exception:
                api_ok = False
            api_latency = time.time() - t1

        with self._lock:
            self._last_dns_latency = dns_latency
            self._last_api_latency = api_latency
            self._api_healthy = api_ok
            self._last_check = time.time()

            if api_ok:
                # The actual model service is reachable — that is authoritative.
                self._state = ConnectivityState.ONLINE
            elif not dns_ok:
                self._state = ConnectivityState.OFFLINE
            elif self._api_check_fn:
                # DNS works but the API host is unreachable → degraded
                self._state = ConnectivityState.DEGRADED
            elif dns_latency > self._degraded_threshold:
                self._state = ConnectivityState.DEGRADED
            else:
                self._state = ConnectivityState.ONLINE

            new_state = self._state

        log.info(
            "[CONNECTIVITY] state=%s dns=%.0fms api=%.0fms api_healthy=%s",
            new_state.value,
            dns_latency * 1000,
            api_latency * 1000,
            api_ok,
        )
        return new_state

    def start_background(self) -> None:
        """Start background monitoring thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="nova-connectivity", daemon=True
        )
        self._thread.start()
        log.info("[CONNECTIVITY] background monitor started (interval=%.0fs)", self._check_interval)

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_now()
            except Exception as e:
                log.warning("[CONNECTIVITY] check error: %s", e)
            self._stop.wait(timeout=self._check_interval)

    def _check_dns(self) -> bool:
        for host in self._dns_hosts:
            try:
                sock = socket.create_connection((host, 53), timeout=self._dns_timeout)
                sock.close()
                return True
            except OSError:
                continue
        return False
