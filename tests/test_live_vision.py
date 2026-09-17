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


def test_the_camera_still_belongs_to_core():
    """mss grabs displays. The webcam is a different device and a different
    pipeline, and pretending otherwise would capture the screen and call it a
    photo of the user."""
    assert not session()._vision_in_session({"angle": "camera"})


@pytest.mark.parametrize("arg", ls.LiveManager._VISION_NEEDS_CORE)
def test_work_that_needs_core_still_goes_to_core(arg):
    """OCR and face work run local models; saving writes a file. None of that
    is something a picture dropped into a conversation can do."""
    assert not session()._vision_in_session({arg: True})


def test_a_second_look_is_refused_while_the_first_is_in_flight():
    """Otherwise two screenshots stack and NOVA describes the screen twice."""
    m = session()
    m._vision_busy = True
    assert not m._vision_in_session({})


# ── capturing ────────────────────────────────────────────────────────────────

def test_capturing_returns_at_once_and_asks_NOVA_to_say_so():
    """The silence is the bug. A tool call blocks the turn, so the answer has
    to be "I'm looking", said immediately, not the description."""
    m = session()
    resp = asyncio.run(m._look_at_screen(FakeFC(), {"question": "read this"}))
    out = resp.response["output"].lower()
    assert "say" in out and "looking" in out
    assert "do not describe" in out, (
        "nothing stops the model inventing a description before it has seen "
        "anything")


def test_capturing_holds_the_frame_for_the_end_of_the_turn():
    m = session()
    asyncio.run(m._look_at_screen(FakeFC(), {"question": "read this"}))
    assert m._pending_vision is not None
    jpeg, mime, question = m._pending_vision
    assert mime == "image/jpeg"
    assert len(jpeg) > 1000, "that is not a screenshot"
    assert question == "read this"
    assert m._vision_busy


def test_the_users_question_travels_with_the_frame():
    """The model has to answer what was asked, not describe the desktop."""
    m = session()
    asyncio.run(m._look_at_screen(FakeFC(), {"question": "what error is this?"}))
    assert m._pending_vision[2] == "what error is this?"


def test_a_missing_question_still_asks_something():
    m = session()
    asyncio.run(m._look_at_screen(FakeFC(), {}))
    assert m._pending_vision[2].strip()


def test_a_capture_that_fails_says_so_and_does_not_stay_busy(monkeypatch):
    """A stuck busy flag is a NOVA who will not look at anything again."""
    m = session()
    monkeypatch.setattr(ls, "capture_once",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no mss")))
    resp = asyncio.run(m._look_at_screen(FakeFC(), {}))
    assert "could not capture" in resp.response["output"].lower()
    assert m._pending_vision is None
    assert not m._vision_busy
    assert events(m, "vision_failed")


# ── showing the model ────────────────────────────────────────────────────────

def test_the_frame_is_sent_as_its_own_turn():
    """Unlike an ambient frame, this one is the question. It closes the turn
    so the model answers instead of filing the picture away as context."""
    m = session()
    asyncio.run(m._look_at_screen(FakeFC(), {"question": "read this"}))
    s = FakeSession()
    asyncio.run(m._send_pending_vision(s))

    assert len(s.sent) == 1
    assert s.sent[0]["turn_complete"] is True
    parts = s.sent[0]["turns"]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
    assert parts[1]["text"] == "read this"


def test_sending_clears_the_frame_and_the_busy_flag():
    m = session()
    asyncio.run(m._look_at_screen(FakeFC(), {}))
    asyncio.run(m._send_pending_vision(FakeSession()))
    assert m._pending_vision is None
    assert not m._vision_busy


def test_sending_twice_does_not_send_the_frame_twice():
    m = session()
    asyncio.run(m._look_at_screen(FakeFC(), {}))
    s = FakeSession()
    asyncio.run(m._send_pending_vision(s))
    asyncio.run(m._send_pending_vision(s))
    assert len(s.sent) == 1


def test_a_send_that_fails_tells_the_model_rather_than_going_quiet():
    """NOVA has already said out loud that she is looking. Failing silently
    leaves that sentence as the whole answer."""
    m = session()
    asyncio.run(m._look_at_screen(FakeFC(), {}))
    asyncio.run(m._send_pending_vision(FakeSession(fail=True)))
    assert not m._vision_busy
    assert events(m, "vision_failed")


# ── the surfaces are told ────────────────────────────────────────────────────

def test_looking_at_the_screen_is_visible_in_the_activity_stream():
    """Reading the user's screen is the most sensitive thing NOVA does. It is
    never something that happens without the interface showing it."""
    m = session()
    asyncio.run(m._look_at_screen(FakeFC(), {}))
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
    asyncio.run(m._look_at_screen(FakeFC(), {"question": "read this"}))
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

    asyncio.run(m._look_at_screen(FakeFC(), {"question": "read this"}))
    s = TurnSession()
    asyncio.run(m._receiver(s))

    assert len(s.sent) == 1, "the turn ended and NOVA never looked"
    assert s.sent[0]["turn_complete"] is True
    assert m._pending_vision is None
