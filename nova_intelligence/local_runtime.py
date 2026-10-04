"""nova_intelligence.local_runtime — Local runtime lifecycle management.

Detects Ollama installation, starts/stops the process, and provides
health monitoring. NOVA should NOT require the user to manually run
`ollama serve` — this module handles it transparently.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from enum import Enum, auto
from typing import Optional

log = logging.getLogger(__name__)

OLLAMA_DEFAULT_URL = "http://localhost:11434"
OLLAMA_COMMON_PATHS = {
    "win32": [
        r"C:\Users\{user}\AppData\Local\Programs\Ollama\ollama.exe",
        r"C:\Program Files\Ollama\ollama.exe",
        r"C:\ProgramData\Ollama\ollama.exe",
    ],
    "darwin": [
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama",
    ],
    "linux": [
        "/usr/local/bin/ollama",
        "/usr/bin/ollama",
    ],
}


class RuntimeState(Enum):
    """State of the local Ollama runtime."""
    UNKNOWN = auto()
    NOT_INSTALLED = auto()
    INSTALLED_NOT_RUNNING = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    FAILED = auto()


class LocalRuntimeManager:
    """Manages the Ollama local runtime lifecycle.
    
    Responsibilities:
    - Detect whether Ollama is installed
    - Start Ollama automatically when needed
    - Stop Ollama on shutdown (configurable)
    - Health-check the running process
    - Provide installation guidance if not found
    """

    def __init__(
        self,
        ollama_url: str = OLLAMA_DEFAULT_URL,
        auto_start: bool = True,
        auto_stop: bool = False,
        # Ollama's first `serve` on Windows routinely needs well over 15s
        # (service registration + GPU probe). Timing out early marked a
        # perfectly healthy runtime FAILED, so the UI reported "local AI
        # unavailable" while the router was successfully using it.
        start_timeout: float = 45.0,
    ):
        self._ollama_url = ollama_url.rstrip("/")
        self._auto_start = auto_start
        self._auto_stop = auto_stop
        self._start_timeout = start_timeout
        self._state = RuntimeState.UNKNOWN
        self._ollama_path: Optional[str] = None
        self._process: Optional[subprocess.Popen] = None
        self._pid: Optional[int] = None
        self._started_by_nova = False

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def ollama_path(self) -> Optional[str]:
        return self._ollama_path

    @property
    def is_running(self) -> bool:
        """Whether Ollama is answering right now.

        This re-probes instead of trusting the cached state: the state is only
        written at start-up, so a runtime that came up slowly stayed reported as
        FAILED/STARTING for the rest of the session even while serving requests.
        """
        if self._state == RuntimeState.RUNNING:
            return True
        if self._state in (RuntimeState.NOT_INSTALLED, RuntimeState.STOPPING):
            return False
        if self._check_http_alive():
            self._state = RuntimeState.RUNNING
            return True
        return False

    def detect(self) -> RuntimeState:
        """Detect Ollama installation without starting it.
        
        Returns the detected state.
        """
        # 1. Check if already running via HTTP
        if self._check_http_alive():
            self._state = RuntimeState.RUNNING
            log.info("[RUNTIME] Ollama detected: already running")
            return self._state

        # 2. Find the ollama executable
        path = self._find_ollama_binary()
        if path is None:
            self._state = RuntimeState.NOT_INSTALLED
            log.info("[RUNTIME] Ollama not found")
            return self._state

        self._ollama_path = path
        self._state = RuntimeState.INSTALLED_NOT_RUNNING
        log.info("[RUNTIME] Ollama found at %s", path)
        return self._state

    def ensure_running(self) -> bool:
        """Ensure Ollama is running. Auto-starts if needed.
        
        Returns True if Ollama is running after the call.
        """
        # Already running?
        if self._check_http_alive():
            self._state = RuntimeState.RUNNING
            return True

        # Not installed?
        if self._ollama_path is None:
            self.detect()
        if self._state == RuntimeState.NOT_INSTALLED:
            return False

        # Not auto-starting?
        if not self._auto_start:
            self._state = RuntimeState.INSTALLED_NOT_RUNNING
            return False

        # Start it
        return self.start()

    def start(self) -> bool:
        """Start the Ollama process.
        
        Returns True if started successfully.
        """
        if self._check_http_alive():
            self._state = RuntimeState.RUNNING
            return True

        if self._ollama_path is None:
            path = self._find_ollama_binary()
            if path is None:
                self._state = RuntimeState.NOT_INSTALLED
                return False
            self._ollama_path = path

        self._state = RuntimeState.STARTING
        log.info("[RUNTIME] Starting Ollama from %s ...", self._ollama_path)

        try:
            # Start ollama serve in a subprocess
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NO_WINDOW

            self._process = subprocess.Popen(
                [self._ollama_path, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags if sys.platform == "win32" else 0,
            )
            self._pid = self._process.pid
            self._started_by_nova = True

            # Wait for it to become responsive
            if self._wait_for_ready(self._start_timeout):
                self._state = RuntimeState.RUNNING
                log.info("[RUNTIME] Ollama started (pid=%d)", self._pid)
                return True
            else:
                log.warning("[RUNTIME] Ollama started but not responding within %.1fs", self._start_timeout)
                # It may still be coming up. Leave the state as STARTING rather
                # than FAILED so a later health_check()/is_running() can promote
                # it — a slow start is not a permanent failure, and marking it
                # FAILED made NOVA report local AI as unavailable for the whole
                # session even once Ollama answered normally.
                self._state = RuntimeState.STARTING
                return False

        except FileNotFoundError:
            log.error("[RUNTIME] Cannot execute: %s", self._ollama_path)
            self._state = RuntimeState.FAILED
            return False
        except Exception as e:
            log.error("[RUNTIME] Failed to start Ollama: %s", e)
            self._state = RuntimeState.FAILED
            return False

    def stop(self) -> bool:
        """Stop the Ollama process (only if NOVA started it)."""
        if self._process is None or self._pid is None:
            return True

        if not self._started_by_nova:
            log.info("[RUNTIME] Ollama was not started by NOVA; not stopping")
            return True

        self._state = RuntimeState.STOPPING
        log.info("[RUNTIME] Stopping Ollama (pid=%d) ...", self._pid)

        try:
            if sys.platform == "win32":
                self._process.terminate()
            else:
                self._process.send_signal(signal.SIGTERM)
            self._process.wait(timeout=5)
            log.info("[RUNTIME] Ollama stopped")
            self._state = RuntimeState.INSTALLED_NOT_RUNNING
            return True
        except subprocess.TimeoutExpired:
            log.warning("[RUNTIME] Ollama did not stop gracefully; killing")
            try:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception:
                pass
            self._state = RuntimeState.INSTALLED_NOT_RUNNING
            return True
        except Exception as e:
            log.error("[RUNTIME] Failed to stop Ollama: %s", e)
            self._state = RuntimeState.FAILED
            return False
        finally:
            self._process = None
            self._pid = None

    def health_check(self) -> dict:
        """Check runtime health and return diagnostics."""
        alive = self._check_http_alive()
        if alive:
            self._state = RuntimeState.RUNNING

        process_alive = False
        if self._process is not None:
            process_alive = self._process.poll() is None

        return {
            "state": self._state.name,
            "http_alive": alive,
            "process_alive": process_alive,
            "pid": self._pid,
            "ollama_path": self._ollama_path,
            "started_by_nova": self._started_by_nova,
            "url": self._ollama_url,
        }

    def get_install_guidance(self) -> str:
        """Return human-readable installation guidance."""
        if sys.platform == "win32":
            return (
                "Ollama is not installed. Download from https://ollama.com/download/windows\n"
                "After installing, restart NOVA — it will detect Ollama automatically."
            )
        elif sys.platform == "darwin":
            return (
                "Ollama is not installed. Install with: brew install ollama\n"
                "Or download from https://ollama.com/download/mac"
            )
        else:
            return (
                "Ollama is not installed. Install with:\n"
                "  curl -fsSL https://ollama.com/install.sh | sh\n"
                "Or visit https://ollama.com/download"
            )

    # ── Private helpers ──────────────────────────────────────────────────────

    def _find_ollama_binary(self) -> Optional[str]:
        """Locate the ollama binary on this system."""
        # 1. Check PATH
        found = shutil.which("ollama")
        if found:
            return found

        # 2. Check platform-specific common paths
        paths = OLLAMA_COMMON_PATHS.get(sys.platform, [])
        for template in paths:
            path = template.replace("{user}", os.environ.get("USERNAME", ""))
            if os.path.isfile(path):
                return path

        return None

    def _check_http_alive(self) -> bool:
        """Check if Ollama HTTP API is responsive."""
        try:
            import requests
            r = requests.get(f"{self._ollama_url}/api/tags", timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    def _wait_for_ready(self, timeout: float) -> bool:
        """Poll HTTP endpoint until Ollama responds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._check_http_alive():
                return True
            # Check if process died
            if self._process and self._process.poll() is not None:
                log.error("[RUNTIME] Ollama process exited with code %s", self._process.returncode)
                return False
            time.sleep(0.5)
        return False
