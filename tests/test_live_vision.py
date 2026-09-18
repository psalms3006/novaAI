"""Looking at the screen happens inside the conversation, not beside it.

NOVA has two ways to see. The ambient sampler trickles low-resolution frames
into the live session as context, so that when someone asks "what is this?"
the answer is already in front of the model. This file is about the other
one: the user asking to be looked at, explicitly, now.

That used to go to NOVA Core's vision tool, which captures the screen, writes
it to disk and asks a second model about it over REST. A tool call blocks the
turn, so the user heard several seconds of nothing, and the answer came back
as a string for the live model to read out — a second round trip on a second
quota, describing a screenshot the conversation never saw.

What replaces it shows the screen to the model already in the conversation,
and says something out loud while it does.
"""
from __future__ import annotations

import asyncio
import queue as q
import time
import threading

import pytest

import nova_voice
from desk import live_session as ls


class FakeFC:
    id = "call-1"
    name = "vision"


class FakeSession:
    """Records what was sent, the way the real session would receive it."""

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self._fail = fail

    async def send_client_content(self, turns=None, turn_complete=None):
        if self._fail:
            raise ConnectionError("socket gone")
        self.sent.append({"turns": turns, "turn_complete": turn_complete})


def session():
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._play_q = q.Queue(maxsize=200)
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._speaker_alive = True
    m._gate = nova_voice.VoiceGate(chunk_samples=1024, on_barge_in=lambda: None)
    m._pending_vision = None
    m._vision_busy = False
    m._vision_busy_at = 0.0
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    return m


def events(m, kind):
    return [e for e in m._published if e.type == kind]


# ── which calls the session can answer itself ────────────────────────────────

def test_a_plain_look_at_the_screen_is_answered_in_session():
    assert session()._vision_in_session({"question": "what am I looking at?"})


def test_the_default_angle_is_the_screen():
    assert session()._vision_in_session({})


def test_the_camera_is_a_source_not_a_separate_pipeline():
    """Looking through the webcam is the same conversational act as looking at
    the screen, and it was the slower of the two: three consecutive camera
    calls in one session took 11.6s, 10.0s and then a timeout, 33 seconds of
    a blocked conversation."""
    assert session()._vision_in_session({"angle": "camera"})


def test_an_angle_that_is_not_a_source_still_goes_to_core():
    assert not session()._vision_in_session({"angle": "telescope"})


@pytest.mark.parametrize("arg", ls.LiveManager._VISION_NEEDS_CORE)
def test_work_that_needs_core_still_goes_to_core(arg):
    """OCR and face work run local models; saving writes a file. None of that
    is something a picture dropped into a conversation can do."""
    assert not session()._vision_in_session({arg: True})


def test_a_second_look_is_refused_while_the_first_is_in_flight():
    """Otherwise two screenshots stack and NOVA describes the screen twice."""
    m = session()
    m._vision_busy = True
    m._vision_busy_at = time.monotonic()
    assert not m._vision_in_session({})


# ── capturing ────────────────────────────────────────────────────────────────

def test_capturing_returns_at_once_and_asks_NOVA_to_say_so():
    """The silence is the bug. A tool call blocks the turn, so the answer has
    to be "I'm looking", said immediately, not the description."""
    m = session()
    resp = asyncio.run(m._look(FakeFC(), {"question": "read this"}))
    out = resp.response["output"].lower()
    assert "say" in out and "looking" in out
    assert "do not describe" in out, (
        "nothing stops the model inventing a description before it has seen "
        "anything")


def test_capturing_holds_the_frame_for_the_end_of_the_turn():
    m = session()
    asyncio.run(m._look(FakeFC(), {"question": "read this"}))
    assert m._pending_vision is not None
    jpeg, mime, question = m._pending_vision
    assert mime == "image/jpeg"
    assert len(jpeg) > 1000, "that is not a screenshot"
    assert question == "read this"
    assert m._vision_busy


def test_the_users_question_travels_with_the_frame():
    """The model has to answer what was asked, not describe the desktop."""
    m = session()
    asyncio.run(m._look(FakeFC(), {"question": "what error is this?"}))
    assert m._pending_vision[2] == "what error is this?"


