"""
NOVA Agent Error Handler — Error analysis and fix generation.

This module was missing from the original codebase, causing AgentExecutor
to crash with ModuleNotFoundError. Provides error classification and
remediation suggestions.
"""
from __future__ import annotations

import logging
import re
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Optional

log = logging.getLogger("nova.agent.error_handler")


class ErrorDecision(Enum):
    """Decision on how to handle an error."""
    RETRY = "retry"
    REPLAN = "replan"
    ABORT = "abort"
    SKIP = "skip"
    ESCALATE = "escalate"


@dataclass
class ErrorAnalysis:
    """Result of error analysis."""
    error_type: str
    category: str
    decision: ErrorDecision
    description: str
    suggestion: str
    retry_count: int = 0


def analyze_error(error: Exception, context: str = "") -> ErrorAnalysis:
    """
    Analyze an error and determine the best recovery strategy.

    Categories:
      - network: Transient network issues → RETRY
      - auth: Authentication failures → ABORT (can't self-fix)
      - rate_limit: API rate limiting → RETRY with backoff
      - file_not_found: Missing files → REPLAN or SKIP
      - permission: Access denied → ABORT
      - timeout: Operation timed out → RETRY
      - validation: Invalid input → ABORT
      - unknown: Unrecognized → ESCALATE
    """
    error_str = str(error).lower()
    error_type = type(error).__name__
    tb_str = traceback.format_exc()

    # Network errors
    if any(k in error_str for k in [
        "connection", "timeout", "timed out", "network", "socket",
        "connectionreset", "connectionrefused", "eoferror"
    ]):
        return ErrorAnalysis(
            error_type=error_type,
            category="network",
            decision=ErrorDecision.RETRY,
            description=f"Network error: {error}",
            suggestion="Retry after a brief delay. Check network connectivity.",
        )

    # Rate limiting
    if any(k in error_str for k in ["429", "rate_limit", "resource_exhausted", "quota"]):
        return ErrorAnalysis(
            error_type=error_type,
            category="rate_limit",
            decision=ErrorDecision.RETRY,
            description=f"Rate limited: {error}",
            suggestion="Wait for rate limit window to expire, then retry with backoff.",
        )

    # Authentication
    if any(k in error_str for k in ["401", "403", "unauthorized", "forbidden", "auth"]):
        return ErrorAnalysis(
            error_type=error_type,
            category="auth",
            decision=ErrorDecision.ABORT,
            description=f"Authentication error: {error}",
            suggestion="Check API key configuration. This cannot be auto-fixed.",
        )

    # File not found
    if any(k in error_str for k in ["filenotfound", "no such file", "not found"]):
        return ErrorAnalysis(
            error_type=error_type,
            category="file_not_found",
            decision=ErrorDecision.REPLAN,
            description=f"File not found: {error}",
            suggestion="Verify the file path exists. The plan may need adjustment.",
        )

    # Permission errors
    if any(k in error_str for k in ["permission", "access denied", "permissionerror"]):
        return ErrorAnalysis(
            error_type=error_type,
            category="permission",
            decision=ErrorDecision.ABORT,
            description=f"Permission error: {error}",
            suggestion="Check file/directory permissions. User intervention needed.",
        )

    # Timeout
    if "timeout" in error_str or "timed out" in error_str:
        return ErrorAnalysis(
            error_type=error_type,
            category="timeout",
            decision=ErrorDecision.RETRY,
            description=f"Timeout: {error}",
            suggestion="Increase timeout or retry.",
        )

    # Validation errors
    if any(k in error_str for k in ["validation", "invalid", "valueerror", "typeerror", "keyerror"]):
        return ErrorAnalysis(
            error_type=error_type,
            category="validation",
            decision=ErrorDecision.ABORT,
            description=f"Validation error: {error}",
            suggestion="Fix the input parameters.",
        )

    # Module/import errors
    if any(k in error_str for k in ["modulenotfound", "importerror"]):
        return ErrorAnalysis(
            error_type=error_type,
            category="import",
            decision=ErrorDecision.ABORT,
            description=f"Import error: {error}",
            suggestion="Install missing module or fix import path.",
        )

    # Default
    return ErrorAnalysis(
        error_type=error_type,
        category="unknown",
        decision=ErrorDecision.ESCALATE,
        description=f"Unknown error: {error}",
        suggestion="Manual investigation needed. Error logged for review.",
    )


def generate_fix(analysis: ErrorAnalysis, context: str = "") -> Optional[str]:
    """
    Generate a fix suggestion based on error analysis.
    Returns a natural language suggestion, or None if no fix is possible.
    """
    fixes = {
        "network": "I encountered a network issue. Let me retry the operation.",
        "rate_limit": "The API rate limit was hit. I'll wait and try again with proper backoff.",
        "file_not_found": f"The file wasn't found. Could you verify the path? {analysis.description}",
        "permission": "I don't have permission for this action. You may need to adjust file permissions.",
        "timeout": "The operation timed out. I'll try again with a longer timeout.",
        "auth": "There's an authentication issue. Please check your API key configuration.",
        "validation": f"There was a validation error: {analysis.description}",
        "import": "A required module is missing. Please install it with pip.",
        "unknown": f"An unexpected error occurred: {analysis.description}",
    }
    return fixes.get(analysis.category, f"Error: {analysis.description}")