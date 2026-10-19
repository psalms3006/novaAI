"""
Recovery Manager — Diagnoses shell failures and suggests safe repairs.
"""
from __future__ import annotations
import logging
from core.shell_manager import ShellExecutionError, ShellResult
from core.capability_bus import CapabilityBus

log = logging.getLogger("nova.recovery")

class RecoveryManager:
    def __init__(self, capability_bus: CapabilityBus):
        self.bus = capability_bus

    def diagnose_and_repair(self, error: ShellExecutionError) -> Optional[str]:
        result = error.result
        log.info(f"Diagnosing shell failure: {result}")
        
        # Diagnostic rules
        if "Executable not found" in (result.error or ""):
            return "MISSING_EXECUTABLE"
        if result.returncode == 127: # Standard bash/cmd not found
            return "MISSING_EXECUTABLE"
        if "timed out" in (result.error or "").lower():
            return "TIMEOUT"
            
        return "UNKNOWN_FAILURE"