def test_a_missing_question_still_asks_something():
    m = session()
    asyncio.run(m._look(FakeFC(), {}))
    assert m._pending_vision[2].strip()


def test_a_capture_that_fails_says_so_and_does_not_stay_busy(monkeypatch):
    """A stuck busy flag is a NOVA who will not look at anything again."""
    m = session()
    monkeypatch.setattr(ls, "capture_once",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no mss")))
    resp = asyncio.run(m._look(FakeFC(), {}))
    assert "could not use" in resp.response["output"].lower()
    assert m._pending_vision is None
    assert not m._vision_busy
    assert events(m, "vision_failed")


# ── showing the model ────────────────────────────────────────────────────────

def test_the_frame_is_sent_as_its_own_turn():
    """Unlike an ambient frame, this one is the question. It closes the turn
    so the model answers instead of filing the picture away as context."""
    m = session()
    asyncio.run(m._look(FakeFC(), {"question": "read this"}))
    s = FakeSession()
    asyncio.run(m._send_pending_vision(s))

    assert len(s.sent) == 1
    assert s.sent[0]["turn_complete"] is True
    parts = s.sent[0]["turns"]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
    assert parts[1]["text"] == "read this"


def test_sending_clears_the_frame_and_the_busy_flag():
    m = session()
    asyncio.run(m._look(FakeFC(), {}))
    asyncio.run(m._send_pending_vision(FakeSession()))
    assert m._pending_vision is None
    assert not m._vision_busy


def test_sending_twice_does_not_send_the_frame_twice():
    m = session()
    asyncio.run(m._look(FakeFC(), {}))
    s = FakeSession()
    asyncio.run(m._send_pending_vision(s))
    asyncio.run(m._send_pending_vision(s))
    assert len(s.sent) == 1


def test_a_send_that_fails_tells_the_model_rather_than_going_quiet():
    """NOVA has already said out loud that she is looking. Failing silently
    leaves that sentence as the whole answer."""
    m = session()
    asyncio.run(m._look(FakeFC(), {}))
    asyncio.run(m._send_pending_vision(FakeSession(fail=True)))
    assert not m._vision_busy
    assert events(m, "vision_failed")


# ── the surfaces are told ────────────────────────────────────────────────────

def test_looking_at_the_screen_is_visible_in_the_activity_stream():
    """Reading the user's screen is the most sensitive thing NOVA does. It is
    never something that happens without the interface showing it."""
    m = session()
    asyncio.run(m._look(FakeFC(), {}))
    asyncio.run(m._send_pending_vision(FakeSession()))
    kinds = [e.type for e in m._published]
    assert kinds == ["vision_capture", "vision_captured", "vision_sent"]


def test_no_screenshot_is_written_to_disk(tmp_path, monkeypatch):
    """Core's vision tool saves the frame before analysing it. This one holds
    it in memory and lets it go.

    Asserted by watching the filesystem rather than by reading the source,
    because the encoder does call `Image.save` — into a BytesIO, which is
    memory — and a grep would either miss the real thing or trip over that.
    """
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))

    m = session()
    asyncio.run(m._look(FakeFC(), {"question": "read this"}))
    asyncio.run(m._send_pending_vision(FakeSession()))

    assert set(tmp_path.rglob("*")) == before, "a picture of the screen was left behind"


# ── and the receiver actually does it ────────────────────────────────────────

class _Msg:
    tool_call = None
    server_content = None
    go_away = None

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _SC:
    model_turn = None
    input_transcription = None
    output_transcription = None
    interrupted = False
    turn_complete = False

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TurnSession(FakeSession):
    """Yields one turn that ends, then stops the receiver."""

    def __init__(self):
        super().__init__()
        self._turns = 0

    def receive(self):
        async def gen():
            self._turns += 1
            if self._turns > 1:
                raise asyncio.CancelledError
            yield _Msg(server_content=_SC(turn_complete=True))
        return gen()


