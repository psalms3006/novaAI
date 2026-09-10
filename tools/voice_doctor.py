"""Diagnose NOVA's voice pipeline, stage by stage, and say what is actually wrong.

    python tools/voice_doctor.py            # full check, including a live connect
    python tools/voice_doctor.py --offline  # skip anything that needs the network

Each stage is tested, not assumed. The point is to replace "voice doesn't work"
with a specific failing stage, because the failure modes are indistinguishable
from the outside: a rejected API key, a missing audio backend, a muted device
and a blocked microphone all present identically as an assistant that sits
there saying "listening" and never responds.

No secret is ever printed -- only a credential's length, prefix and kind.

Exit codes:  0 everything passed   1 a stage failed
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_results: list[tuple[str, str, str]] = []


def _say(text: str) -> None:
    """Print without ever dying on the console encoding.

    Windows consoles default to cp1252, and audio device names routinely
    contain characters it cannot encode -- a diagnostic that crashes on a
    device name would be worse than useless, since it crashes precisely on the
    machines it exists to diagnose.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"))


def record(stage: str, status: str, detail: str = "") -> None:
    _results.append((stage, status, detail))
    tick = {PASS: "[ok]", FAIL: "[FAIL]", WARN: "[warn]", SKIP: "[skip]"}[status]
    _say(f"  {tick:7} {stage}")
    if detail:
        for line in detail.splitlines():
            _say(f"          {line}")


# -- stages -----------------------------------------------------------------

def check_audio_backend() -> bool:
    try:
        import sounddevice as sd
    except Exception as e:
        record("audio backend (sounddevice)", FAIL,
               f"import failed: {type(e).__name__}: {e}\n"
               "NOVA cannot hear or speak at all without this. In a packaged "
               "build it usually means the PortAudio DLL was not bundled.")
        return False
    record("audio backend (sounddevice)", PASS, f"PortAudio {sd.get_portaudio_version()[1]}")
    return True


def check_devices() -> bool:
    import sounddevice as sd
    ok = True
    try:
        devices = sd.query_devices()
    except Exception as e:
        record("audio devices", FAIL, f"query_devices failed: {type(e).__name__}: {e}")
        return False

    ins = [d for d in devices if d["max_input_channels"] > 0]
    outs = [d for d in devices if d["max_output_channels"] > 0]
    if not ins:
        record("input device", FAIL, "no microphone is visible to NOVA")
        ok = False
    else:
        try:
            name = sd.query_devices(kind="input")["name"]
        except Exception:
            name = ins[0]["name"]
        record("input device", PASS, f"{len(ins)} available; default: {name}")
    if not outs:
        record("output device", FAIL, "no speaker is visible to NOVA")
        ok = False
    else:
        try:
            name = sd.query_devices(kind="output")["name"]
        except Exception:
            name = outs[0]["name"]
        record("output device", PASS, f"{len(outs)} available; default: {name}")
    return ok


def check_mic_capture(seconds: float = 2.0) -> bool:
    """Open the microphone for real and measure what arrives.

    A stream that opens but delivers digital silence is the single most
    misleading state: everything looks healthy and NOVA is deaf. Windows
    microphone privacy settings produce exactly this.
    """
    import numpy as np
    import sounddevice as sd

    frames: list = []
    try:
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                            blocksize=1024,
                            callback=lambda ind, n, t, s: frames.append(ind.copy())):
            print(f"          speak now -- listening for {seconds:.0f}s ...")
            time.sleep(seconds)
    except Exception as e:
        record("microphone capture", FAIL,
               f"could not open the microphone: {type(e).__name__}: {e}\n"
               "Check Windows Settings > Privacy & security > Microphone, and "
               "that 'Let desktop apps access your microphone' is on.")
        return False

    if not frames:
        record("microphone capture", FAIL, "the stream opened but delivered no audio")
        return False

    audio = np.concatenate(frames).astype("float32").reshape(-1)
    peak = float(np.abs(audio).max())
    rms = float(np.sqrt((audio ** 2).mean()))
    import nova_voice
    detail = (f"{len(frames)} blocks, peak {peak:.0f}, RMS {rms:.0f} "
              f"(NOVA's barge-in floor is {nova_voice.BARGE_IN_FLOOR_RMS:.0f})")
    if peak < 20:
        record("microphone capture", FAIL, detail +
               "\nThe device is delivering silence. It is muted at the OS or "
               "hardware level, or blocked by privacy settings.")
        return False
    if rms < 200:
        record("microphone capture", WARN, detail +
               "\nAudio is arriving but is quieter than NOVA's speech "
               "threshold. Raise the input level, or move closer.")
        return True
    record("microphone capture", PASS, detail)
    return True


