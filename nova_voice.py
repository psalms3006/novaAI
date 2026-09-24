"""nova_voice — the one voice behaviour model for NOVA.

There is one NOVA. Every surface (the desktop window, the ambient orb, the
terminal) is an adapter around the same conversational behaviour, and this
module is that behaviour: everything about *how NOVA listens* that must be
identical everywhere.

It deliberately owns no transport. It knows nothing about Gemini, WebSockets,
Flask or pywebview — callers feed it mic frames, the audio they hand to the
speaker, and NOVA's speaking state; it answers "what should I transmit" and
"did the user just interrupt".

Why this is not a plain loudness gate
-------------------------------------
The previous policy interrupted NOVA whenever a mic frame exceeded a fixed
RMS of 350 for two consecutive frames. On any machine with speakers — which
is every machine — NOVA's own voice re-enters the microphone. Measured against
real Gemini Live output (median frame RMS 2398, peak 9157), even 10 % acoustic
coupling puts 37 % of frames over that threshold. NOVA therefore interrupted
*herself* within a few hundred milliseconds of starting to speak, every time.
The recorded symptom was a greeting that played three chunks and stopped.

The fix is a double-talk detector built on echo cancellation. We know exactly
what we sent to the speaker, so :class:`EchoCanceller` subtracts it from what
the microphone hears and reports what is left. NOVA's own voice cancels; a
person talking does not. Nothing is assumed about volume, speakers or room —
the coupling is solved for on every frame.

Policy summary
--------------
  * While NOVA speaks the mic is muted: silence is *sent*, not withheld, so
    the model's turn detection sees a continuous stream.
  * A short cooldown after speech ends keeps the decaying tail of NOVA's own
    audio out of the microphone.
  * Barge-in requires a residual — sound the speaker cannot account for —
    that stands clear of the residual floor, sustained over several frames.
    Failing safe means failing to interrupt, never interrupting at random.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import numpy as np

# ── Policy constants ─────────────────────────────────────────────────────────

#: Absolute floor on the post-cancellation residual. Nothing quieter than
#: this is ever treated as speech. Room tone on a laptop mic sits near 40 and
#: a person talking at the machine measures in the thousands, so this rejects
#: keyboard noise and breath without being anywhere near real speech.
BARGE_IN_FLOOR_RMS = 700.0

#: How far a real interruption must stand above the leakage NOVA is already
#: producing. A single-tap canceller cannot model reverberation, so in a live
#: room some of her voice survives cancellation; requiring 2x (~6 dB) over
#: whatever is actually surviving adapts to that instead of assuming it away.
BARGE_IN_ECHO_MARGIN = 2.0

#: Per-frame decay of the learned leakage ratio, so a value picked up from one
#: awkward moment relaxes again (~64 ms/frame; halves in ~0.4 s). Slower than
#: this and one badly-cancelled syllable leaves NOVA uninterruptible for over
#: a second afterwards.
RESIDUAL_FLOOR_DECAY = 0.90

#: How long a reference stays usable after the last audio was handed to the
#: sound device. Past this, playback has stopped and there is nothing left to
#: cancel — see EchoCanceller.armed.
REF_STALE_S = 0.5

#: Aligned playback level below which the speaker counts as quiet.
REF_ACTIVE_RMS = 150.0

#: How long after the reference goes quiet the room is still ringing with it.
#:
#: The reference is recorded when audio is handed to the sound device, but the
#: device plays it out a few hundred milliseconds later. At the end of a turn
#: that leaves a window where NOVA's last words are physically in the air with
#: no reference left to cancel them against — the residual is then all of her
#: voice, the ratio explodes, and she "hears someone talking". Observed first
#: on a greeting, where the model transcribed her own closing words, "the
#: hand", back as something the user had said.
#:
#: Measured rather than guessed: with the guard at 0.5 s, false barge-ins
#: still landed 0.57 s after the last reference write, because the output
#: buffer holds roughly 300 ms and the acoustic path adds more. 1.2 s clears
#: that with margin. It costs nothing in practice — the gate is only consulted
#: while NOVA is speaking, and once she stops it is not consulted at all.
REF_TAIL_S = 1.2

#: Frames of NOVA's speech to allow the canceller to lock onto the echo
#: before concluding there is no echo to lock onto.
#:
#: Requiring convergence indefinitely was a bug with teeth: on a machine whose
#: speakers do not reach its microphone at all — headphones, a distant
#: speaker, a quiet room — the canceller can *never* converge, because there
#: is nothing to converge on. NOVA was then uninterruptible for the entire
#: session. Reproduced directly: room tone at RMS 700 with no echo path, user
#: talking over her for four seconds, forty-three frames suppressed, barge-in
#: never fired.
ECHO_LOCK_GRACE_FRAMES = 12

#: How far above the room's own noise a voice must stand when there is no
#: echo path to cancel. The ratio test is meaningless there — it compares the
#: microphone against a playback level that never reaches the microphone — so
#: the fallback is the classic one: louder than the room has been.
NOISE_MARGIN = 2.0

#: Leakage assumed at the start of a turn, before anything has been measured.
#:
#: Not 1.0. Assuming the room leaks *all* of NOVA's voice back is so
#: pessimistic that the threshold is still above a real interruption several
#: frames later — measured, it cost the whole first word: the user says
#: "NOVA," at RMS 6349 and the gate declined it, catching only the phrase
#: after the pause, 1.2 s in. Measured echo leakage sits between 0.10 and
#: 0.35 across the coupling sweep, so starting at the top of the learnable
#: range is still conservative and converges in a third of the time. The
#: warm-up frames, not this value, are what protect the start of a turn.
INITIAL_LEAK_RATIO = 0.5

#: The learned leakage floor only moves up on frames comfortably *below* the
#: decision threshold, not merely under it. Without that gap the floor chases
#: the user's own voice: each frame of their speech is a little under the
#: current threshold, so it is learned as echo, which raises the threshold
#: just above their next frame, forever. Measured at 25 % coupling, that
#: runaway tracked speech from a ratio of 0.57 up past 1.3 and no
#: interruption was ever possible.
LEAK_LEARN_MARGIN = 1.2

#: A learned floor above this means cancellation has effectively failed. Let
#: it stop there rather than climbing until barge-in is impossible; NOVA is
#: better off occasionally hearing her own tail than never hearing the user.
MAX_LEARNED_LEAK = 0.5

#: ...and it must never fall below this either. Echo-only leakage measured
#: 0.15-0.21 at the ninetieth percentile across the coupling sweep, so a floor
#: under 0.15 is below NOVA's own noise and every loud syllable of hers reads
#: as an interruption.
MIN_LEARNED_LEAK = 0.15

#: Frames of residual averaged before the detector looks at it. Uncancelled
#: reverberation arrives as short spikes; a person talking sustains. Measured
#: over the fixtures at 25 % coupling, averaging five frames (320 ms) drops
#: the worst echo spike from 1729 to 1015 while leaving real speech at 4453 —
#: the difference between a usable margin and an overlapping one.
RESIDUAL_SMOOTHING_FRAMES = 5

#: Smoothed frames above threshold before we believe it. The smoothing window
#: already demands 320 ms of evidence, so two is enough to reject a single
#: transient without making the user repeat themselves.
BARGE_IN_CHUNKS = 2

#: Sustained frames required to call it an interruption when there is *no*
#: echo path — headphones, or a speaker the microphone cannot hear.
#:
#: With an echo path the canceller does the discriminating: NOVA's voice
#: subtracts out and whatever survives is the room. On headphones there is
#: nothing to subtract, so the only question left is "is this the user or the
#: room?", and two frames cannot answer it. The room wins that argument
#: constantly: measured against this machine's own recorded room tone, with
#: nobody speaking at all, the gate fired eight times a minute — NOVA cutting
#: her own sentence off every few seconds, which is indistinguishable from
#: being broken.
#:
#: Swept against that recording rather than guessed. False cut-offs per 15 s
#: of silence, and detections when a voice is mixed in:
#:
#:      6 frames -> 1 false, 13 real       12 frames -> 1 false,  9 real
#:      8 frames -> 1 false, 11 real       16 frames -> 0 false,  8 real
#:     10 frames -> 1 false, 10 real       20 frames -> 0 false,  7 real
#:
#: Sixteen frames is just over a second. That is how long someone has to keep
#: talking before NOVA yields, which is a real cost — but the error it
#: replaces is her interrupting herself, and only one of those two is
#: survivable in a conversation.
NO_ECHO_BARGE_IN_CHUNKS = 16

#: Ignore the mic this long after NOVA stops speaking, so the speaker's
#: decaying tail is not mistaken for the user starting a turn.
SPEAK_COOLDOWN_S = 0.25


def simple_voice_default() -> bool:
    """Is NOVA running the plain half-duplex voice policy?

    True by default, deliberately.

    Everything the full policy adds — echo cancellation, automatic barge-in,
    a learned room floor — sits inside the PortAudio input callback, and that
    callback has a hard real-time budget. The correlation search alone scans
    ~9600 offsets per frame. On this hardware the operating system was
    dropping microphone blocks before NOVA ever saw them ("input overflow"),
    and a quarter of what survived was discarded because the send queue could
    not keep up behind it. Audio that is never transmitted cannot be
    understood, which is why "NOVA cannot hear me" and "NOVA crackles" were
    the same bug.

    So the default is the simple thing that works: while NOVA speaks the
    microphone is muted, and the rest of the time every frame goes straight
    out untouched. That is what the shipped reference implementation does,
    and it costs an interruption button instead of interrupting by voice.

    Set NOVA_VOICE_FULL_DUPLEX=1 to get the full policy back. It is not
    deleted and its tests still run against it; it is waiting for the basics
    to be solid underneath it.
    """
    import os
    flag = os.getenv("NOVA_VOICE_FULL_DUPLEX", "").strip().lower()
    return flag not in ("1", "true", "yes", "on")

SEND_RATE = 16000           # mic -> model
RECEIVE_RATE = 24000        # model -> speaker
CHANNELS = 1



#: How far above the learned room floor a frame must be to count as sound
#: worth transmitting. Generous, because the cost of getting this wrong in one
#: direction is wasted bandwidth and in the other is not hearing someone.
QUIET_FLOOR_MARGIN = 2.2

#: An absolute floor as well, so a perfectly silent digital input (a virtual
#: cable, a muted device) cannot learn a floor of zero and call its own hiss
#: speech.
QUIET_FLOOR_MIN = 120.0

#: Keep transmitting for this long after the last sound. Covers the gap
#: between words, a breath mid-sentence, and the pause before someone changes
#: their mind -- none of which should truncate a turn.
QUIET_HOLDOVER_S = 1.2

#: Frames (~1 s) of the room heard with NOVA silent before its level is
#: trusted as the no-echo barge-in baseline.
ROOM_FRAMES_TO_TRUST = 16

# ── speech detection ─────────────────────────────────────────────────────────
#
# Loudness cannot tell a voice from a key press, and NOVA was deciding both
# "the user is talking over her" and "this is worth sending to the model" on
# loudness alone: she stopped mid-sentence on a chair creak, and the room's
# noise went up to Gemini, whose own detector then answered it. A speech
# model can tell: measured here, Silero scored 0% of frames as speech for
# room noise, keyboard clicks, a door slam and mains hum, and 68% for speech
# (the rest are the gaps between words), at 0.4 ms per 64 ms frame.

#: Probability at or above which a frame counts as speech.
VOICE_PROB = 0.5
#: Stricter bar for the frames that may interrupt her.
BARGE_IN_VOICE_PROB = 0.6
#: Keep treating it as speech this long after the last voiced frame, so the
#: pauses between words and the tail of a sentence are not cut.
VOICE_HANGOVER_S = 0.6
#: With a speech model: interrupt her once this many of the last
#: VOICE_BARGE_IN_WINDOW frames were the user's voice (~0.4 s of speech within
#: ~0.64 s). Windowed rather than consecutive, because real speech has gaps.
VOICE_BARGE_IN_WINDOW = 10
VOICE_BARGE_IN_NEEDED = 6
#: With a speech model the room margin can be gentler: the model already
#: rules out noise, and a soft-spoken user should still be able to stop her.
NOISE_MARGIN_WITH_VOICE = 1.5
#: A sound this far above the room -- a crash, a slam, a scream. Flagged as an
#: event, never sent to the model and never an interruption: on this laptop's
#: mic, desk bangs and typing alone reach it many times a minute, and telling
#: a *concerning* sound from those needs a sound-classification model.
LOUD_EVENT_FACTOR = 6.0
LOUD_EVENT_MIN_RMS = 9000.0
LOUD_EVENT_HOLD_S = 0.6


def _find_vad_model() -> Optional[str]:
    """Silero VAD, as shipped inside faster-whisper (and bundled with NOVA)."""
    import importlib.util
    import os
    import sys
    name = "silero_vad_v6.onnx"
    candidates = []
    env = os.getenv("NOVA_VAD_MODEL", "").strip()
    if env:
        candidates.append(env)
    try:
        spec = importlib.util.find_spec("faster_whisper")
        for loc in (spec.submodule_search_locations or []) if spec else []:
            candidates.append(os.path.join(loc, "assets", name))
    except Exception:
        pass
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "faster_whisper", "assets", name))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


class SpeechDetector:
    """Streaming speech probability for 16 kHz int16 frames (Silero VAD).

    If the model cannot be loaded, or fails while running, ``available``
    becomes False and callers fall back to their loudness rules -- a broken
    detector must never leave NOVA deaf, which is what scoring everything as
    "not speech" would do.
    """

    CHUNK = 512
    CONTEXT = 64

    def __init__(self, path: Optional[str] = None) -> None:
        self._session = None
        self.available = False
        self.path = path or _find_vad_model()
        if not self.path:
            return
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.log_severity_level = 4
            self._session = ort.InferenceSession(
                self.path, providers=["CPUExecutionProvider"], sess_options=opts)
            self.available = True
        except Exception:
            self._session = None
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(self.CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._last = 0.0

    def probability(self, frame: np.ndarray) -> float:
        """Highest speech probability in this frame (0 when unavailable)."""
        if not self.available:
            return 0.0
        x = np.concatenate([self._pending,
                            np.asarray(frame, dtype=np.float32).reshape(-1) / 32768.0])
        best = None
        try:
            while len(x) >= self.CHUNK:
                chunk, x = x[:self.CHUNK], x[self.CHUNK:]
                inp = np.concatenate([self._ctx, chunk])[None, :]
                out, self._h, self._c = self._session.run(
                    None, {"input": inp, "h": self._h, "c": self._c})
                self._ctx = chunk[-self.CONTEXT:]
                p = float(np.asarray(out).reshape(-1)[0])
                best = p if best is None else max(best, p)
        except Exception:
            self.available = False
            return 0.0
        self._pending = x
        if best is not None:
            self._last = best
        return self._last


def frame_rms(frame: np.ndarray) -> float:
    """RMS amplitude of an int16 frame."""
    if frame is None or len(frame) == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


class EchoCanceller:
    """Works out how much of a mic frame is NOVA's own voice coming back.

    This is a single-tap acoustic echo canceller, and it exists only to make a
    *decision* — it never touches the audio NOVA transmits, which is always the
    raw microphone frame. Its whole job is answering "is this the user, or is
    it us?".

    How it works: the playback path hands us every chunk it writes to the
    speaker, so we hold a short history of exactly what the room just heard.
    For an incoming mic frame we find the reference offset that correlates best
    with it, solve for the scalar gain by least squares, subtract, and measure
    what is left. Echo cancels; anything the speaker did not produce does not.

    Measured on recorded Gemini Live audio at 15 % coupling: mic frames peaking
    at RMS 1374 leave a residual of 42 — the noise floor — which is 30 dB of
    cancellation, while a person talking over the top leaves a residual of
    several thousand. That gap is what makes the decision reliable.

    A single tap cannot model reverberation, so real rooms cancel less well
    than that. :class:`VoiceGate` therefore does not trust a fixed residual
    threshold; it tracks the residual it actually sees during NOVA's own speech
    and requires a real interruption to stand well clear of it.
    """

    #: Longest echo delay we look for: output buffer plus acoustic path.
    #:
    #: 600 ms, not 300. If the real delay exceeds this window the canceller
    #: cannot align at all, the whole echo lands in the residual, and NOVA
    #: interrupts herself mid-sentence — the original bug wearing a different
    #: hat. WASAPI shared mode alone can hold 200-400 ms depending on the
    #: device, so 300 ms had no margin. The cost of doubling it is one extra
    #: millisecond of FFT per 64 ms frame, which is nothing next to being
    #: wrong.
    MAX_LAG_S = 0.60

    #: Correlation above which the locked lag is still considered good. Below
    #: it we re-search, because the device buffer has moved.
    LOCK_QUALITY = 0.30

    #: Correlation that confirms the locked offset is really the echo path.
    #:
    #: Deliberately far above LOCK_QUALITY, and for a different job. 0.30 is
    #: the bar for "keep trying to cancel at this offset", and it has to be
    #: low or a moderately coupled room would never cancel at all. This is the
    #: bar for "there is an echo path here", which grants the two-frame
    #: barge-in and the leak-ratio test, and a wrong answer there is NOVA
    #: stopping for her own voice.
    #:
    #: 0.30 cannot do that job, because the search is over ~9600 offsets and
    #: the best of that many correlations of a signal as self-similar as
    #: speech clears 0.30 by chance regularly. Measured on this machine, with
    #: a driver that suppresses the echo so thoroughly there is nothing to
    #: find: every interruption in a long-talk session was granted an echo
    #: path, at correlations of -0.22, 0.17, -0.21 and -0.13 — spurious
    #: matches re-locking the filter faster than the staleness counter could
    #: retire it.
    #:
    #: Pure echo frames correlate at a median of 0.83 and double-talk frames
    #: at 0.45-0.54, so 0.55 separates a path that is really there from both
    #: the noise and the ambiguous case.
    CONFIRM_CORR = 0.55

    #: How much of the frame subtracting the reference has to actually
    #: remove before we believe there is an echo path here.
    #:
    #: Correlation alone could not answer this. Raising the confirmation bar
    #: to 0.55 cut the spurious locks down but did not end them — over
    #: hundreds of frames the best of ~9600 offsets clears any fixed
    #: correlation sometimes, and one lucky frame renews the lock. So the
    #: confirming test is the thing the downstream branch actually assumes:
    #: that cancelling *works*. A frame the reference genuinely explains gets
    #: quieter when the reference is subtracted from it.
    #:
    #: 0.8 is about 2 dB — a deliberately low bar, because a weakly coupled
    #: room is still a coupled room and this must not disqualify one. What it
    #: does disqualify is the case measured here, where the filter's gain had
    #: decayed to 0.06 and the "cancelled" residual was no smaller than the
    #: microphone, or larger.
    CONFIRM_CANCEL_RATIO = 0.8

    #: Frames of *audible playback* the lock may go unconfirmed before we stop
    #: claiming there is an echo path at all.
    #:
    #: A lock used to be permanent: one frame correlating at 0.30 set the lag,
    #: and `converged` answered True for the rest of the session however badly
    #: the filter tracked afterwards. That is not a small inaccuracy, because
    #: everything downstream branches on it — claiming a path drops the
    #: evidence required for a barge-in from sixteen frames to two, and
    #: switches the test to a leak ratio that only means anything if the
    #: cancellation is real.
    #:
    #: Measured on this machine, whose microphone driver does its own echo
    #: suppression and leaves nothing coherent to lock onto: the canceller
    #: caught one spurious correlation early in the session and then reported
    #: an echo path for the rest of it at correlations of 0.10-0.14 and a
    #: residual *larger* than the reference — a filter adding energy rather
    #: than removing it. Three of five interruptions in one conversation were
    #: NOVA stopping for her own voice, each on the two-frame fast path.
    #:
    #: Only frames where NOVA was actually audible count, because a silent
    #: reference says nothing about the path. Thirty-two of them is about two
    #: seconds of continuous speech — far longer than a real echo path ever
    #: goes unconfirmed, since pure echo frames correlate at a median of 0.83,
    #: and short enough that a machine which cannot cancel falls back to the
    #: conservative policy within one sentence.
    LOCK_STALE_FRAMES = 32

    #: How fast the coupling estimate follows the instantaneous fit, chosen by
    #: how well the reference explains the frame.
    #:
    #: A fixed rate cannot work. Fitting the gain fresh on every frame makes
    #: the canceller subtract the *user* during double-talk — the fit that best
    #: cancels a frame containing speech is the one that removes the speech —
    #: and the barge-in then vanishes. Adapting uniformly slowly instead means
    #: the gain is still near zero when NOVA starts a turn, so her first half
    #: second does not cancel and she interrupts herself.
    #:
    #: Correlation resolves it. Measured over the fixtures: frames that are
    #: pure echo correlate with the reference at a median of 0.83, frames with
    #: the user talking over the top at 0.45-0.54. So converge fast when the
    #: frame is clearly just NOVA, crawl when it is ambiguous, and hold still
    #: when it is not echo at all.
    GAIN_ALPHA_FAST = 0.50      # |corr| >= CONFIDENT: clean echo
    GAIN_ALPHA_SLOW = 0.05      # |corr| >= LOCK_QUALITY: probably echo
    CONFIDENT_CORR = 0.70

    #: Physical bounds on the coupling. Outside these it is not an echo path.
    MAX_GAIN = 2.0

    def __init__(self, rate: int = SEND_RATE, frame: int = 1024):
        self._rate = rate
        self._frame = frame
        self._max_lag = int(rate * self.MAX_LAG_S)
        self._lock = threading.Lock()
        self._ref = np.zeros(0, dtype=np.float32)
        self._locked_lag: int | None = None
        #: Frames of audible playback since the lag was last confirmed.
        self._unconfirmed = 0
        self._gain: float | None = None
        self._pending: tuple | None = None
        self._last_ref_at = 0.0
        self.last_ref_rms = 0.0
        self._nfft = 1
        while self._nfft < self._max_lag + frame:
            self._nfft *= 2
        self.last_corr = 0.0

    # ── reference signal ─────────────────────────────────────────────────

    def reference(self, pcm: bytes, rate: int = RECEIVE_RATE) -> None:
        """Record audio handed to the speaker. Called from the playback path."""
        if not pcm:
            return
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if rate != self._rate:
            # Only the correlation shape matters, so plain decimation is fine.
            idx = (np.arange(int(len(a) * self._rate / rate))
                   * rate / self._rate).astype(np.int64)
            a = a[idx[idx < len(a)]]
        with self._lock:
            keep = self._max_lag + self._frame * 4
            self._ref = (np.concatenate([self._ref, a])[-keep:]
                         if self._ref.size else a[-keep:])
            self._last_ref_at = time.time()

    def reset(self) -> None:
        with self._lock:
            self._ref = np.zeros(0, dtype=np.float32)
            self._locked_lag = None
            self._unconfirmed = 0
            self._gain = None
            self._pending = None
            self._last_ref_at = 0.0
        self.last_corr = 0.0

    @property
    def armed(self) -> bool:
        """Is there enough *recent* playback for cancellation to mean anything?

        Recency is the point. The buffer holds the last fraction of a second
        of audio, but it has no idea time has passed — once NOVA stops
        talking it would go on matching microphone frames against her last
        words indefinitely, reporting a healthy reference level and
        suppressing everything the user said next.
        """
        with self._lock:
            if not self._last_ref_at or time.time() - self._last_ref_at > REF_STALE_S:
                return False
            return self._ref.size >= self._frame * 2

    @property
    def gain(self) -> float:
        with self._lock:
            return self._gain or 0.0

    # ── cancellation ─────────────────────────────────────────────────────

    def residual_rms(self, frame: np.ndarray) -> float:
        """RMS of the mic frame with NOVA's own voice removed.

        This *measures* only; it never adapts. The caller classifies the frame
        from the number returned and then calls :meth:`accept` if it concluded
        the frame was echo. Splitting it that way is not fussiness — adapting
        on the same frame being judged is how the filter learns the user's
        voice and cancels it, which silently deletes every barge-in.

        Returns the frame's own RMS when there is no reference to cancel
        against: with nothing playing, every sound in the room is by definition
        not an echo.
        """
        mic = frame.astype(np.float32)
        with self._lock:
            stale = (not self._last_ref_at
                     or time.time() - self._last_ref_at > REF_STALE_S)
            ref = np.zeros(0, dtype=np.float32) if stale else (
                self._ref.copy() if self._ref.size else self._ref)
            locked = self._locked_lag
            gain = self._gain
        self._pending = None
        self.last_ref_rms = 0.0
        if ref.size < self._frame * 2:
            self.last_corr = 0.0
            return frame_rms(frame)

        search = ref[-(self._max_lag + self._frame):]
        fit = None
        if locked is not None and 0 <= locked <= search.size - self._frame:
            fit = self._fit(mic, search, locked)
        # Re-search whenever the locked offset stops explaining the frame. The
        # device buffer moves and the stream restarts; a lag that was right a
        # minute ago silently stops cancelling anything, which looks exactly
        # like the user talking, forever.
        if fit is None or abs(fit[0]) < self.LOCK_QUALITY:
            found = self._search(mic, search)
            if found is not None:
                alt = self._fit(mic, search, found)
                if alt is not None and (fit is None or abs(alt[0]) > abs(fit[0])):
                    fit, locked = alt, found
        if fit is None:
            self.last_corr = 0.0
            return frame_rms(frame)

        corr, instant, seg = fit
        self.last_corr = corr
        if seg is None:
            # Nothing playing at that offset: whatever this is, it is not echo.
            return frame_rms(frame)

        self.last_ref_rms = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
        if gain is None:
            # Nothing learned yet. Using the instantaneous fit for this one
            # frame is the least-wrong option; accept() will make it stick.
            gain = instant
        residual = mic - gain * seg
        res_rms = float(np.sqrt(np.mean(residual.astype(np.float64) ** 2)))
        # Carry what cancelling this frame achieved, so accept() can ask
        # whether the echo path is real rather than only whether something
        # correlated.
        self._pending = (corr, instant, locked, frame_rms(frame), res_rms)
        return res_rms

    def accept(self) -> None:
        """Fold the last measured frame into the echo model.

        Call this only for frames judged to be NOVA's own voice.
        """
        pending = self._pending
        if pending is None:
            return
        corr, instant, lag, mic_rms, res_rms = pending
        # Did subtracting the reference make this frame quieter, and by enough
        # to mean anything?
        cancelled = mic_rms > 1.0 and res_rms <= mic_rms * self.CONFIRM_CANCEL_RATIO
        with self._lock:
            if self._gain is None:
                self._gain = instant
            else:
                alpha = (self.GAIN_ALPHA_FAST if abs(corr) >= self.CONFIDENT_CORR
                         else self.GAIN_ALPHA_SLOW if abs(corr) >= self.LOCK_QUALITY
                         else 0.0)
                self._gain = (1.0 - alpha) * self._gain + alpha * instant
            if abs(corr) >= self.LOCK_QUALITY:
                # Good enough to go on cancelling at this offset...
                self._locked_lag = lag
            if abs(corr) >= self.CONFIRM_CORR and cancelled:
                # ...but the path is only real if cancelling it works.
                self._unconfirmed = 0
            else:
                # NOVA was audible on this frame and the reference still did
                # not explain it. A few of those are ordinary; a run of them
                # means whatever is locked onto is not the echo path, and
                # going on claiming one is how she ends up judging her own
                # voice by a ratio that assumes it has been cancelled.
                self._unconfirmed += 1
                if self._unconfirmed > self.LOCK_STALE_FRAMES:
                    self._locked_lag = None
        self._pending = None

    @property
    def converged(self) -> bool:
        """Is there an echo path right now that a frame can be judged by?

        Not "was one ever found". Until this is true a loud frame proves
        nothing — the filter is not cancelling, so NOVA's own voice is still
        sitting in the residual — and that is equally true of a lock that has
        stopped tracking as of one that never happened. Losing it puts NOVA
        back on the conservative no-echo policy, which is the right place to
        be on a machine where cancellation does not work.
        """
        with self._lock:
            return self._gain is not None and self._locked_lag is not None

    def _fit(self, mic, search, lag):
        """(correlation, least-squares gain, reference segment) at one offset."""
        seg = search[lag:lag + self._frame]
        if seg.size != self._frame:
            return None
        energy = float(np.dot(seg, seg))
        if energy < 1.0:
            return (0.0, 0.0, None)
        cross = float(np.dot(mic, seg))
        mic_energy = float(np.dot(mic, mic))
        corr = (cross / (energy ** 0.5 * mic_energy ** 0.5)) if mic_energy > 0 else 0.0
        return (corr, float(np.clip(cross / energy, 0.0, self.MAX_GAIN)), seg)

    def _search(self, mic, search):
        """Best-correlating reference offset, by FFT. About 1 ms per call."""
        n = self._nfft
        if search.size < self._frame:
            return None
        cc = np.fft.irfft(np.fft.rfft(search, n) * np.fft.rfft(mic[::-1], n), n)
        cc = cc[self._frame - 1:search.size]
        if cc.size == 0:
            return None
        cs = np.concatenate([[0.0], np.cumsum(search.astype(np.float64) ** 2)])
        win = cs[self._frame:search.size + 1] - cs[:search.size - self._frame + 1]
        m = min(cc.size, win.size)
        if m == 0:
            return None
        ncc = cc[:m] / np.sqrt(np.maximum(win[:m], 1.0))
        return int(np.argmax(np.abs(ncc)))

    def snapshot(self) -> dict:
        with self._lock:
            lag = self._locked_lag
        return {"echo_lag_ms": round(lag / self._rate * 1000, 1) if lag is not None else None,
                "echo_corr": round(self.last_corr, 3),
                "echo_gain": round(self.gain, 3),
                # How loud NOVA's own playback was for this frame. Without it
                # the rest of these numbers cannot answer the one question
                # that matters about an interruption — whether the sound that
                # caused it was the user or NOVA herself.
                "ref_rms": round(self.last_ref_rms, 1)}


class VoiceGate:
    """Decides, per mic frame, what NOVA should transmit to the model.

    Usage::

        gate = VoiceGate(chunk_samples=1024, on_barge_in=stop_playback)
        gate.reference(pcm)              # everything written to the speaker
        gate.set_speaking(True)          # when NOVA starts talking
        payload = gate.process(frame)    # every mic callback
        gate.set_speaking(False)         # when NOVA stops

    ``process`` always returns bytes to transmit — real audio when NOVA is
    listening, silence when she is speaking. It never returns ``None``:
    withholding frames entirely leaves gaps the model's turn detection has to
    guess about.
    """

    def __init__(
        self,
        chunk_samples: int,
        on_barge_in: Optional[Callable[[], None]] = None,
        barge_in_floor: float = BARGE_IN_FLOOR_RMS,
        barge_in_margin: float = BARGE_IN_ECHO_MARGIN,
        barge_in_chunks: int = BARGE_IN_CHUNKS,
        cooldown_s: float = SPEAK_COOLDOWN_S,
        rate: int = SEND_RATE,
        simple: Optional[bool] = None,
        speech_detector: Optional["SpeechDetector"] = None,
    ):
        #: Tells a voice from other sound. None keeps the loudness rules.
        self._detector = speech_detector
        #: Speech probability of the latest frame, and whether the user is
        #: speaking (with hangover) or something extremely loud just happened.
        self.last_speech_prob = 0.0
        self.last_is_voice = False
        self.last_is_loud_event = False
        self._last_voice_at = 0.0
        self._last_loud_event_at = 0.0
        self._voice_window: list[bool] = []
        #: Half-duplex, with none of the per-frame analysis. See
        #: :func:`simple_voice_default` for why this is the default.
        self.simple = simple_voice_default() if simple is None else bool(simple)
        self._silence = bytes(chunk_samples * 2)      # int16 = 2 bytes/sample
        self._on_barge_in = on_barge_in
        self._floor = barge_in_floor
        self._margin = barge_in_margin
        self._barge_in_chunks = barge_in_chunks
        self._cooldown_s = cooldown_s

        self.echo = EchoCanceller(rate=rate, frame=chunk_samples)

        self._lock = threading.Lock()
        self._speaking = False
        self._muted = False
        self._last_speak_end = 0.0
        self._speech_runs = 0
        self._speaking_frames = 0
        self._residual_history: list[float] = []
        self._ref_history: list[float] = []
        self._last_loud_ref_at = 0.0
        #: Loudest the room itself has been recently, in absolute RMS. Used
        #: only when there is no echo path, where it is the honest baseline.
        self._noise_floor = 0.0
        #: Learned level of the room with nobody speaking, and when it
        #: last had something in it. Together they decide whether a
        #: frame is worth the uplink.
        self._room_floor = 0.0
        self._last_loud_at = 0.0
        #: Frames of room heard while NOVA was silent, and the room level as
        #: it stood when she last started speaking -- the no-echo barge-in
        #: baseline. Snapshotted only once enough silence has been heard, so
        #: a gate that has only ever heard the user talking cannot mistake
        #: their voice for the room and become uninterruptible.
        self._room_frames = 0
        self._turn_room_floor = 0.0
        #: True when this frame is room tone rather than anyone talking.
        self.last_was_quiet = False
        #: Amplitude of the most recent mic frame, 0..1. Read by the desktop
        #: surface so the orb can react to the user's voice; the orb used to
        #: be driven only by NOVA's *outgoing* audio, so it sat still during
        #: the one moment it most needed to look like it was listening.
        self.last_level = 0.0
        #: Frames of NOVA's own speech to observe before a barge-in is
        #: believable. The echo path is unknown at the start of every turn,
        #: and 4 frames is ~256 ms — long enough for the filter to lock on,
        #: short enough that no real interruption lands inside it.
        self._warmup_frames = 4
        #: Worst leakage ratio (residual / aligned playback level) recently
        #: attributed to NOVA's own voice. This is what a genuine interruption
        #: has to beat, and it is learned rather than assumed so a reverberant
        #: room raises the bar by itself.
        self._residual_floor = 0.0

        # Counters, so a surface can report honestly instead of guessing.
        self.frames_sent = 0
        self.frames_muted = 0
        self.barge_ins = 0
        self.suppressed_echo_frames = 0
        self.echo_warmup_suppressions = 0
        self.last_residual = 0.0
        self.last_smoothed = 0.0
        self.last_leak_ratio = 0.0
        self.last_echo_path = False
        self.last_was_silence = False
        #: What the detector was looking at on the frame it decided someone
        #: was talking over NOVA, captured at the decision itself.
        #:
        #: Reading the gate's state afterwards does not answer this. By the
        #: time a barge-in has been published, `set_speaking(False)` has
        #: cleared the floors it was measured against, and the playback
        #: thread — a different thread — may already have set speaking back
        #: to True for the next chunk of a turn that has not stopped yet. A
        #: snapshot taken then describes neither the decision nor the moment.
        self.last_trigger: dict = {}

    # ── state ────────────────────────────────────────────────────────────

    @property
    def speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def set_speaking(self, value: bool, *, interrupted: bool = False) -> None:
        """Mark NOVA as speaking or not.

        ``interrupted=True`` means the user cut in. That distinction matters:
        a natural end needs the cooldown below, because the speaker's tail is
        still decaying into the microphone. An interruption does not — the
        user is mid-sentence, and swallowing the next quarter second of it is
        the difference between being heard and being ignored.

        Only a *change* of state resets anything, and that is the whole point
        of the guard below rather than an optimisation.

        Everything this method clears is per-turn learning: the run of
        consecutive speech frames a barge-in has to accumulate, the smoothing
        window, and the leak floor it is measured against. The caller is the
        playback path, which learns NOVA is speaking from each chunk of model
        audio it is handed — so it says so once per chunk, hundreds of times
        a turn. Re-running the reset on each of those zeroed the run of
        speech frames between almost every microphone frame, and a run that
        cannot reach two never reaches sixteen: measured on unambiguous
        continuous speech, 60 frames (~3.8 s) of it produced zero barge-ins
        with the redundant calls and one after 16 frames without them.

        That is the reported failure exactly — NOVA could not be interrupted
        by voice, only by the button.
        """
        with self._lock:
            if bool(value) == self._speaking:
                return
            self._speaking = bool(value)
            if not value:
                self._last_speak_end = 0.0 if interrupted else time.time()
                self._speech_runs = 0
                self._voice_window.clear()
                self._residual_floor = 0.0
                self._speaking_frames = 0
                self._residual_history.clear()
                self._ref_history.clear()
                self._last_loud_ref_at = 0.0
            else:
                self._speech_runs = 0
                self._voice_window.clear()
                self._turn_room_floor = (self._room_floor
                                         if self._room_frames >= ROOM_FRAMES_TO_TRUST
                                         else 0.0)
                # Begin every turn deaf to interruption and earn hearing back
                # as the echo path is learned. Failing in this direction means
                # a late barge-in; failing the other way means NOVA cutting
                # herself off, which is the bug this file exists to prevent.
                self._residual_floor = INITIAL_LEAK_RATIO
                self._residual_history.clear()
                self._ref_history.clear()
                self._last_loud_ref_at = 0.0

    @property
    def muted(self) -> bool:
        """User-controlled mute. Distinct from the automatic speak-mute."""
        with self._lock:
            return self._muted

    def set_muted(self, value: bool) -> None:
        with self._lock:
            self._muted = bool(value)

    def reference(self, pcm: bytes, rate: int = RECEIVE_RATE) -> None:
        """Tell the gate what was just handed to the speaker.

        A no-op under the simple policy: nothing reads the buffer, and filling
        it would mean resampling every chunk of NOVA's own speech on the
        playback thread for the benefit of a canceller that is switched off.
        """
        if self.simple:
            return
        self.echo.reference(pcm, rate)

    def reset(self) -> None:
        """Forget everything learned about this room. Used between sessions."""
        self.echo.reset()
        with self._lock:
            self._residual_floor = 0.0
            self._speech_runs = 0
            self._speaking_frames = 0
            self._residual_history.clear()
            self._ref_history.clear()

    # ── the policy ───────────────────────────────────────────────────────

    def process(self, frame: np.ndarray) -> bytes:
        """Return the bytes to transmit for this mic frame.

        Also records whether the frame was muted, readable from
        :attr:`last_was_silence`. Callers that batch frames before sending use
        it to avoid spending bandwidth on long runs of digital zeroes; it is
        written from the microphone callback and meant to be read there.
        """
        self._note_room(frame)
        self._note_voice(frame)
        if self.simple:
            return self._process_simple(frame)
        with self._lock:
            if self._muted:
                # Explicit user mute: still send silence so the session stays
                # alive and NOVA can resume instantly when unmuted.
                self.frames_muted += 1
                self.last_was_silence = True
                return self._silence
            speaking = self._speaking
            too_soon = (self._last_speak_end > 0.0
                        and (time.time() - self._last_speak_end) < self._cooldown_s)

        if not (speaking or too_soon):
            self.frames_sent += 1
            self.last_was_silence = False
            return frame.tobytes()

        # NOVA is talking (or just stopped). Mute by default, but listen for a
        # genuine interruption.
        if speaking and self._is_double_talk(frame):
            self.barge_ins += 1
            # Interrupted, not finished: no cooldown, or the next four frames
            # of the user's sentence are swallowed and the model receives a
            # fragment too short to act on.
            self.set_speaking(False, interrupted=True)
            if self._on_barge_in is not None:
                try:
                    self._on_barge_in()
                except Exception:
                    pass
            self.frames_sent += 1
            self.last_was_silence = False
            return frame.tobytes()

        self.frames_muted += 1
        self.last_was_silence = True
        return self._silence

    def _process_simple(self, frame: np.ndarray) -> bytes:
        """Half-duplex: transmit unless NOVA is talking.

        No correlation search, no residual, no learned floors — nothing that
        costs measurable time inside a real-time audio callback. The cooldown
        stays, because the speaker's tail is still decaying into the room for
        a moment after she stops, and that tail transcribes as the user
        speaking.
        """
        with self._lock:
            if self._muted or self._speaking or (
                    self._last_speak_end > 0.0
                    and (time.time() - self._last_speak_end) < self._cooldown_s):
                self.frames_muted += 1
                self.last_was_silence = True
                return self._silence
        self.frames_sent += 1
        self.last_was_silence = False
        return frame.tobytes()

    @property
    def hears_speech(self) -> bool:
        """Is a working speech model deciding what counts as the user?"""
        return self._detector is not None and self._detector.available

    def _note_voice(self, frame: np.ndarray) -> None:
        """Is this the user's voice, or a sound loud enough to matter?"""
        now = time.time()
        level = frame_rms(frame)
        if level > max(self._room_floor * LOUD_EVENT_FACTOR, LOUD_EVENT_MIN_RMS):
            self._last_loud_event_at = now
        self.last_is_loud_event = (self._last_loud_event_at > 0.0 and
                                   now - self._last_loud_event_at < LOUD_EVENT_HOLD_S)
        if not self.hears_speech:
            self.last_speech_prob = 0.0
            self.last_is_voice = False
            return
        self.last_speech_prob = self._detector.probability(frame)
        if self.last_speech_prob >= VOICE_PROB:
            self._last_voice_at = now
        self.last_is_voice = (self._last_voice_at > 0.0 and
                              now - self._last_voice_at < VOICE_HANGOVER_S)

    def _note_room(self, frame: np.ndarray) -> None:
        """Record whether this frame sounds like an empty room.

        The gate already suppresses NOVA's own voice, and the batcher already
        throttles that. What neither noticed is the far more common case: no
        one is talking at all. NOVA streamed 32 KB/s of room tone up to the
        model continuously, and on a connection without headroom that is the
        bandwidth the user's actual speech needed — measured on this machine,
        mic sends climbing past twenty seconds and 180 frames of real speech
        dropped because the queue behind them had filled with nothing.

        Quiet is judged against a floor learned from the room rather than a
        fixed number, because a laptop fan and a quiet study are not the same
        silence. And once anything is heard the hold-over keeps transmitting
        for a moment afterwards, so the tail of a word, a pause for breath
        mid-sentence, and the gap before "...actually, no" all still arrive.
        """
        level = frame_rms(frame)
        if not self._speaking:
            self._room_frames += 1
        # Normalised for anyone drawing it. The gate measures this on every
        # frame anyway to decide what is worth transmitting; the orb needs the
        # same number to show that someone is talking, and computing it twice
        # -- or opening a second microphone to get it -- would be absurd.
        self.last_level = min(1.0, level / 32768.0)
        if self._room_floor <= 0.0:
            self._room_floor = level
        loud = level > max(self._room_floor * QUIET_FLOOR_MARGIN, QUIET_FLOOR_MIN)
        if loud:
            self._last_loud_at = time.time()
        else:
            # Track the quiet only, so speech never drags the floor up after
            # itself and deafens her to the sentence that follows.
            self._room_floor += (level - self._room_floor) * 0.05
        self.last_was_quiet = (
            not loud and (time.time() - self._last_loud_at) > QUIET_HOLDOVER_S)

    def _is_double_talk(self, frame: np.ndarray) -> bool:
        """Is there sound here that NOVA's own playback cannot account for?

        Every path that decides "not the user" falls through to the same
        learning step at the bottom. That is deliberate: earlier versions
        returned early from the warm-up guards, which meant the learned floor
        never decayed while they were active. It was still sitting near its
        pessimistic starting value when the user began talking, so the first
        second of a genuine interruption was measured against a threshold
        that had never been allowed to come down, and barge-in took 1.2 s
        instead of 0.3 s.
        """
        residual = self.echo.residual_rms(frame)
        self.last_residual = residual

        with self._lock:
            self._residual_history.append(residual)
            # The reference level is smoothed over the *same* window. Dividing
            # a five-frame residual by an instantaneous playback level makes
            # the ratio explode every time NOVA pauses between words, and a
            # ratio that explodes is indistinguishable from someone shouting.
            self._ref_history.append(self.echo.last_ref_rms)
            if len(self._residual_history) > RESIDUAL_SMOOTHING_FRAMES:
                del self._residual_history[0]
                del self._ref_history[0]
            smoothed = sum(self._residual_history) / len(self._residual_history)
            ref_rms = sum(self._ref_history) / len(self._ref_history)
            floor = self._residual_floor
            self._speaking_frames += 1
            frames = self._speaking_frames
            # The room's level, from whichever source knows it. _noise_floor
            # only learns from frames under the fixed barge-in floor, so in a
            # room louder than that (this laptop's mic: 1,300-1,700 RMS empty)
            # it stayed 0 and all room tone counted as the user talking.
            # _room_floor is learned from quiet frames all the time; its
            # value as she began this turn is the room with her silent.
            noise = max(self._noise_floor, self._turn_room_floor)
        self.last_smoothed = smoothed

        now = time.time()
        # Keyed to the instantaneous reference, not the smoothed one. The
        # smoothed value still carries several frames of history after
        # playback has stopped, and refreshing the guard from that keeps
        # refreshing it from its own tail.
        if self.echo.last_ref_rms > REF_ACTIVE_RMS:
            self._last_loud_ref_at = now

        # Is there an echo path at all?
        #
        # If the speakers do not reach the microphone — headphones, a distant
        # speaker, a quiet room — the canceller never locks on, because there
        # is nothing to lock onto. Waiting for it forever left NOVA
        # permanently uninterruptible on exactly those machines.
        has_echo = self.echo.converged
        self.last_echo_path = has_echo
        ratio = smoothed / ref_rms if ref_rms > 1.0 else 0.0
        self.last_leak_ratio = ratio if has_echo else 0.0

        # Loud in its own right, in both the window and this frame. Smoothing
        # alone is not enough: one spike keeps a five-frame mean above the
        # floor afterwards, which reads as sustained speech when it was a door.
        loud = smoothed > self._floor and residual > self._floor * 0.5

        suppressed = False
        if has_echo and frames <= self._warmup_frames:
            # The filter has not learned this turn's echo path yet, so a loud
            # frame is not evidence of anything: it is just uncancelled NOVA.
            suppressed = True
        elif has_echo and (self._last_loud_ref_at
                           and self.echo.last_ref_rms <= REF_ACTIVE_RMS
                           and now - self._last_loud_ref_at < REF_TAIL_S):
            # The speaker has gone quiet on our side but the room has not.
            suppressed = True
        elif not has_echo and self.echo.armed and frames <= ECHO_LOCK_GRACE_FRAMES:
            # Still giving the canceller a chance to find an echo path.
            suppressed = True

        if suppressed:
            self.echo_warmup_suppressions += 1
            speech = False
        elif has_echo:
            speech = loud and (ref_rms <= 1.0 or ratio > floor * self._margin)
        else:
            # No echo path. The leak ratio is meaningless — it compares the
            # microphone against a playback level that never reaches it — so
            # judge the honest way: louder than this room has been.
            margin = NOISE_MARGIN_WITH_VOICE if self.hears_speech else NOISE_MARGIN
            speech = loud and smoothed > noise * margin
        # Whatever the energy says, only a voice may interrupt her. A key, a
        # chair or a door is loud and is not the user.
        voiced = (not self.hears_speech
                  or self.last_speech_prob >= BARGE_IN_VOICE_PROB)
        speech = speech and voiced
        if self.hears_speech and not suppressed and not has_echo:
            with self._lock:
                self._voice_window.append(bool(speech))
                del self._voice_window[:-VOICE_BARGE_IN_WINDOW]

        if not speech:
            self.echo.accept()
            with self._lock:
                # Decayed whenever the speaker is actually producing sound,
                # not only once the canceller has converged. Gating it on
                # convergence left the floor pinned at its starting value for
                # the whole lock-on period, and it was still there when the
                # user spoke.
                if ref_rms > 1.0 and ratio <= self._residual_floor * LEAK_LEARN_MARGIN:
                    # Decay only while actually learning. Decaying on frames we
                    # decline to learn from collapses the floor to zero during
                    # exactly the loud passage it is needed for.
                    self._residual_floor = max(
                        self._residual_floor * RESIDUAL_FLOOR_DECAY,
                        min(ratio, MAX_LEARNED_LEAK),
                        MIN_LEARNED_LEAK)
                # The room's own level, learned the same cautious way: only
                # from frames we are confident are not the user.
                if smoothed <= max(self._floor, self._noise_floor * LEAK_LEARN_MARGIN):
                    self._noise_floor = max(
                        self._noise_floor * RESIDUAL_FLOOR_DECAY, smoothed)
                self._speech_runs = 0
            if self.echo.armed:
                self.suppressed_echo_frames += 1
            return False

        with self._lock:
            self._speech_runs += 1
            if self.hears_speech and not self.last_echo_path:
                # Speech has gaps between words, so count voiced frames in a
                # window rather than demanding an unbroken run of them.
                needed = VOICE_BARGE_IN_NEEDED
                triggered = sum(self._voice_window) >= needed
            else:
                needed = (self._barge_in_chunks if self.last_echo_path
                          else max(self._barge_in_chunks, NO_ECHO_BARGE_IN_CHUNKS))
                triggered = self._speech_runs >= needed
            if triggered:
                self._speech_runs = 0
                self._voice_window.clear()
                self.last_trigger = {
                    # The deciding number. Playback loud here means the sound
                    # that stopped NOVA was most likely NOVA; playback silent
                    # means it came from the room.
                    "ref_rms": round(self.echo.last_ref_rms, 1),
                    "residual": round(residual, 1),
                    "smoothed": round(smoothed, 1),
                    "leak_ratio": round(ratio, 3) if has_echo else None,
                    "leak_floor": round(floor, 3),
                    "noise_floor": round(noise, 1),
                    "echo_path": has_echo,
                    "echo_gain": round(self.echo.gain, 3),
                    "echo_corr": round(self.echo.last_corr, 3),
                    "frames_speaking": frames,
                    "frames_required": needed,
                    "voice_prob": round(self.last_speech_prob, 2),
                }
        return triggered

    def snapshot(self) -> dict:
        with self._lock:
            base = {
                "speaking": self._speaking,
                "muted": self._muted,
                "frames_sent": self.frames_sent,
                "frames_muted": self.frames_muted,
                "barge_ins": self.barge_ins,
                "suppressed_echo_frames": self.suppressed_echo_frames,
                "echo_warmup_suppressions": self.echo_warmup_suppressions,
                "residual_floor": round(self._residual_floor, 1),
                "noise_floor": round(self._noise_floor, 1),
                "last_residual": round(self.last_residual, 1),
                "last_smoothed": round(self.last_smoothed, 1),
                "leak_ratio": round(self.last_leak_ratio, 3),
                "echo_path": self.last_echo_path,
            }
        base.update(self.echo.snapshot())
        return base


def drain(*queues) -> int:
    """Empty playback queues immediately. Used on barge-in.

    Accepts asyncio.Queue and queue.Queue alike; anything that raises is
    skipped, because a half-drained queue is still better than audio that
    keeps playing over the user.
    """
    dropped = 0
    for q in queues:
        if q is None:
            continue
        # queue.Queue — clear under its own mutex
        mutex = getattr(q, "mutex", None)
        inner = getattr(q, "queue", None)
        if mutex is not None and inner is not None:
            try:
                with mutex:
                    dropped += len(inner)
                    inner.clear()
                continue
            except Exception:
                pass
        # asyncio.Queue / anything with get_nowait
        getter = getattr(q, "get_nowait", None)
        if getter is None:
            continue
        while True:
            try:
                getter()
                dropped += 1
            except Exception:
                break
    return dropped


__all__ = [
    "VoiceGate", "EchoCanceller", "drain", "frame_rms",
    "BARGE_IN_FLOOR_RMS", "BARGE_IN_ECHO_MARGIN", "BARGE_IN_CHUNKS",
    "RESIDUAL_FLOOR_DECAY", "SPEAK_COOLDOWN_S",
    "SEND_RATE", "RECEIVE_RATE", "CHANNELS",
]
