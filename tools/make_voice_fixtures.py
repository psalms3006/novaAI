"""Generate the audio fixtures the voice tests need.

Two recordings are required, and both have to be *real*:

  nova_speech_24k.wav — an actual Gemini Live response. Synthetic tones do not
    reproduce the bug these tests guard: it depends on speech being loud and
    bursty in the way a real voice is.
  user_speech_16k.wav — a person talking. Produced here with the Windows
    speech synthesiser, which is offline, deterministic and on every target
    machine.

Run:  python tools/make_voice_fixtures.py
Needs GEMINI_API_KEY for the first file only; if it is unavailable the script
says so and leaves any existing fixture alone.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "tests" / "fixtures" / "voice"

NOVA_WAV = OUT / "nova_speech_24k.wav"
USER_WAV = OUT / "user_speech_16k.wav"

LIVE_MODEL = os.getenv("LIVE_MODEL", "models/gemini-3.1-flash-live-preview")


def _write(path: Path, pcm: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    print(f"wrote {path.relative_to(ROOT)}  {len(pcm)} bytes  "
          f"{len(pcm) / 2 / rate:.2f}s")


async def _capture_nova() -> bytes:
    import nova_tls
    nova_tls.ensure_tls_trust()
    from google import genai
    from google.genai import types as gt

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=key)
    cfg = gt.LiveConnectConfig(
        response_modalities=[gt.Modality.AUDIO],
        system_instruction="You are NOVA. Be warm and concise.",
        speech_config=gt.SpeechConfig(voice_config=gt.VoiceConfig(
            prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name="Aoede"))),
    )
    chunks: list[bytes] = []
    async with client.aio.live.connect(model=LIVE_MODEL, config=cfg) as s:
        await s.send_client_content(
            turns={"role": "user", "parts": [{"text":
                "Greet the user warmly in two short sentences, then ask what "
                "they need."}]},
            turn_complete=True)
        async for msg in s.receive():
            sc = msg.server_content
            if sc is None:
                continue
            if sc.model_turn and sc.model_turn.parts:
                for p in sc.model_turn.parts:
                    data = getattr(getattr(p, "inline_data", None), "data", None)
                    if data:
                        chunks.append(data)
            if sc.turn_complete:
                break
    return b"".join(chunks)


def _capture_user() -> None:
    """Windows SAPI, via PowerShell — no third-party TTS dependency."""
    OUT.mkdir(parents=True, exist_ok=True)
    target = str(USER_WAV).replace("\\", "\\\\")
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, `
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, `
  [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile("{target}", $fmt)
$s.Speak("NOVA, can you hear me? I would like to ask you a question.")
$s.Dispose()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print(f"wrote {USER_WAV.relative_to(ROOT)}")


def main() -> int:
    if sys.platform != "win32":
        print("user-speech fixture needs Windows SAPI; skipping that half")
    elif USER_WAV.exists():
        print(f"{USER_WAV.name} already present")
    else:
        _capture_user()

    if NOVA_WAV.exists():
        print(f"{NOVA_WAV.name} already present")
        return 0
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    try:
        pcm = asyncio.run(_capture_nova())
    except Exception as e:
        print(f"could not capture Gemini Live audio: {e}")
        print("voice echo tests will skip until this fixture exists")
        return 1
    _write(NOVA_WAV, pcm, 24000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
