"""Cutting NOVA off mid-sentence.

The shipped voice policy is half-duplex — the microphone is muted for exactly
as long as NOVA is speaking — so she cannot hear herself being interrupted.
That is a deliberate trade (see test_simple_voice.py and test_voice_policy.py)
but it cannot mean "wait until she has finished": being unable to stop an
assistant mid-sentence is the difference between a conversation and a recital.

So every surface provides the interruption instead, and they all go through
one path — the same one the automatic detector uses, so a deliberate
interruption and a detected one leave NOVA in identical states.

These tests exercise the real manager with the audio hardware stubbed, not the
source text: the interesting failures here are a queue that was not drained
and a model that was never told, and both read perfectly fine.
"""
from __future__ import annotations

import inspect
import queue as q
import threading

import pytest

import nova_voice
from desk import live_session as ls
import terminal_voice

CHUNK = b"\x10\x00" * 512


# ── a manager with the sound card stubbed out ────────────────────────────────

class FakeStream:
    """A sound device that remembers being aborted."""

    def __init__(self):
        self.aborts = 0

    def abort(self):
        self.aborts += 1

    def start(self):
        pass


def session(speaking=False):
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._play_q = q.Queue(maxsize=200)
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._speaker_alive = True
    m._last_audio_at = 0.0
    m._play_generation = 0
    m._suppress_until = 0.0
    m._suppressed_tail = 0
    m._trace = None
    m._turn_done_flag = False
    m._turn_count = 0
    m._audio_bytes_out = 0
    m._gate = nova_voice.VoiceGate(chunk_samples=1024, on_barge_in=m._barge_in)
    m._stream = FakeStream()
    m._stream_lock = threading.Lock()
    m._playing_until = 0.0
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    # Stand in for the asyncio queue the session loop owns.
    m._to_model = []
    m._post_to_loop = lambda queue_, item: (m._to_model.append(item), True)[1]
    m._input_text_queue = object()
    if speaking:
        m._enqueue_audio(CHUNK)
        m._published.clear()
    return m


def events(m, kind):
    return [e for e in m._published if e.type == kind]


def states(m):
    return [e.data.get("state") for e in m._published if e.type == "state"]


# ── what an interruption actually does ───────────────────────────────────────

def test_the_audio_already_produced_is_thrown_away():
    """Draining our own queue is not stopping.

    Everything handed to the sound device lives in a buffer several hundred
    milliseconds deep, which is how NOVA calmly finished her sentence while
    the interface said "listening".
    """
    m = session(speaking=True)
    for _ in range(20):
        m._enqueue_audio(CHUNK)
    assert m._play_q.qsize() > 0

    m.barge_in()

    assert m._play_q.qsize() == 0, "queued audio survived the interruption"
    assert m._stream.aborts == 1, (
        "the sound device was politely asked to finish rather than aborted, "
        "so NOVA plays out the rest of the sentence from its buffer")


def test_the_pre_roll_is_invalidated_too():
    """The speaker thread holds audio of its own; draining the queue alone
    leaves it to play a fragment of the old answer after the cut."""
    m = session(speaking=True)
    before = m._play_generation
    m.barge_in()
    assert m._play_generation > before


def test_she_stops_being_in_the_speaking_state():
    m = session(speaking=True)
    assert m.speaking
    m.barge_in()
    assert not m.speaking
    assert states(m)[-1] == "listening"


def test_the_surfaces_are_told_playback_was_cancelled():
    m = session(speaking=True)
    m.barge_in()
    assert events(m, "playback_cancelled"), "no surface can show the cut"
    assert events(m, "interrupted")


def test_the_model_is_told_to_stop_generating():
    """Otherwise it keeps going, and the next thing NOVA says is the middle
    of the answer nobody is listening to any more."""
    m = session(speaking=True)
    m.barge_in()
    assert m._to_model == [ls.INTERRUPT_NOTICE]


