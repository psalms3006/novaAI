"""
Shell Manager — Centralized, safe subprocess execution and diagnosis.

Provides a unified interface for running commands without shell injection risks
and captures rich diagnostic information for recovery.
"""
from __future__ import annotations

import subprocess
import logging
from typing import List, Optional, Tuple, Dict, Any
from core.utils import sanitize_shell_arg

log = logging.getLogger("nova.shell")

class ShellResult:
    def __init__(self, returncode: int, stdout: str, stderr: str, error: Optional[str] = None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error

    def is_success(self) -> bool:
        return self.returncode == 0 and self.error is None

    def __repr__(self) -> str:
        return f"ShellResult(rc={self.returncode}, stdout={self.stdout[:50]!r}, stderr={self.stderr[:50]!r}, error={self.error!r})"

class ShellExecutionError(Exception):
    def __init__(self, result: ShellResult):
        self.result = result
        super().__init__(result.error or f"Shell execution failed with rc={result.returncode}")

def execute_safe(
    cmd: List[str],
    timeout: int = 10,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None
) -> ShellResult:
    """Execute a command safely using an argument list instead of shell=True."""
    # The executable is trusted (first element), only arguments are sanitized.
    executable = cmd[0]
    args = cmd[1:]
    
    # Sanitize arguments before execution
    try:
        sanitized_args = [sanitize_shell_arg(arg) for arg in args]
    except ValueError as e:
        return ShellResult(1, "", "", error=str(e))

    sanitized_cmd = [executable] + sanitized_args

    try:
        log.info(f"Executing: {' '.join(sanitized_cmd)}")
        result = subprocess.run(
            sanitized_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
            encoding="utf-8",
            errors="replace"
        )
        return ShellResult(result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired as e:
        return ShellResult(1, "", "", error=f"Command timed out after {timeout}s: {str(e)}")
    except FileNotFoundError as e:
        return ShellResult(1, "", "", error=f"Executable not found: {str(e)}")
    except Exception as e:
        return ShellResult(1, "", "", error=f"Execution failed: {str(e)}")
