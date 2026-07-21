#!/usr/bin/env python3
"""
NOVA Wake Word Detector — nova_wake.py v1.0
═══════════════════════════════════════════
Runs silently in the background. Launches nova.py when it hears:

  🗣️  Voice trigger (any of):
      "Wake up NOVA"  |  "Hello NOVA"  |  "Hey NOVA"  |  "Hi NOVA"
      "NOVA wake up"  |  "OK NOVA"     |  "Rise NOVA"  |  "Start NOVA"
      + fuzzy: any phrase with "nova" + a greeting/command word

  👏  Gesture trigger:
      Double clap — two sharp sounds within 1.5 seconds

Usage:
  python nova_wake.py              → Start detector (shows console output)
  python nova_wake.py --install    → Add to Windows startup (runs silently)
  python nova_wake.py --uninstall  → Remove from Windows startup
  python nova_wake.py --status     → Check startup registration
  python nova_wake.py --test       → Run mic calibration and phrase test

Requirements (already in NOVA's stack):
  sounddevice  scipy  faster-whisper  numpy
"""

from __future__ import annotations

import sys
import os
import time
import threading
import tempfile
import subprocess
import re
from pathlib import Path
from collections import deque
from typing import Optional, List, Deque

import numpy as np
import sounddevice as sd
from scipy.io.wavfile import write as wav_write


from nova_ui import init_ui

# Start the UI
ui = init_ui()

# In your main loop, call these at the right moments:

# When script starts
ui.set_state('idle', 'NOVA Online')

# When wake word detected
ui.wake()

# When processing command
ui.set_state('speaking', 'Processing...')

# When speaking response
ui.speak("Here's what I found")

# When going back to listen
ui.listen()

# On error
ui.error("Something went wrong")

# ══════════════════════════════════════════════════════════════════════════════
#  PATHS & TUNABLE CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

NOVA_DIR    = Path(__file__).parent.resolve()
NOVA_SCRIPT = NOVA_DIR / "nova.py"

# Wake phrases — substring match (case-insensitive).
# Keep them short so Whisper-tiny transcribes them correctly.
WAKE_PHRASES: List[str] = [
    "wake up nova",
    "hello nova",
    "hey nova",
    "hi nova",
    "nova wake up",
    "nova hello",
    "nova hey",
    "nova hi",
    "ok nova",
    "okay nova",
    "rise nova",
    "nova rise",
    "start nova",
    "nova start",
    "activate nova",
    "nova activate",
    "good morning nova",
    "nova come online",
    "yo nova",
    "nova yo",
]

# Words that, combined with "nova" in the transcript, also count as a wake.
WAKE_COMMAND_WORDS = [
    "wake", "hello", "hey", "hi", "rise", "come", "start",
    "activate", "ok", "okay", "good", "morning", "evening",
    "night", "yo", "sup", "up", "online",
]

# ── Audio ──────────────────────────────────────────────────────────────────
FS              = 16000   # Sample rate (Hz)
CHUNK           = 512     # Frames per callback (~32 ms at 16 kHz)

# Speech detection
ENERGY_TH       = 0.018   # RMS above this = potential speech
MIN_SPEECH_SEC  = 0.35    # Seconds of sustained speech before we start recording
CAP_DUR_SEC     = 3.5     # Max seconds to capture for one utterance
SILENCE_END_SEC = 0.9     # Seconds of silence that ends the capture

# Clap detection
CLAP_TH         = 0.10    # RMS spike that counts as a clap
CLAP_MAX_MS     = 90      # A clap is brief — max duration in ms
CLAP_WIN_SEC    = 1.5     # Two claps within this window = double-clap
CLAP_MIN_GAP    = 0.10    # Minimum gap between two claps (avoids echo)

# Misc
WAKE_DEBOUNCE   = 6.0     # Seconds to ignore further detections after waking
WHISPER_MODEL   = "tiny"  # "tiny" ≈ 75 MB, ~0.5 s on CPU; "base" is more accurate