def test_the_notice_is_sent_as_a_completed_turn():
    """Because that, and only that, cancels the generation in progress.

    Sending it with `turn_complete=False` was tried, to stop her answering
    "okay" the moment she was asked for silence. It does not cancel anything:
    measured against a real session, the local window suppressed the first
    three seconds and then 45 chunks of the answer that had just been stopped
    played out of it. A completed turn cancels in about two tenths of a
    second, which is why the notice is worth sending at all.
    """
    src = inspect.getsource(ls.LiveManager._text_sender)
    body = src.split('"""')[-1]                  # skip the docstring
    assert "turn_complete=True" in body
    assert "turn_complete=False" not in body


def test_the_notice_does_not_ask_her_a_question():
    """It is still a stage direction. Being a turn means she may answer it,
    so the wording has to give her as little as possible to answer."""
    assert "?" not in ls.INTERRUPT_NOTICE
    assert "say nothing" in ls.INTERRUPT_NOTICE.lower()


def test_words_of_the_users_own_replace_the_notice():
    """When the user has something to say, their words are what stops the
    model. A synthetic notice as well and NOVA answers twice: an
    acknowledgement of the interruption, then the real answer."""
    m = session(speaking=True)
    m.barge_in(notify_model=False)
    assert m._to_model == []
    assert not m.speaking, "the audio must still be dropped"


def test_stopping_a_silent_nova_is_a_no_op():
    m = session(speaking=False)
    assert m.barge_in()["message"] == "not speaking"
    assert m._to_model == []
    assert m._stream.aborts == 0


def test_a_deliberate_interruption_lands_where_a_detected_one_does():
    """Two ways to cancel speech is two sets of edge cases."""
    deliberate = session(speaking=True)
    deliberate.barge_in()

    detected = session(speaking=True)
    detected._barge_in()          # what the detector calls

    def shape(m):
        return (m.speaking, m._play_q.qsize(), m._stream.aborts,
                m._turn_done_flag, sorted({e.type for e in m._published}))

    assert shape(deliberate) == shape(detected)


def test_the_rest_of_the_sentence_is_thrown_away_as_it_arrives():
    """The half of the job that dropping the queue does not do.

    Measured against a real Gemini Live session: after barge_in() the model
    went on sending for 2.52 s — 25 more chunks — because the cut has to cross
    the network and the audio already generated is in flight behind it. All
    25 were played. The room went quiet and then NOVA carried on talking.
    """
    m = session(speaking=True)
    m.barge_in()
    for _ in range(25):
        m._enqueue_audio(CHUNK)
    assert m._play_q.qsize() == 0, (
        "%d chunks of the interrupted answer were played after the cut"
        % m._play_q.qsize())
    assert not m.speaking, "the tail put her back into the speaking state"


def test_cutting_in_on_the_tail_end_does_not_swallow_the_next_answer():
    """The user cuts in just as she finishes, so the model has already sent
    turn_complete and will never send `interrupted`. Nothing more of that turn
    is coming, so there is nothing to discard — and discarding anyway would
    eat the opening of the next answer."""
    m = session(speaking=True)
    m._turn_done_flag = True              # turn_complete already arrived
    m.barge_in()
    m._enqueue_audio(CHUNK)
    assert m._play_q.qsize() == 1, "the next answer began inaudibly"


def test_a_second_session_does_not_inherit_the_first_ones_finished_turn():
    """Found by running two sessions, not one.

    `_turn_done_flag` is set when the model says a turn is over and cleared
    when playback of that turn drains. Closing NOVA mid-sentence stops the
    session between those two, leaving it set — and it was never reset on
    connect, so session two began believing a turn it had not started had
    already finished. The tail window is deliberately not armed for a turn
    the model has finished, so on the second session an interruption played
    21 chunks of the sentence it was supposed to stop.
    """
    src = inspect.getsource(ls.LiveManager._connect_and_run)
    setup = src.split("self._mic_queue = asyncio.Queue")[0]
    assert "self._turn_done_flag = False" in setup, (
        "a connect that inherits the last session's turn state; the second "
        "conversation of the day behaves differently from the first")
    assert "self._suppress_until = 0.0" in setup


# ── the terminal ─────────────────────────────────────────────────────────────