def check_speaker() -> bool:
    """Play a short quiet tone through the same path NOVA speaks through."""
    import numpy as np
    import sounddevice as sd
    try:
        t = np.linspace(0, 0.35, int(24000 * 0.35), endpoint=False)
        tone = (np.sin(2 * np.pi * 440 * t) * 0.18 * 32767).astype("int16")
        with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as out:
            out.write(tone.tobytes())
        record("speaker output", PASS, "played a 440 Hz tone at 24 kHz "
                                       "(NOVA's output rate) -- you should have heard it")
        return True
    except Exception as e:
        record("speaker output", FAIL, f"{type(e).__name__}: {e}")
        return False


def check_echo_coupling(seconds: float = 3.0) -> bool:
    """Play a sound and measure how much of it comes back into the microphone.

    This is the number NOVA's listening policy is built on. She mutes the
    microphone while speaking and only interrupts herself for sound the echo
    canceller cannot explain, so how loudly the speakers reach the microphone
    on *this* machine decides how well that works.

    Measured through nova_voice.EchoCanceller itself, not by comparing
    loudness. Comparing loudness fails in a room with any ambient noise at all
    -- on this machine the room tone alone read 1877, which swamped the
    playback entirely and reported 0% coupling for speakers that were working
    perfectly. Correlating against the known probe is both robust to that and
    a genuine test of the component NOVA actually relies on.
    """
    try:
        import numpy as np
        import sounddevice as sd

        import nova_voice
    except Exception as e:
        record("echo coupling", SKIP, f"needs numpy + sounddevice ({e})")
        return True

    rate = nova_voice.SEND_RATE
    frame = 1024
    try:
        # Speech-shaped noise, not a tone: a pure tone measures one frequency
        # and says nothing about how a voice couples through.
        rng = np.random.default_rng(0)
        probe = rng.normal(0, 6000, int(rate * seconds))
        probe = np.convolve(probe, np.ones(60) / 60, mode="same")
        probe = (probe / max(1.0, np.abs(probe).max()) * 9000).astype(np.int16)

        ec = nova_voice.EchoCanceller(rate=rate, frame=frame)
        captured: list = []
        fed = {"n": 0}

        def _cb(indata, frames, t, status):
            captured.append(indata.copy().reshape(-1))

        with sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                            blocksize=frame, callback=_cb):
            sd.play(probe, rate)
            t0 = time.time()
            # Feed the canceller the probe as it plays, exactly as the
            # playback thread feeds it in the real pipeline.
            while time.time() - t0 < seconds:
                played = int((time.time() - t0) * rate)
                if played > fed["n"]:
                    ec.reference(probe[fed["n"]:played].tobytes(), rate)
                    fed["n"] = played
                time.sleep(0.02)
            sd.stop()
            time.sleep(0.2)

        if len(captured) < 8:
            record("echo coupling", WARN, "captured too little audio to judge")
            return True

        mic = np.concatenate(captured)
        gains, corrs = [], []
        for i in range(0, len(mic) - frame, frame):
            ec.residual_rms(mic[i:i + frame])
            if abs(ec.last_corr) > 0.25:
                gains.append(ec.gain)
                corrs.append(abs(ec.last_corr))
        if not gains:
            record("echo coupling", WARN,
                   "could not hear the test sound at all -- speakers may be "
                   "muted, or output is going somewhere the microphone cannot "
                   "hear (headphones, HDMI, another device)")
            return True

        coupling = float(np.median(gains))
        lag = ec.snapshot().get("echo_lag_ms")
        detail = (f"coupling {coupling:.0%}, echo delay {lag} ms, "
                  f"correlation {np.median(corrs):.2f}")
        if coupling <= 0.45:
            record("echo coupling", PASS, detail + " -- comfortable")
        else:
            record("echo coupling", WARN,
                   detail + " -- loud. NOVA will still never interrupt "
                   "herself, but barge-in may be slow; headphones or less "
                   "volume fixes it")
        return True
    except Exception as e:
        record("echo coupling", WARN, f"could not measure ({type(e).__name__}: {e})")
        return True


def check_tls() -> bool:
    """Establish TLS trust exactly as the app does before testing the network.

    nova.py and nova_desktop_app.py both call ensure_tls_trust() at startup, so
    a diagnostic that skips it exercises a different stack than the one that
    actually runs -- and reports certificate errors the app never sees.
    """
    try:
        import nova_tls
    except Exception as e:
        record("TLS trust", WARN, f"nova_tls unavailable: {type(e).__name__}: {e}")
        return False
    try:
        nova_tls.ensure_tls_trust()
    except Exception as e:
        record("TLS trust", FAIL, f"ensure_tls_trust failed: {type(e).__name__}: {e}")
        return False
    try:
        ok, err = nova_tls.probe(timeout=8.0)
    except Exception as e:
        record("TLS trust", WARN, f"trust applied; probe failed: {type(e).__name__}: {e}")
        return True
    if ok:
        record("TLS trust", PASS, "outbound HTTPS verified against the OS trust store")
        return True
    record("TLS trust", FAIL,
           f"HTTPS cannot be verified: {err}\n"
           "Every Gemini call will fail with a certificate error. This is "
           "usually corporate TLS interception or a stale certifi bundle.")
    return False