# ══════════════════════════════════════════════════════════════════════════════
#  GLOBALS
# ══════════════════════════════════════════════════════════════════════════════

_stt_model: Optional[object] = None
_stt_lock   = threading.Lock()
_stt_ready  = threading.Event()
_running    = True
_last_wake  = 0.0  # debounce timestamp


# ══════════════════════════════════════════════════════════════════════════════
#  STT  (faster-whisper tiny — loaded once in background thread)
# ══════════════════════════════════════════════════════════════════════════════

def _load_stt() -> None:
    global _stt_model
    try:
        from faster_whisper import WhisperModel  # type: ignore
        print(f"[WAKE] 🧠 Loading Whisper {WHISPER_MODEL!r} model…")
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        with _stt_lock:
            _stt_model = model
        _stt_ready.set()
        print(f"[WAKE] ✅ Whisper {WHISPER_MODEL!r} ready — voice detection active.")
    except ImportError:
        print("[WAKE] ❌ faster-whisper not installed → voice wake disabled.")
        print("       Fix: pip install faster-whisper")
        print("[WAKE] ℹ️  Double-clap detection is still active.")
    except Exception as e:
        print(f"[WAKE] ❌ STT load failed: {e}")
        print("[WAKE] ℹ️  Double-clap detection is still active.")


