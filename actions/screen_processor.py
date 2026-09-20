# actions/screen_processor.py
# NOVA — Vision & Screen Analysis (Fixed for google.genai SDK)

import asyncio
import io
import json
import logging
import os
import re
import sys
import time
import threading
import cv2
import mss
import mss.tools
import sounddevice as sd
from concurrent.futures import Future
from pathlib import Path
from google import genai as genai_live
from google.genai import types

try:
    import PIL.Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

log = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# ─── Constants ────────────────────────────────────────────────────────────────

_DEFAULT_LIVE_MODEL   = "models/gemini-2.5-flash-native-audio-preview-12-2025"
# An alias, not a pinned version: gemini-2.0-flash was retired and every
# screen analysis through this path returned HTTP 404. A dated id is a
# time bomb with a long fuse.
_DEFAULT_VISION_MODEL = "gemini-flash-latest"

CHANNELS            = 1
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

IMG_MAX_W = 640
IMG_MAX_H = 360
JPEG_Q    = 55

SYSTEM_PROMPT = (
    "You are NOVA, a JARVIS-class personal AI assistant. "
    "Analyze what you see with precision and intelligence. "
    "Be concise — maximum 2 short sentences. "
    "Address the user as 'sir'. "
    "Ask if further help is needed."
)

# ─── Config helpers ───────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _get_api_key():
    try:
        with open("config/api_keys.json") as f:
            data = json.load(f)
            # Try multiple key names for compatibility
            for key in ["GEMINI_API_KEY", "gemini_api_key"]:
                if key in data:
                    return data[key]
            raise KeyError("No Gemini API key found in config")
    except Exception as e:
        # Fallback to env var
        env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("gemini_api_key")
        if env_key:
            return env_key
        log.error(f"Failed to get API key: {e}")
        return None  # Return None instead of crashing

def _get_live_model() -> str:
    return (
        os.environ.get("NOVA_LIVE_MODEL")
        or _load_config().get("GEMINI_LIVE_MODEL")  # ← Changed from "live_model"
        or _load_config().get("live_model")           # ← Keep fallback
        or _DEFAULT_LIVE_MODEL
    )

def _get_vision_model() -> str:
    return (
        os.environ.get("NOVA_VISION_MODEL")
        or _load_config().get("GEMINI_VISION_MODEL")  # ← Changed from "vision_model"
        or _load_config().get("vision_model")         # ← Keep fallback
        or _DEFAULT_VISION_MODEL
    )

def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])

    print("[ScreenProc] 🔍 Auto-detecting camera...")
    best_index = 0

    for idx in range(6):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            continue
        for _ in range(5):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None and frame.mean() > 5:
            best_index = idx
            print(f"[ScreenProc] ✅ Camera at index {idx}")
            try:
                cfg["camera_index"] = best_index
                with open(API_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=4)
            except Exception as e:
                print(f"[ScreenProc] ⚠️ Could not save camera index: {e}")
            break
        else:
            print(f"[ScreenProc] ⚠️ Index {idx}: no valid frame.")

    return best_index

# ─── Image helpers ────────────────────────────────────────────────────────────

def _to_jpeg(img_bytes: bytes) -> bytes:
    if not _PIL_OK:
        return img_bytes
    img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img.thumbnail([IMG_MAX_W, IMG_MAX_H], PIL.Image.BILINEAR) # type: ignore
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_Q, optimize=False)
    return buf.getvalue()

def _capture_screenshot() -> bytes:
    with mss.mss() as sct:
        shot      = sct.grab(sct.monitors[1])
        png_bytes = mss.tools.to_png(shot.rgb, shot.size)
    if png_bytes is None:
        raise RuntimeError("Could not encode screenshot to PNG.")
    return _to_jpeg(png_bytes)

def _capture_camera() -> bytes:
    camera_index = _get_camera_index()
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError(f"Camera could not be opened: index {camera_index}")
    for _ in range(10):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        raise RuntimeError("Could not capture camera frame.")
    if _PIL_OK:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail([IMG_MAX_W, IMG_MAX_H], PIL.Image.BILINEAR) # type: ignore
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_Q, optimize=False)
        return buf.getvalue()
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
    return buf.tobytes()

# ─── Tier 1: Static vision (always runs, returns text) ───────────────────────

