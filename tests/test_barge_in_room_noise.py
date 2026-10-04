"""Room noise is not the user talking.

Observed 2026-09-24 11:37-11:42: 17 barge-ins in four and a half minutes, the
first during the greeting before the user had said anything; NOVA finished
almost no sentence. Speaker was Bluetooth earbuds, so there was no echo path
and the gate judged frames against the room: "louder than this room has
been". But the room level it compared with, ``_noise_floor``, started at 0
and was only ever learned from frames quieter than the fixed 700 RMS barge-in
floor. This laptop's microphone sits at 1,300-1,700 RMS in an empty room, so
nothing ever qualified, the floor stayed 0, and every frame of room tone was
"speech" -- sixteen of them (1 s) and NOVA stopped herself.

Replaying a real recording from this machine (speech through the earbuds,
nobody talking) reproduced it: 6 false barge-ins in 13 s, noise floor 0,
while the gate's continuously-learned room level sat at 1,669.
"""
from __future__ import annotations

import numpy as np

from nova_voice import VoiceGate

FRAME = 1024
RATE_OUT = 24000


def _noise(rms, n=FRAME, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * rms).astype(np.int16)


def _speech_ref(seconds=0.064):
    t = np.arange(int(RATE_OUT * seconds)) / RATE_OUT
    return (np.sin(2 * np.pi * 220 * t) * 4000).astype(np.int16).tobytes()


def _run(gate, mic_rms, frames, seed=1):
    fired = []
    gate._on_barge_in = lambda: fired.append(1)
    for i in range(frames):
        gate.reference(_speech_ref(), RATE_OUT)     # NOVA is talking...
        gate.process(_noise(mic_rms, seed=seed + i))  # ...into earbuds
        if not gate._speaking:
            gate.set_speaking(True)                 # her next sentence
    return fired


def _gate_in_a_noisy_room(room_rms=1500):
    g = VoiceGate(chunk_samples=FRAME, simple=False)
    for i in range(40):                             # 2.5 s of room before she speaks
        g.process(_noise(room_rms, seed=100 + i))
    g.set_speaking(True)
    return g


def test_steady_room_noise_does_not_interrupt_her():
    g = _gate_in_a_noisy_room(1500)
    fired = _run(g, mic_rms=1500, frames=200)       # ~13 s of her talking
    assert fired == [], f"{len(fired)} false barge-ins from room noise alone"


def test_the_user_speaking_up_still_interrupts_her():
    g = _gate_in_a_noisy_room(1500)
    assert _run(g, mic_rms=1500, frames=60) == []
    fired = _run(g, mic_rms=6000, frames=40, seed=500)   # the user, clearly
    assert fired, "a voice four times the room level no longer interrupts"


def test_a_quiet_room_behaves_as_before():
    g = _gate_in_a_noisy_room(150)
    assert _run(g, mic_rms=150, frames=100) == []
    assert _run(g, mic_rms=3000, frames=40, seed=900)