def _transcribe(wav_path: str) -> str:
    """Transcribe a WAV file. Returns '' if model not ready or error."""
    if not _stt_ready.wait(timeout=60):
        return ""
    with _stt_lock:
        if _stt_model is None:
            return ""
        try:
            segments, _ = _stt_model.transcribe(  # type: ignore[union-attr]
                wav_path,
                language="en",
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip().lower()
        except Exception as e:
            print(f"[WAKE] ⚠️  Transcription error: {e}")
            return ""


# ══════════════════════════════════════════════════════════════════════════════
#  WAKE TRIGGER
# ══════════════════════════════════════════════════════════════════════════════

def _is_nova_running() -> bool:
    """Return True if nova.py is already running."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["wmic", "process", "where", "name='python.exe'",
                 "get", "commandline"],
                capture_output=True, text=True, timeout=5,
            )
            return "nova.py" in r.stdout
        else:
            r = subprocess.run(
                ["pgrep", "-f", "nova.py"],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
    except Exception:
        return False


def _launch_nova() -> None:
    """Launch nova.py in a new console window. Debounced."""
    global _last_wake
    now = time.time()
    if now - _last_wake < WAKE_DEBOUNCE:
        return
    _last_wake = now

    _play_chime()

    if _is_nova_running():
        print("[WAKE] ℹ️  NOVA is already running.")
        return

    print("[WAKE] 🚀 Launching NOVA…")
    try:
        kwargs: dict = {"cwd": str(NOVA_DIR)}
        if sys.platform == "win32":
            # Open NOVA in its own console window
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen([sys.executable, str(NOVA_SCRIPT)], **kwargs)
    except Exception as e:
        print(f"[WAKE] ❌ Launch failed: {e}")


def _play_chime(freq1: int = 660, freq2: int = 990, ms: int = 250) -> None:
    """
    Play a two-note rising chime so the user knows the wake word was heard.
    Runs on a daemon thread so it never blocks detection.
    """
    def _play() -> None:
        try:
            t = np.linspace(0, ms / 1000, int(FS * ms / 1000), False)
            tone  = 0.28 * np.sin(2 * np.pi * freq1 * t)
            tone += 0.18 * np.sin(2 * np.pi * freq2 * t)
            # Smooth fade-in + fade-out
            fade = np.ones_like(t)
            attack = int(0.05 * len(t))
            fade[:attack] = np.linspace(0, 1, attack)
            fade[-attack:] = np.linspace(1, 0, attack)
            sd.play((tone * fade).astype(np.float32), samplerate=FS, blocking=True)
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  PHRASE CHECKER
# ══════════════════════════════════════════════════════════════════════════════

def _check_transcript(text: str) -> bool:
    """Return True if text contains a recognised wake phrase."""
    text = re.sub(r"[^\w\s]", "", text.lower().strip())
    if not text:
        return False
    # Exact substring match
    for phrase in WAKE_PHRASES:
        if phrase in text:
            print(f"[WAKE] 🎤 '{text}' → exact match: '{phrase}'")
            return True
    # Fuzzy: "nova" + any command word
    if "nova" in text.split():
        words = set(text.split())
        for cmd in WAKE_COMMAND_WORDS:
            if cmd in words:
                print(f"[WAKE] 🎤 '{text}' → fuzzy match (nova + {cmd!r})")
                return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  WAKE DETECTOR STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

class WakeDetector:
    """
    Processes audio chunks from the sounddevice callback.

    States:
        IDLE         — monitoring energy, checking for speech onset
        RECORDING    — capturing audio for STT
        TRANSCRIBING — waiting for STT result (audio loop continues)
    """

    def __init__(self) -> None:
        # ── Speech detection ─────────────────────────────────────────────────
        self.state            = "IDLE"
        self.recorded:        List[np.ndarray] = []
        self.speech_chunks    = 0
        self.silence_chunks   = 0
        self._min_speech      = max(1, int(MIN_SPEECH_SEC * FS / CHUNK))
        self._silence_cutoff  = max(1, int(SILENCE_END_SEC * FS / CHUNK))
        self._max_chunks      = max(1, int(CAP_DUR_SEC * FS / CHUNK))

        # ── Clap detection ───────────────────────────────────────────────────
        self.clap_times: Deque[float] = deque(maxlen=10)
        self._in_clap         = False
        self._clap_start      = 0.0
        self._clap_chunk_cnt  = 0
        self._clap_max_chunks = max(1, int(CLAP_MAX_MS / 1000 * FS / CHUNK))

    # ── Public entry point ───────────────────────────────────────────────────

    def process(self, chunk: np.ndarray, ts: float) -> None:
        """Called once per audio chunk (~32 ms of mono float32)."""
        energy = float(np.sqrt(np.mean(chunk ** 2)))
        self._update_clap(energy, ts)
        self._update_speech(chunk, energy)

    # ── Clap detection ───────────────────────────────────────────────────────

    def _update_clap(self, energy: float, ts: float) -> None:
        if not self._in_clap:
            if energy > CLAP_TH:
                self._in_clap        = True
                self._clap_start     = ts
                self._clap_chunk_cnt = 1
        else:
            if energy > CLAP_TH:
                self._clap_chunk_cnt += 1
                if self._clap_chunk_cnt > self._clap_max_chunks:
                    # Too long — sustained sound, not a clap
                    self._in_clap        = False
                    self._clap_chunk_cnt = 0
            else:
                # Clap ended
                if self._clap_chunk_cnt <= self._clap_max_chunks:
                    self._register_clap(ts)
                self._in_clap        = False
                self._clap_chunk_cnt = 0

    def _register_clap(self, ts: float) -> None:
        # Remove old claps outside the window
        while self.clap_times and ts - self.clap_times[0] > CLAP_WIN_SEC:
            self.clap_times.popleft()

        if self.clap_times:
            gap = ts - self.clap_times[-1]
            if gap >= CLAP_MIN_GAP:
                # Valid gap — register this clap
                self.clap_times.append(self._clap_start)
                if len(self.clap_times) >= 2:
                    total_span = self.clap_times[-1] - self.clap_times[0]
                    if CLAP_MIN_GAP <= total_span <= CLAP_WIN_SEC:
                        print(
                            f"[WAKE] 👏 Double clap! "
                            f"(gap {total_span:.2f}s)"
                        )
                        self.clap_times.clear()
                        self._fire()
        else:
            self.clap_times.append(self._clap_start)

    # ── Speech / voice detection ─────────────────────────────────────────────

    def _update_speech(self, chunk: np.ndarray, energy: float) -> None:
        if self.state == "TRANSCRIBING":
            return  # Don't touch state while STT is running

        if self.state == "IDLE":
            if energy > ENERGY_TH:
                self.speech_chunks += 1
                if self.speech_chunks >= self._min_speech:
                    self.state          = "RECORDING"
                    self.recorded       = [chunk.copy()]
                    self.silence_chunks = 0
            else:
                # Decay slowly so brief pauses don't reset counter
                self.speech_chunks = max(0, self.speech_chunks - 1)

        elif self.state == "RECORDING":
            self.recorded.append(chunk.copy())
            if energy < ENERGY_TH:
                self.silence_chunks += 1
            else:
                self.silence_chunks = 0

            done = (
                self.silence_chunks >= self._silence_cutoff
                or len(self.recorded) >= self._max_chunks
            )
            if done:
                self._finalize()

    def _finalize(self) -> None:
        if not self.recorded:
            self._reset()
            return
        audio = np.concatenate(self.recorded, axis=0)
        self.recorded       = []
        self.speech_chunks  = 0
        self.silence_chunks = 0
        self.state          = "TRANSCRIBING"
        threading.Thread(
            target=self._run_stt, args=(audio,), daemon=True
        ).start()

    def _run_stt(self, audio: np.ndarray) -> None:
        audio_i16 = (audio * 32767).astype(np.int16)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
            wav_write(tmp, FS, audio_i16)
        try:
            text = _transcribe(tmp)
            if text:
                if _check_transcript(text):
                    self._fire()
                else:
                    print(f"[WAKE] 💬 (not a wake phrase): '{text}'")
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
            self._reset()

    def _reset(self) -> None:
        self.state         = "IDLE"
        self.speech_chunks = 0
        self.silence_chunks = 0
        self.recorded      = []

    # ── Fire ─────────────────────────────────────────────────────────────────

    def _fire(self) -> None:
        """Trigger NOVA launch on a daemon thread."""
        threading.Thread(target=_launch_nova, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  CALIBRATION (--test mode)
# ══════════════════════════════════════════════════════════════════════════════

def run_calibration() -> None:
    """
    Measure ambient noise level and print recommended ENERGY_TH.
    Also transcribes a test phrase so the user can verify Whisper output.
    """
    print("\n[CALIBRATE] Measuring ambient noise for 3 seconds — stay quiet…")
    samples: List[float] = []

    def _cb(indata: np.ndarray, frames: int, t: object, s: object) -> None:
        samples.append(float(np.sqrt(np.mean(indata.astype(np.float32) ** 2))))

    with sd.InputStream(samplerate=FS, channels=1, dtype="float32",
                        blocksize=CHUNK, callback=_cb):
        time.sleep(3.0)

    arr = np.array(samples)
    ambient = float(np.mean(arr))
    peak    = float(np.max(arr))
    suggested = min(round(ambient * 4, 4), 0.06)
    print(f"[CALIBRATE] Ambient RMS : {ambient:.4f}")
    print(f"[CALIBRATE] Peak RMS    : {peak:.4f}")
    print(f"[CALIBRATE] Suggested ENERGY_TH: {suggested}")
    if suggested > ENERGY_TH:
        print(f"[CALIBRATE] ⚠️  Your environment is noisy. "
              f"Edit ENERGY_TH = {suggested} in nova_wake.py to reduce false triggers.")
    else:
        print(f"[CALIBRATE] ✅ Current ENERGY_TH={ENERGY_TH} looks good.")

    # Test a short phrase
    print("\n[CALIBRATE] Say 'Wake up NOVA' in 3 seconds…")
    time.sleep(1.0)
    print("[CALIBRATE] Go! Recording 3 s…")
    recorded: List[np.ndarray] = []

    def _rec(indata: np.ndarray, frames: int, t: object, s: object) -> None:
        recorded.append(indata[:, 0].astype(np.float32).copy())

    with sd.InputStream(samplerate=FS, channels=1, dtype="float32",
                        blocksize=CHUNK, callback=_rec):
        time.sleep(3.0)

    audio = np.concatenate(recorded)
    audio_i16 = (audio * 32767).astype(np.int16)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
        wav_write(tmp, FS, audio_i16)

    print("[CALIBRATE] Transcribing with Whisper…")
    _load_stt()
    _stt_ready.wait(timeout=120)
    result = _transcribe(tmp)
    os.remove(tmp)
    print(f"[CALIBRATE] Whisper heard: '{result}'")
    if _check_transcript(result):
        print("[CALIBRATE] ✅ Wake phrase detected correctly!")
    else:
        print("[CALIBRATE] ❌ Phrase not matched. Check mic or adjust WAKE_PHRASES.")


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP MANAGEMENT (Windows)
# ══════════════════════════════════════════════════════════════════════════════

def _bat_path() -> Path:
    appdata = os.getenv("APPDATA", "")
    return (
        Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
        / "Programs" / "Startup" / "NOVA_Wake.bat"
    )


def install_startup() -> str:
    """Register nova_wake.py in Windows Startup (runs hidden on login)."""
    bat = _bat_path()
    # pythonw.exe runs without a console window
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    runner  = str(pythonw) if pythonw.exists() else sys.executable
    script  = str(Path(__file__).resolve())
    content = f'@echo off\nstart "" "{runner}" "{script}"\n'
    try:
        bat.write_text(content, encoding="utf-8")
        return (
            f"✅ NOVA Wake added to Windows startup.\n"
            f"   It will run silently every time you log in.\n"
            f"   File: {bat}"
        )
    except Exception as e:
        return f"❌ Install failed: {e}"


def uninstall_startup() -> str:
    bat = _bat_path()
    if bat.exists():
        bat.unlink()
        return "✅ NOVA Wake removed from startup."
    return "ℹ️  NOVA Wake was not registered in startup."


def status_startup() -> str:
    bat = _bat_path()
    if bat.exists():
        return f"✅ NOVA Wake IS registered for startup:\n   {bat}"
    return "❌ NOVA Wake is NOT in startup. Run: python nova_wake.py --install"


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN AUDIO LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_detector() -> None:
    """Block-forever listening loop."""
    detector = WakeDetector()

    def _callback(indata: np.ndarray, frames: int, time_info: object,
                  status: object) -> None:
        mono = indata[:, 0].astype(np.float32)
        detector.process(mono, time.time())

    print("[WAKE] 👂 Listening…")
    try:
        with sd.InputStream(
            samplerate=FS, channels=1, dtype="float32",
            blocksize=CHUNK, callback=_callback,
        ):
            while _running:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[WAKE] 🔴 Stopped.")
    except Exception as e:
        print(f"[WAKE] ❌ Audio stream error: {e}")
        raise


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    argv = sys.argv[1:]

    if "--install" in argv:
        print(install_startup())
        sys.exit(0)

    if "--uninstall" in argv:
        print(uninstall_startup())
        sys.exit(0)

    if "--status" in argv:
        print(status_startup())
        sys.exit(0)

    if "--test" in argv:
        run_calibration()
        sys.exit(0)

    # ── Normal run ────────────────────────────────────────────────────────────
    print("=" * 58)
    print("  N.O.V.A  Wake Word Detector  v1.0")
    print("  ─────────────────────────────────────────────────────")
    print("  🗣️   Say: 'Wake up NOVA'  |  'Hello NOVA'  |  'Hey NOVA'")
    print("  👏  Or:  Double-clap")
    print("  ─────────────────────────────────────────────────────")
    print("  Options:")
    print("    --install    Add to Windows startup (runs silently)")
    print("    --uninstall  Remove from startup")
    print("    --test       Mic calibration + phrase test")
    print("  Ctrl+C to stop")
    print("=" * 58)

    if not NOVA_SCRIPT.exists():
        print(f"[WAKE] ⚠️  nova.py not found at {NOVA_SCRIPT}")
        print("       Place nova_wake.py in the same folder as nova.py")

    # Load STT in background — clap detection starts immediately
    threading.Thread(target=_load_stt, daemon=True).start()

    run_detector()