def test_the_end_of_the_acknowledgement_is_what_sends_the_frame():
    """A turn cannot be interleaved: content pushed while the model is still
    speaking either gets ignored or cuts the acknowledgement off mid-word. So
    the frame waits for turn_complete, and this is that wiring."""
    m = session()
    m._turn_done_flag = False
    m._turn_count = 0
    m._audio_bytes_out = 0
    m._trace = None
    m._play_generation = 0

    asyncio.run(m._look(FakeFC(), {"question": "read this"}))
    s = TurnSession()
    asyncio.run(m._receiver(s))

    assert len(s.sent) == 1, "the turn ended and NOVA never looked"
    assert s.sent[0]["turn_complete"] is True
    assert m._pending_vision is None


# ── and they reach the surfaces ──────────────────────────────────────────────

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_vision_event_is_forwarded_to_the_shared_event_bus():
    """The ambient orb and the telemetry panel read that bus rather than
    opening their own voice socket. An event missing from the forward list is
    an event those surfaces never see."""
    src = (ROOT / "desk" / "live_session.py").read_text(encoding="utf-8")
    published = set(re.findall(r'LiveEvent\("(vision_[a-z_]+)"', src))
    assert published, "no vision events are published at all"

    bridge = (ROOT / "desk" / "bridge.py").read_text(encoding="utf-8")
    block = bridge.split('ev_dict.get("type") in (', 1)[1].split("):", 1)[0]
    forwarded = set(re.findall(r'"(vision_[a-z_]+)"', block))
    assert not published - forwarded, (
        f"published but never forwarded: {sorted(published - forwarded)}")


