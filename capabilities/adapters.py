"""
Dynamic handlers that adapt NOVA's existing ``actions/*`` tool modules.

The registry prefers explicit capability handlers; for the long tail of
``actions.*`` tools it resolves the module and mirrors the exact call
shape ``nova._execute_tool_sync`` and ``agent/executor._call_tool`` used:

- module with ``execute(args)``  → most ``actions`` modules
- ``screen_process``            → ``actions.screen_processor.screen_process``
- ``dev_agent``                 → ``actions.dev_agent.dev_agent`` (passes speak)

Resolution failures raise ``CapabilityNotFoundError`` so the caller
(orchestrator / agent executor) can replan instead of silently succeeding.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Dict, Optional

from capabilities.contracts import CapabilityContext

log = logging.getLogger("nova.capabilities.adapters")

# tool name -> (module name, callable name)
_SPECIAL_MODULES: Dict[str, str] = {
    "screen_process": "screen_processor",
    "close_app": "close_app",
    "file_processor": "file_processor",
    "computer_control": "computer_control",
    "send_message": "send_message",
    "reminder": "reminder",
    "generated_code": "generated_code",
}


def _import_actions_module(module_name: str):
    """Import an ``actions.<module>`` module; returns None when unavailable."""
    try:
        return importlib.import_module(f"actions.{module_name}")
    except Exception as exc:  # noqa: BLE001 — module may genuinely be missing
        log.debug("actions.%s unavailable: %s", module_name, exc)
        return None


def resolve_dynamic_handler(name: str) -> Optional[Callable[[Dict[str, Any], CapabilityContext], str]]:
    """Return a handler that reproduces the existing ``actions`` call shapes."""
    module_name = _SPECIAL_MODULES.get(name, name)
    module = _import_actions_module(module_name)
    if module is None:
        return None

    if name == "screen_process":
        fn = getattr(module, "screen_process", None)
        if fn is None:
            return None
        return lambda args, ctx: str(fn(parameters=args, player=None) or "Screen captured and analyzed.")

    if name == "dev_agent":
        fn = getattr(module, "dev_agent", None)
        if fn is None:
            return None
        return lambda args, ctx: str(
            fn(parameters=args, player=None, speak=ctx.speak) or "Done."
        )

    execute = getattr(module, "execute", None)
    if execute is None:
        # Fallback to a bare function named after the tool.
        fn = getattr(module, name, None)
        if not callable(fn):
            return None
        return lambda args, ctx: str(fn(args) or "Done.")

    return lambda args, ctx: str(execute(args) or "Done.")