class FakeManager:
    def __init__(self, speaking=True):
        self._speaking = speaking
        self.barges = []
        self.sent = []

    @property
    def speaking(self):
        return self._speaking

    def barge_in(self, *, notify_model=True):
        self.barges.append(notify_model)
        self._speaking = False
        return {"ok": True}

    def send_text(self, text):
        self.sent.append(text)
        return {"ok": True}


class FakeStdin:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) + "\n" if self._lines else ""


def terminal(speaking=True):
    tv = terminal_voice.TerminalVoice.__new__(terminal_voice.TerminalVoice)
    tv._mgr = FakeManager(speaking)
    tv._ready = threading.Event()
    tv._ready.set()
    tv._pending = []
    tv._pending_lock = threading.Lock()
    tv._stop = threading.Event()
    return tv


def typed(tv, lines, monkeypatch):
    """Feed the real stdin reader some lines and let it run to the end."""
    monkeypatch.setattr(terminal_voice.sys, "stdin", FakeStdin(lines))
    tv._read_typed()
    return tv._mgr


def test_a_bare_enter_stops_her(monkeypatch):
    """The quickest thing a hand can do is the one bound to "be quiet"."""
    mgr = typed(terminal(speaking=True), [""], monkeypatch)
    assert mgr.barges == [True]
    assert mgr.sent == []


def test_saying_stop_stops_her(monkeypatch):
    for word in ("stop", "Stop", "shh", "wait"):
        mgr = typed(terminal(speaking=True), [word], monkeypatch)
        assert mgr.barges == [True], word
        assert mgr.sent == [], f"{word!r} was sent to the model as a question"


def test_typing_while_she_talks_interrupts_and_still_asks(monkeypatch):
    """Waiting for her to finish before the question is even accepted is what
    makes an assistant feel like a form rather than a conversation."""
    mgr = typed(terminal(speaking=True), ["what is the time"], monkeypatch)
    assert mgr.barges == [False], "the model was told twice"
    assert mgr.sent == ["what is the time"]


def test_typing_while_she_is_quiet_just_asks(monkeypatch):
    mgr = typed(terminal(speaking=False), ["hello"], monkeypatch)
    assert mgr.barges == []
    assert mgr.sent == ["hello"]


def test_quitting_still_quits(monkeypatch):
    tv = terminal(speaking=True)
    typed(tv, ["quit", "not reached"], monkeypatch)
    assert tv._stop.is_set()
    assert tv._mgr.sent == []


# ── the HTTP surface the desktop window uses ─────────────────────────────────

TOKEN = "test-desk-token"


class BridgeManager:
    def __init__(self):
        self.calls = []

    def barge_in(self, *, notify_model=True):
        self.calls.append(notify_model)
        return {"ok": True, "message": "interrupted"}


@pytest.fixture()
def client(monkeypatch):
    """The bridge's own app, so the route and its real token guard are the
    ones under test rather than a reconstruction of them."""
    import desk.bridge as bridge
    mgr = BridgeManager()
    monkeypatch.setattr(bridge.desk_live, "get_live_manager", lambda: mgr)
    monkeypatch.setattr(bridge, "run_token", TOKEN)
    bridge.app.config["TESTING"] = True
    with bridge.app.test_client() as c:
        c.mgr = mgr
        yield c


AUTH = {"X-NOVA-Desk": TOKEN, "Content-Type": "application/json"}


def test_the_window_can_stop_her(client):
    r = client.post("/api/live/interrupt", headers=AUTH, data="{}")
    assert r.status_code == 200 and r.get_json()["ok"]
    assert client.mgr.calls == [True]


def test_the_window_can_stop_her_quietly_when_words_follow(client):
    r = client.post("/api/live/interrupt", headers=AUTH,
                    data='{"notify_model": false}')
    assert r.status_code == 200
    assert client.mgr.calls == [False]


def test_an_empty_body_is_not_an_error(client):
    """The surface fires this on a keystroke; it must never need ceremony."""
    r = client.post("/api/live/interrupt", headers={"X-NOVA-Desk": TOKEN})
    assert r.status_code == 200


def test_interrupting_needs_the_token(client):
    r = client.post("/api/live/interrupt", data="{}")
    assert r.status_code == 401
    assert client.mgr.calls == []
