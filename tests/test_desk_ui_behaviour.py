"""What NOVA's window (desk/ui) must keep doing -- carried over from the tests
that guarded the previous interface (desk/static), one guarantee each.

Each check names the behaviour it protects, not the file layout, and several
exist because the behaviour was once broken in the old window. Where the
logic is a pure function it is executed (under Node, which runs TypeScript
directly), not just searched for.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "desk" / "ui" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def _all_sources() -> dict[str, str]:
    return {str(p.relative_to(SRC)).replace("\\", "/"): p.read_text(encoding="utf-8")
            for p in SRC.rglob("*.ts*")}


# ── one voice session, owned by the backend ─────────────────────────────────

def test_no_surface_captures_the_microphone_or_plays_audio_itself():
    """Two windows capturing or playing would mean two microphones feeding one
    conversation, or NOVA speaking twice. The backend owns both."""
    for name, text in _all_sources().items():
        for forbidden in ("getUserMedia", "new AudioContext", "speechSynthesis", "playAudioChunk"):
            assert forbidden not in text, f"{name} uses {forbidden}; audio belongs to the backend session"


def test_no_window_stops_the_session_when_it_is_hidden_or_closed():
    """Switching to ambient hides the main window. Hiding must not end the conversation."""
    for name, text in _all_sources().items():
        for event in ("beforeunload", "pagehide", "unload", "visibilitychange"):
            assert f"'{event}'" not in text and f'"{event}"' not in text, (
                f"{name} listens for {event}; minimising or switching modes could tear down the voice session")


def test_the_session_is_only_stopped_by_a_deliberate_act():
    """Stopping voice ends it for every surface, so only a user action may do it."""
    runtime = _read("nova/runtime.tsx")
    assert runtime.count("'/api/live/stop'") == 1, "voice is stopped from more than one place in the runtime"
    callers = {n: t for n, t in _all_sources().items() if re.search(r"\b(stopVoice|toggleVoice)\b", t) and n != "nova/runtime.tsx"}
    assert callers, "nothing can stop voice; check this test still means anything"
    for name, text in callers.items():
        for m in re.finditer(r"\b(?:rt\.)?(stopVoice|toggleVoice)\b", text):
            line = text[max(0, m.start() - 240):m.end()]
            assert re.search(r"onClick|run:|\bact\(", line), f"{name} stops voice outside a user action"


def test_a_failed_start_does_not_end_the_conversation():
    runtime = _read("nova/runtime.tsx")
    body = runtime[runtime.index("const startVoice = useCallback"):runtime.index("const stopVoice = useCallback")]
    assert "/api/live/stop" not in body and "stopVoice" not in body, (
        "a start that fails would end a conversation another window is having")


def test_the_window_reconnects_its_sockets():
    """One dropped socket must not leave the window deaf for the rest of the run."""
    events = _read("nova/events.ts")
    assert "private schedule()" in events and "setTimeout(() => {" in events
    assert "ws.onclose = dropped" in events and "this.schedule()" in events


def test_the_ambient_window_listens_on_the_bus_only():
    """The backend copies voice events onto the bus once per open live socket:
    a second live socket in the ambient window would double every event."""
    events = _read("nova/events.ts")
    assert "if (this.opts.liveSocket) this.live.open();" in events
    runtime = _read("nova/runtime.tsx")
    assert "new NovaEventHub({ liveSocket: !ambient })" in runtime


# ── what the window shows ───────────────────────────────────────────────────

def test_hearing_the_user_is_distinct_from_waiting():
    runtime = _read("nova/runtime.tsx")
    assert "listening: 'Hearing you'" in runtime and "idle: 'Ready'" in runtime
    visual = _read("nova/visual.ts")
    assert "case 'mic_level'" in visual, "the user's voice level never reaches the orb"


def test_being_interrupted_is_shown():
    runtime = _read("nova/runtime.tsx")
    assert "case 'interrupted':" in runtime and "'interrupted'" in runtime.split("export type Phase")[1][:1200]


def test_background_work_is_shown():
    runtime = _read("nova/runtime.tsx")
    assert "case 'task_activity':" in runtime and "setTaskBusy" in runtime


def test_reading_the_screen_is_shown_in_both_windows():
    runtime = _read("nova/runtime.tsx")
    assert "case 'VISION':\n        return 'looking';" in runtime
    assert "looking: 'Looking at the screen'" in runtime
    canvas = _read("components/three/SpatialCanvas.tsx")
    assert "p?.vision" in canvas, "the orb ignores the vision state"
    # The ambient window takes every bus event, including vision_capture.
    assert "opts.liveSocket ? isBusType : () => true" in _read("nova/events.ts")


def test_running_tasks_are_recognised_by_their_real_status():
    presence = _read("screens/PresenceScreen.tsx")
    nav = _read("components/NavRail.tsx")
    assert "RUNNING" in presence and "'RUNNING'" in nav


def test_every_voice_state_the_backend_publishes_is_handled():
    """desk/live_session.py LiveState, plus the states the manager publishes."""
    live = (ROOT / "desk" / "live_session.py").read_text(encoding="utf-8")
    block = live[live.index("class LiveState"):]
    block = block[:block.index("\n\n\n")]
    states = set(re.findall(r'=\s*"([a-z_]+)"', block))
    assert {"connecting", "connected", "streaming", "error", "closed"} <= states
    runtime = _read("nova/runtime.tsx")
    running = set(re.findall(r"'([a-z_]+)'", runtime[runtime.index("const VOICE_RUNNING"):].split(";")[0]))
    handled = running | {"error", "closed", "idle", "disconnecting", "offline"}
    assert not states - handled, f"voice states the window does not account for: {sorted(states - handled)}"


def _engine_states(script: str) -> dict:
    """Run the real visual engine (nova/visual.ts) under Node and report its state."""
    path = (SRC / "nova" / "visual.ts").as_uri()
    js = (f"const {{ VisualEngine }} = await import({json.dumps(path)});"
          "const e = new VisualEngine(); const ev = (type, x = {}) => e.interpret({ source: 'live', type, ts: 0, ...x });"
          "const run = (s) => { for (let i = 0; i < s * 10; i++) e.tick(0.1); return e.current; };"
          "const out = {};" + script + "process.stdout.write(JSON.stringify(out));")
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


BUSY = {"TOOL_SELECTION", "TOOL_EXECUTION", "VISION", "SEARCHING", "MEMORY_RETRIEVAL"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runs the real visual engine")
def test_a_refused_look_does_not_leave_her_working():
    """Seen in the real app: a look refused as too soon after the last one
    produced no tool_result, and the window said "Working" indefinitely."""
    out = _engine_states("ev('state', {state:'ready'}); run(3);"
                         "ev('tool_call', {tools:['vision']}); run(1); out.during = e.current;"
                         "ev('vision_refused', {since_s: 3.5}); out.after = run(3);")
    assert out["during"] in BUSY
    assert out["after"] not in BUSY, out


@pytest.mark.skipif(shutil.which("node") is None, reason="node runs the real visual engine")
def test_a_withdrawn_tool_call_ends_with_the_turn():
    out = _engine_states("ev('state', {state:'ready'}); run(3);"
                         "ev('tool_call', {tools:['vision']}); run(1);"
                         "ev('interrupted'); ev('turn_complete'); out.after = run(4);")
    assert out["after"] not in BUSY, out


@pytest.mark.skipif(shutil.which("node") is None, reason="node runs the real visual engine")
def test_a_tool_that_never_answers_stops_showing_as_work():
    out = _engine_states("ev('state', {state:'ready'}); run(3);"
                         "ev('tool_call', {tools:['web_search']}); out.soon = run(10); out.later = run(40);")
    assert out["soon"] in BUSY, "a real tool call must show as work while it runs"
    assert out["later"] not in BUSY, out


# ── ambient ─────────────────────────────────────────────────────────────────

def test_a_click_opens_the_full_window_and_a_drag_does_not():
    ambient = _read("components/AmbientView.tsx")
    up = ambient[ambient.index("onPointerUp"):]
    up = up[:up.index("}}")]
    assert "if (!p || p.moved) return" in up, "a drag would also open the full window"
    assert "'/api/ambient'" in up and "mode: 'full'" in up


# ── settings ────────────────────────────────────────────────────────────────

def test_every_settings_section_has_a_screen_behind_it():
    groups = _read("context/NovaSettingsContext.tsx")
    ids = set(re.findall(r"\{ id: '([a-z-]+)', label:", groups))
    assert ids, "no settings sections found"
    screen = _read("screens/settings/SettingsScreen.tsx")
    handled = set(re.findall(r"case '([a-z-]+)':", screen))
    assert not ids - handled, f"settings sections with nothing behind them: {sorted(ids - handled)}"


def test_gmail_can_be_connected_from_the_window():
    connections = _read("screens/settings/sections/ConnectionsSection.tsx")
    assert "/api/accounts/${provider}/connect" in connections
    # Consent finishes in a browser; the outcome arrives as account_changed.
    assert "case 'account_changed':" in _read("nova/runtime.tsx")
    assert "kind === 'account'" in connections, "the panel never learns the consent finished"


# ── account ─────────────────────────────────────────────────────────────────

def test_the_window_sends_the_header_the_bridge_checks():
    bridge = (ROOT / "desk" / "bridge.py").read_text(encoding="utf-8")
    header = re.search(r'request\.headers\.get\("([^"]+)",\s*""\)', bridge).group(1)
    assert f"'{header}': TOKEN" in _read("nova/api.ts"), f"the bridge checks {header}; the window does not send it"


def test_never_says_check_your_inbox_when_nothing_was_sent():
    gate = _read("components/FirstRun.tsx")
    step = gate[gate.index("const VerifyStep"):gate.index("I've confirmed my email")]
    sent, _, unsent = step.partition(": //")
    assert "acct?.email_delivery?.sent" in sent and "We've sent" in sent
    assert "could not confirm" in unsent and "We've sent" not in unsent


# ── first run ───────────────────────────────────────────────────────────────

def test_the_setup_screen_cannot_reopen_itself():
    """Once chosen, a latch -- not another status poll -- decides."""
    ob = _read("components/Onboarding.tsx")
    assert "if (done || !shouldShowOnboarding(rt.status)) return null;" in ob
    assert "setDone(true)" in ob


@pytest.mark.skipif(shutil.which("node") is None, reason="node runs the real onboarding module")
def test_the_setup_decision_runs_and_is_pure():
    path = (SRC / "nova" / "onboarding.ts").as_uri()
    script = (f"const m = await import({json.dumps(path)});"
              "process.stdout.write(JSON.stringify([m.shouldShowOnboarding({auth:{onboarded:false,has_credential:false}}),"
              "m.shouldShowOnboarding(null), m.shouldShowOnboarding({auth:{}})]));")
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, False, False]
    source = _read("nova/onboarding.ts")
    for forbidden in ("document.", "window.", "import "):
        assert forbidden not in source, f"onboarding.ts uses {forbidden}; keep the decision separate from the screen"