def check_credential() -> tuple[bool, str]:
    try:
        from desk.creds import classify_credential, resolve
    except Exception as e:
        record("credential", FAIL, f"could not load desk.creds: {type(e).__name__}: {e}")
        return False, ""

    status = resolve()
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    mode = status.get("mode")

    if not key:
        record("credential", FAIL,
               f"no credential resolved (mode: {mode}).\n"
               "Voice cannot start. Add a Gemini API key in Settings.")
        return False, ""

    info = classify_credential(key)
    shape = f"mode={mode}, kind={info['kind']}, length={len(key)}, prefix={key[:4]!r}"
    # Durability is the question, not the exact prefix: AI Studio issues both
    # `AIza…` and `AQ.Ab8…` keys and both are perfectly good.
    if info["durable"]:
        record("credential", PASS, shape)
        return True, key
    record("credential", WARN if info["durable"] else FAIL,
           shape + "\n" + info["warning"])
    return bool(info["durable"]), key


def check_live_connect(key: str) -> bool:
    """Actually open a Gemini Live session. Nothing else proves the key works."""
    try:
        from google import genai
        from google.genai import types as gtypes
    except Exception as e:
        record("Gemini SDK", FAIL, f"google-genai not importable: {type(e).__name__}: {e}")
        return False
    record("Gemini SDK", PASS, "google-genai imported")

    import asyncio
    from desk.live_session import LIVE_MODEL_DEFAULT

    async def _try() -> tuple[bool, str, float]:
        t0 = time.time()
        try:
            client = genai.Client(api_key=key)
            cfg = gtypes.LiveConnectConfig(
                response_modalities=[gtypes.Modality.AUDIO])
            async with client.aio.live.connect(model=LIVE_MODEL_DEFAULT,
                                               config=cfg):
                return True, "", time.time() - t0
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", time.time() - t0

    try:
        ok, err, dt = asyncio.run(asyncio.wait_for(_try(), timeout=30))
    except Exception as e:
        ok, err, dt = False, f"{type(e).__name__}: {e}", 30.0

    if ok:
        record("Gemini Live connect", PASS,
               f"connected in {dt:.2f}s to {LIVE_MODEL_DEFAULT}")
        return True

    from desk.live_session import LiveManager
    hint = ""
    if LiveManager._is_auth_failure(err):
        hint = ("\nGemini rejected the credential. This is the exact failure "
                "behind a NOVA that shows 'listening' but never responds: the "
                "session never opens, so the microphone is never opened either.")
    elif "handshake" in err.lower() or "timed out" in err.lower():
        hint = ("\nThe connection timed out rather than being rejected -- that "
                "points at the network, a proxy, or TLS interception.")
    record("Gemini Live connect", FAIL, f"after {dt:.2f}s: {err}{hint}")
    return False


# -- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="skip the stages that need the network")
    ap.add_argument("--no-mic", action="store_true",
                    help="skip the live microphone capture test")
    ap.add_argument("--seconds", type=float, default=2.0,
                    help="how long to sample the microphone (default 2)")
    args = ap.parse_args()

    print(f"\nNOVA voice doctor")
    print(f"  python   {sys.version.split()[0]}")
    print(f"  frozen   {getattr(sys, 'frozen', False)}")
    print(f"  root     {ROOT}\n")

    print("audio")
    have_audio = check_audio_backend()
    if have_audio:
        check_devices()
        if args.no_mic:
            record("microphone capture", SKIP, "--no-mic")
        else:
            check_mic_capture(args.seconds)
        check_speaker()
        if args.no_mic:
            record("echo coupling", SKIP, "--no-mic")
        else:
            check_echo_coupling()
    else:
        record("audio devices", SKIP, "no audio backend")

    print("\ncredential")
    cred_ok, key = check_credential()

    print("\nnetwork")
    if not args.offline:
        check_tls()
    if args.offline:
        record("Gemini Live connect", SKIP, "--offline")
    elif not key:
        record("Gemini Live connect", SKIP, "no credential to test")
    else:
        check_live_connect(key)

    failed = [r for r in _results if r[1] == FAIL]
    warned = [r for r in _results if r[1] == WARN]
    print("\n" + "-" * 68)
    print(f"{len([r for r in _results if r[1] == PASS])} passed, "
          f"{len(warned)} warning(s), {len(failed)} failed")
    if failed:
        print("\nVoice will not work until these are fixed:")
        for stage, _, _ in failed:
            print(f"  - {stage}")
    elif warned:
        print("\nVoice should work, with the caveats above.")
    else:
        print("\nEvery stage passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
