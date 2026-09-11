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

#: Ignore the mic this long after NOVA stops speaking, so the speaker's
#: decaying tail is not mistaken for the user starting a turn.
SPEAK_COOLDOWN_S = 0.25

SEND_RATE = 16000           # mic -> model
RECEIVE_RATE = 24000        # model -> speaker
CHANNELS = 1



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
        self._pending = (corr, instant, locked)
        if gain is None:
            # Nothing learned yet. Using the instantaneous fit for this one
            # frame is the least-wrong option; accept() will make it stick.
            gain = instant
        residual = mic - gain * seg
        return float(np.sqrt(np.mean(residual.astype(np.float64) ** 2)))

    def accept(self) -> None:
        """Fold the last measured frame into the echo model.

        Call this only for frames judged to be NOVA's own voice.
        """
        pending = self._pending
        if pending is None:
            return
        corr, instant, lag = pending
        with self._lock:
            if self._gain is None:
                self._gain = instant
            else:
                alpha = (self.GAIN_ALPHA_FAST if abs(corr) >= self.CONFIDENT_CORR
                         else self.GAIN_ALPHA_SLOW if abs(corr) >= self.LOCK_QUALITY
                         else 0.0)
                self._gain = (1.0 - alpha) * self._gain + alpha * instant
            if abs(corr) >= self.LOCK_QUALITY:
                self._locked_lag = lag
        self._pending = None

    @property
    def converged(self) -> bool:
        """Has the echo path been learned well enough to judge a frame by?

        Until this is true a loud frame proves nothing: the filter is not yet
        cancelling, so NOVA's own voice is still sitting in the residual.
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
                "echo_gain": round(self.gain, 3)}


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
    ):
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
        """
        with self._lock:
            self._speaking = bool(value)
            if not value:
                self._last_speak_end = 0.0 if interrupted else time.time()
                self._speech_runs = 0
                self._residual_floor = 0.0
                self._speaking_frames = 0
                self._residual_history.clear()
                self._ref_history.clear()
                self._last_loud_ref_at = 0.0
            else:
                self._speech_runs = 0
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
        """Tell the gate what was just handed to the speaker."""
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
            noise = self._noise_floor
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
            speech = loud and smoothed > noise * NOISE_MARGIN

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
            triggered = self._speech_runs >= self._barge_in_chunks
            if triggered:
                self._speech_runs = 0
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