def test_the_window_shows_that_NOVA_is_reading_the_screen():
    app = (ROOT / "desk" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'case "vision_capture":' in app
    body = app.split('case "vision_capture":', 1)[1].split("break;", 1)[0]
    assert "vision-active" in body, "nothing marks the screen as being read"


def test_the_ring_that_marks_it_is_actually_styled():
    """A class with no rule behind it is an interface that says nothing."""
    css = (ROOT / "desk" / "static" / "hud.css").read_text(encoding="utf-8")
    assert "body.vision-active .orb-stage::after" in css


def test_the_ambient_orb_shows_it_too():
    """Ambient mode is where the orb is the entire interface, so it is the
    surface on which an invisible screen read would matter most."""
    hud = (ROOT / "desk" / "static" / "hud.js").read_text(encoding="utf-8")
    assert '"vision_capture"' in hud


# ── routing, which is the difference between 1 s and 30 s ────────────────────

def test_reading_text_is_answered_in_session():
    """"Read my screen" is how people ask, and it was the case that went wrong.

    ocr_only used to route to Core, which meant a second model over REST with
    the conversation blocked behind it. The live model reads a screenshot
    perfectly well.
    """
    assert session()._vision_in_session({"ocr_only": True,
                                         "question": "read this error"})


def test_the_reading_intent_survives_into_the_question():
    m = session()
    asyncio.run(m._look(FakeFC(), {"ocr_only": True,
                                             "question": "what does it say?"}))
    asked = m._pending_vision[2].lower()
    assert "read" in asked and "text" in asked
    assert "what does it say?" in asked


def test_a_stale_busy_flag_does_not_lock_the_fast_path_out():
    """A capture whose turn never completed must not cost the whole session.

    If the acknowledging turn is interrupted or the socket drops, the flag is
    never cleared — and every later request silently falls back to the
    blocking path for as long as the app runs.
    """
    m = session()
    m._vision_busy = True
    m._vision_busy_at = time.monotonic() - ls.LiveManager.VISION_BUSY_MAX_S - 1
    assert m._vision_in_session({}), "a stale flag still blocks looking"
    assert not m._vision_busy


def test_a_fresh_capture_in_flight_is_still_respected():
    m = session()
    m._vision_busy = True
    m._vision_busy_at = time.monotonic()
    assert not m._vision_in_session({})


def test_looking_is_bounded_more_tightly_than_other_tools():
    """A web search nobody is listening to can take thirty seconds. A question
    the user asked out loud and is waiting in silence for cannot."""
    assert ls.VISION_TIMEOUT_S < ls.TOOL_TIMEOUT_S


def test_a_tool_timeout_tells_the_model_not_to_retry():
    """Four vision calls back to back, thirty seconds each, two minutes of
    silence. The old wording invited exactly that."""
    import inspect
    src = inspect.getsource(ls.LiveManager._run_tool)
    assert "Do NOT" in src and "call it again" in src


# ── the camera, which was the slow one ───────────────────────────────────────

def test_the_camera_is_captured_through_core_not_re_implemented():
    """Core owns the webcam: index, warm-up frames and compression are settled
    there, and a second answer to "which camera" is a bug waiting to happen."""
    import inspect
    src = inspect.getsource(ls.LiveManager._capture_for)
    assert "_capture_camera" in src


def test_a_camera_look_asks_about_the_camera():
    m = session()
    captured = {}
    import types
    ls.LiveManager._capture_for = staticmethod(
        lambda source: (captured.setdefault("source", source), (b"x" * 4000, "image/jpeg"))[1])
    try:
        asyncio.run(m._look(FakeFC(), {"angle": "camera"}))
    finally:
        del ls.LiveManager._capture_for
    assert captured["source"] == "camera"
    assert "camera" in m._pending_vision[2].lower()


def test_the_acknowledgement_names_what_it_is_looking_through():
    m = session()
    ls.LiveManager._capture_for = staticmethod(lambda s: (b"x" * 4000, "image/jpeg"))
    try:
        resp = asyncio.run(m._look(FakeFC(), {"angle": "camera"}))
    finally:
        del ls.LiveManager._capture_for
    assert "camera" in resp.response["output"].lower()


# ── the storm ────────────────────────────────────────────────────────────────

class _FC:
    def __init__(self, i="c1", **kw):
        self.id = i
        self.name = "vision"
        self.args = kw


def storm_session():
    m = session()
    m._vision_last_at = 0.0
    return m


def test_a_second_look_within_the_cooldown_is_refused_instantly():
    """Twenty-plus vision calls back to back, each Core-routed one costing
    10.7s, is minutes of conversation spent photographing an unchanged
    screen. A refusal that returns at once is worth more than a correct
    answer four captures later."""
    m = storm_session()
    assert m._vision_too_soon(_FC()) is None          # the first look is fine
    refusal = m._vision_too_soon(_FC("c2"))
    assert refusal is not None
    assert "do not call vision again" in refusal.response["output"].lower()


def test_the_refusal_says_the_image_is_already_there():
    """Because it is. The model was sent one seconds ago and is asking again
    instead of answering from it."""
    m = storm_session()
    m._vision_too_soon(_FC())
    said = m._vision_too_soon(_FC("c2")).response["output"].lower()
    assert "already been sent" in said


def test_a_genuine_follow_up_after_the_cooldown_is_allowed():
    m = storm_session()
    m._vision_too_soon(_FC())
    m._vision_last_at = time.monotonic() - ls.LiveManager.VISION_COOLDOWN_S - 0.1
    assert m._vision_too_soon(_FC("c2")) is None


def test_the_cooldown_applies_whichever_path_the_call_would_take():
    """The storm is not about which path a call takes. It is about there
    being twenty of them."""
    import inspect
    src = inspect.getsource(ls.LiveManager._run_tool)
    assert src.index("_vision_too_soon") < src.index("_vision_in_session")


def test_counting_faces_on_a_screenshot_does_not_go_to_core():
    """detect_faces sent every call to Core, and the model sets it freely:
    "what is on my screen?" arrived with detect_faces=True and bought a 10.7s
    REST round trip to count faces on a desktop."""
    assert session()._vision_in_session(
        {"angle": "screen", "detect_faces": True, "question": "what is on my screen?"})


def test_counting_faces_on_the_camera_still_goes_to_core():
    """There it is the actual question, and Core runs the local model."""
    assert not session()._vision_in_session(
        {"angle": "camera", "detect_faces": True, "question": "how many people?"})