# Replace this function block in actions/screen_processor.py
def _static_analyze(image_bytes: bytes, user_text: str) -> str:
    """
    Sends image + question to Groq Vision model to avoid Gemini 429 rate limits.
    """
    import base64
    from groq import Groq

    # Load Groq key from environment
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        print("[ScreenProc] ⚠️ No GROQ_API_KEY found in environment variables.")
        return "Groq Vision key is missing."

    try:
        # 1. Convert the captured image bytes to base64
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        
        # 2. Initialize Groq client
        client = Groq(api_key=groq_key)
        
        print("[ScreenProc] 🌐 Routing screen analysis to Groq...")
        
        # 3. Call the active Scout vision model
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url", 
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                        }
                    ]
                }
            ]
        )
        
        text = response.choices[0].message.content.strip()
        print(f"[ScreenProc] 🔭 Groq static vision: {text[:120]}")
        return text

    except Exception as e:
        print(f"[ScreenProc] ❌ Groq Vision failed: {e}")
        return f"Vision tool failed internally: {str(e)}"
# ─── Tier 2: Live session (optional, audio output only) ──────────────────────

class _LiveSession:
    def __init__(self):
        self._loop:         asyncio.AbstractEventLoop | None = None
        self._thread:       threading.Thread | None          = None
        self._session                                        = None
        self._out_queue:    asyncio.Queue | None             = None
        self._audio_in:     asyncio.Queue | None             = None
        self._ready:        threading.Event                  = threading.Event()
        self._player                                         = None
        self._pending:      dict[int, Future]                = {}
        self._pending_lock: threading.Lock                   = threading.Lock()
        self._call_id:      int                              = 0
        self._alive:        bool                             = False

    def start(self, player=None) -> None:
        if self._thread and self._thread.is_alive() and self._alive:
            return
        self._player = player
        self._alive  = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="NOVAVisionThread"
        )
        self._thread.start()
        if not self._ready.wait(timeout=25):
            self._alive = False
            raise RuntimeError(
                "Live vision session failed to start within 25s. "
                "Check NOVA_LIVE_MODEL env var or 'live_model' in api_keys.json."
            )
        print("[ScreenProc] ✅ Live session ready")

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._alive = False
            self._ready.clear()
            print("[ScreenProc] 🔴 Live session loop exited")

    async def _main(self) -> None:
        self._out_queue = asyncio.Queue(maxsize=30)
        self._audio_in  = asyncio.Queue()

        client = genai_live.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"},
        )

        config: types.LiveConnectConfigDict = {
            "response_modalities": [types.Modality.AUDIO],
            "output_audio_transcription": {},
            "system_instruction": SYSTEM_PROMPT,
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": "Charon"
                    }
                }
            },
        }

        live_model = _get_live_model()
        print(f"[ScreenProc] 🔌 Connecting to {live_model}...")

        while self._alive:
            try:
                async with client.aio.live.connect(model=live_model, config=config) as session:
                    self._session = session
                    self._ready.set()
                    print("[ScreenProc] ✅ Live session connected")
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._send_loop())
                        tg.create_task(self._recv_loop())
                        tg.create_task(self._play_loop())
            except Exception as e:
                print(f"[ScreenProc] ⚠️ Session error: {e!r} — reconnecting in 3s...")
                self._session = None
                with self._pending_lock:
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(e)
                    self._pending.clear()
                if self._alive:
                    await asyncio.sleep(3)
                    self._ready.set()

    async def _send_loop(self) -> None:
        assert self._out_queue is not None
        while True:
            call_id, image_bytes, mime_type, user_text = await self._out_queue.get()
            if not self._session:
                continue
            try:
                await self._session.send_client_content(
                    turns=[
                        types.Content(
                            parts=[
                                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                                types.Part.from_text(text=user_text),
                            ]
                        )
                    ],
                    turn_complete=True,
                )
                print(f"[ScreenProc] ✅ Image sent (call {call_id})")
            except Exception as e:
                print(f"[ScreenProc] ⚠️ Send error (call {call_id}): {e}")
                with self._pending_lock:
                    fut = self._pending.pop(call_id, None)
                if fut and not fut.done():
                    fut.set_exception(e)

    async def _recv_loop(self) -> None:
        transcript_buf: list[str] = []
        current_call_id: int | None = None
        assert self._session is not None
        assert self._audio_in is not None

        async for response in self._session.receive():
            sc = response.server_content
            if sc and sc.model_turn and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    inline = getattr(part, "inline_data", None)
                    if inline is not None and getattr(inline, "data", None):
                        await self._audio_in.put(inline.data)

            if not sc:
                continue

            if sc.output_transcription is not None and sc.output_transcription.text:
                chunk = sc.output_transcription.text.strip()
                if chunk:
                    transcript_buf.append(chunk)

            if sc.turn_complete:
                full_text = re.sub(r"\s+", " ", " ".join(transcript_buf)).strip()
                transcript_buf = []

                with self._pending_lock:
                    if current_call_id is not None:
                        fut = self._pending.pop(current_call_id, None)
                        if fut and not fut.done():
                            fut.set_result(full_text or "Done, sir.")
                    current_call_id = None

                if full_text:
                    if self._player:
                        self._player.write_log(f"NOVA: {full_text}")
                    print(f"[ScreenProc] 💬 {full_text}")

            if current_call_id is None:
                with self._pending_lock:
                    ids = list(self._pending.keys())
                if ids:
                    current_call_id = ids[0]

    async def _play_loop(self) -> None:
        assert self._audio_in is not None
        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        stream.start()
        try:
            while True:
                chunk = await self._audio_in.get()
                await asyncio.to_thread(stream.write, chunk)
        finally:
            stream.stop()
            stream.close()

    def analyze(
        self,
        image_bytes: bytes,
        mime_type:   str,
        user_text:   str,
        timeout:     float = 20.0,
    ) -> str | None:
        if not self._loop or not self._session:
            return None

        with self._pending_lock:
            call_id = self._call_id
            self._call_id += 1
            fut: Future = Future()
            self._pending[call_id] = fut

        if self._out_queue is None:
            return None

        asyncio.run_coroutine_threadsafe(
            self._out_queue.put((call_id, image_bytes, mime_type, user_text)),
            self._loop,
        )

        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            print(f"[ScreenProc] ⚠️ Live result timeout/error: {e}")
            return None

    def is_ready(self) -> bool:
        return self._session is not None and self._alive

