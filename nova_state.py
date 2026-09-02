"""
nova_state.py
══════════════
Shared mutable state, owned centrally. nova.py and every *_extra.py module
import this and reference these names as `nova_state.X`, never as a local
alias — that's the rule that keeps this a leaf module with no dependents
importing back into nova.py. Do not import nova.py from here, ever.
"""
from __future__ import annotations
from typing import Any, List, Optional
import threading

_embedder: Optional[Any] = None
_embedder_loaded = threading.Event()
_memory_texts: List[str] = []
_planner: Optional[Any] = None
_rest_backoff_until: float = 0.0
_rest_backoff_secs: float = 0.0
_mcp_bridge: Optional[Any] = None  # nova_mcp.bridge.MCPBridge instance, set in main()
_living_memory: Optional[Any] = None  # living_memory.LivingMemory, set in main()
_task_manager: Optional[Any] = None   # task_manager.TaskManager, set in main()
