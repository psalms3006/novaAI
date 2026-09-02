"""
Capability-coverage test — every tool the live runtime dispatches must have
CapabilitySpec metadata.

This is the switchover-readiness guard: a live tool with no metadata would be
reported ``no-metadata`` by the shadow monitor (an un-assessable blind spot).
If someone adds a new tool to the executor without a spec, this fails.
"""
from __future__ import annotations

from capabilities.registry import build_core_registry

# Tools dispatched by agent/executor._dispatch_tool + nova._execute_tool_sync.
LIVE_TOOLS = {
    # agent/executor.py dispatch surface
    "open_app", "web_search", "game_updater", "browser_control", "file_controller",
    "cmd_control", "code_helper", "dev_agent", "screen_process", "send_message",
    "reminder", "youtube_video", "weather_report", "computer_settings",
    "desktop_control", "computer_control", "flight_finder", "generated_code",
    # nova.py dispatcher surface
    "vision", "file_processor", "self_editor", "planner", "autostart",
    "remember_fact", "close_app", "list_session_facts",
}


def test_every_live_tool_has_metadata():
    reg = build_core_registry()
    have = set(reg.names())
    missing = sorted(LIVE_TOOLS - have)
    assert not missing, f"no-metadata blind spot for live tools: {missing}"


def test_metadata_aligns_with_nova_safety_gate():
    """Consequential live tools must be ALWAYS-confirm under the registry rules."""
    reg = build_core_registry()
    for tool in ("game_updater", "desktop_control", "cmd_control", "generated_code"):
        spec = reg.get(tool)
        assert spec is not None, tool
        assert spec.effective_policy().value == "always", tool


def test_safe_subactions_stay_unconfirmed():
    reg = build_core_registry()
    updater = reg.get("game_updater")
    assert updater.action_is_safe("list")
    assert updater.action_is_safe("download_status")
    assert not updater.action_is_safe("install")