# ─── Singleton management ─────────────────────────────────────────────────────

_live        = _LiveSession()
_start_lock  = threading.Lock()

def _try_start_live(player=None) -> bool:
    with _start_lock:
        if _live.is_ready():
            if player is not None:
                _live._player = player
            return True
        try:
            _live.start(player=player)
            return True
        except Exception as e:
            print(f"[ScreenProc] ⚠️ Live session unavailable: {e}")
            return False

def _ensure_started(player=None) -> bool:
    """
    Make sure the live vision session is up before a capture/analyze call.
    Returns True if ready, False if it failed to start (caller can decide
    whether to fall back to _static_analyze).
    """
    if _live.is_ready():
        return True
    ok = _try_start_live(player=player)
    if not ok:
        print("[ScreenProc] ⚠️ _ensure_started: live session not ready, analyze() will no-op this call.")
    return ok

# ─── Public API ───────────────────────────────────────────────────────────────

def screen_process(
    parameters:     dict,
    response:       str | None = None,
    player=None,
    session_memory=None,
) -> bool:
    user_text = (parameters or {}).get("text") or (parameters or {}).get("user_text", "")
    user_text = (user_text or "").strip()
    if not user_text:
        print("[ScreenProcess] ⚠️ No user_text provided.")
        return False

    angle = (parameters or {}).get("angle", "screen").lower().strip()
    print(f"[ScreenProcess] angle={angle!r}  text={user_text!r}")

    _ensure_started(player=player)

    try:
        if angle == "camera":
            image_bytes = _capture_camera()
            mime_type   = "image/jpeg"
            print("[ScreenProcess] 📷 Camera captured")
        else:
            image_bytes = _capture_screenshot()
            mime_type   = "image/jpeg" if _PIL_OK else "image/png"
            print("[ScreenProcess] 🖥️ Screen captured")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[ScreenProcess] ❌ Capture error: {e}")
        return False

    print(f"[ScreenProcess] 📦 {len(image_bytes)} bytes → sending")
    _live.analyze(image_bytes, mime_type, user_text)
    return True


def warmup_session(player=None):
    try:
        _ensure_started(player=player)
    except Exception as e:
        print(f"[ScreenProcess] ⚠️ Warmup error: {e}")


if __name__ == "__main__":
    print("[TEST] screen_processor.py v8 — image-only session")
    print("=" * 50)
    mode    = input("screen / camera (default: screen): ").strip().lower() or "screen"
    request = input("Question (Enter for default): ").strip() or "What do you see? Be brief."

    t0 = time.perf_counter()
    warmup_session()
    print(f"Session ready — {time.perf_counter()-t0:.2f}s\n")

    t1     = time.perf_counter()
    result = screen_process({"angle": mode, "text": request}, player=None)
    print(f"Sent — {time.perf_counter()-t1:.3f}s | audio incoming...")
    time.sleep(8)
    print(f"\n{'✅' if result else '❌'}")