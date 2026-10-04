"""
vision_extra.py
════════════════
Screen/camera capture, face detection, Gemini vision + OCR analysis.
Extracted from nova.py (Phase 2). Zero behavior change.
"""
from __future__ import annotations
import os, shutil, tempfile, time
from pathlib import Path
from typing import Any, Dict, Optional

import nova_state
import nova as _nova
log = _nova.log
HAS_GEMINI = _nova.HAS_GEMINI
GEMINI_API_KEY = _nova.GEMINI_API_KEY
VISION_MODEL = _nova.VISION_MODEL
genai = _nova.genai if HAS_GEMINI else None
_gemini_generate_with_delay = _nova._gemini_generate_with_delay
gtypes = _nova.gtypes if HAS_GEMINI else None
HAS_CV2 = _nova.HAS_CV2
cv2 = _nova.cv2 if HAS_CV2 else None
HAS_PIL = _nova.HAS_PIL
Image = _nova.Image if HAS_PIL else None
ImageGrab = _nova.ImageGrab if HAS_PIL else None
_io = _nova._io if HAS_PIL else None
HAS_PYTESSERACT = _nova.HAS_PYTESSERACT
_pytesseract = _nova._pytesseract if HAS_PYTESSERACT else None
REFERENCE_FACE_PATH = _nova.REFERENCE_FACE_PATH

def _capture_screen() -> Optional[Path]:
    if not HAS_PIL:
        return None
    try:
        screenshot = ImageGrab.grab()
        path = Path(tempfile.gettempdir()) / f"nova_screen_{int(time.time())}.png"
        screenshot.save(path)
        return path
    except Exception as e:
        log.error(f"Screen capture failed: {e}")
        return None


def _capture_camera() -> Optional[Path]:
    if not HAS_CV2:
        return None
    try:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None
        for _ in range(5):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None
        path = Path(tempfile.gettempdir()) / f"nova_camera_{int(time.time())}.png"
        cv2.imwrite(str(path), frame)
        return path
    except Exception as e:
        log.error(f"Camera capture failed: {e}")
        return None


def _detect_faces(image_path: Path) -> Dict[str, Any]:
    if not HAS_CV2:
        return {"count": 0, "error": "cv2 not available"}
    try:
        # cv2.data exists at runtime; Pylance stubs are incomplete
        cascade_file = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"  # type: ignore[attr-defined]
        face_cascade = cv2.CascadeClassifier(cascade_file)
        img = cv2.imread(str(image_path))
        if img is None:
            return {"count": 0, "error": "Could not load image"}
        gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        count = len(faces)
        locations = (
            [{"x": int(x), "y": int(y), "w": int(w), "h": int(h)} for (x, y, w, h) in faces]
            if count > 0 else []
        )
        return {"count": count, "locations": locations}
    except Exception as e:
        return {"count": 0, "error": str(e)}


def _gemini_vision(image_path: Path, question: str) -> str:
    """
    Analyse an image using Gemini generateContent.
    Uses VISION_MODEL (default: gemini-flash-latest). Override via NOVA_VISION_MODEL env var.
    Respects shared REST rate-limit backoff — skips call if quota is exhausted.
    """
    if not HAS_GEMINI or not GEMINI_API_KEY:
        return "Gemini vision unavailable (no API key)."
    if _nova._is_rate_limited():
        secs_left = int(nova_state._rest_backoff_until - time.time())
        return (
            f"Vision temporarily unavailable — Gemini REST quota exhausted. "
            f"Ready again in ~{secs_left}s. "
            "Tip: add billing at console.cloud.google.com to remove the limit."
        )
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        if HAS_PIL:
            img = Image.open(image_path)
            max_dim = 1024
            if img.width > max_dim or img.height > max_dim:
                ratio    = min(max_dim / img.width, max_dim / img.height)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            image_bytes = buf.getvalue()
        else:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
        response = _gemini_generate_with_delay(
            client,
            model=VISION_MODEL,
            contents=[question, gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png")]
        )
        _nova._reset_rate_limit()
        text = ""
        try:
            text = (response.text or "").strip()
        except Exception:
            text = ""
        if text:
            return text
        # An empty vision response is a failure, not a description. Returning ""
        # produced the bare header "Looking at your screen:" with nothing under
        # it, which reads to the user (and to the model in the follow-up round)
        # as a successful analysis. Report why it was empty instead.
        reason = ""
        try:
            cand = (getattr(response, "candidates", None) or [None])[0]
            reason = str(getattr(cand, "finish_reason", "") or "")
            if not reason:
                fb = getattr(response, "prompt_feedback", None)
                reason = str(getattr(fb, "block_reason", "") or "")
        except Exception:
            pass
        log.warning("[VISION] empty response from %s (finish_reason=%r)", VISION_MODEL, reason)
        return (
            "The vision model returned no description"
            + (f" (finish reason: {reason})" if reason else "")
            + ". The screenshot was captured but could not be analysed."
        )
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            _nova._record_rate_limit()
            secs_left = int(nova_state._rest_backoff_until - time.time())
            return (
                f"Vision quota exhausted — Gemini REST rate-limited. "
                f"Retrying automatically in ~{secs_left}s."
            )
        log.error(f"Gemini vision error: {e}")
        return f"Vision analysis failed: {e}"


def _vision_analyze(
    angle: str = "screen",
    question: str = "Describe what you see.",
    ocr_only: bool = False,
    save: bool = False,
    detect_faces: bool = False,
    save_reference: bool = False,
    identify_user: bool = False,
) -> str:
    source     = "camera" if angle == "camera" else "screen"
    image_path = _capture_camera() if angle == "camera" else _capture_screen()

    if image_path is None:
        if angle == "camera":
            return (
                "Camera not available. Check cv2 is installed "
                "(pip install opencv-python) and a webcam is connected."
            )
        return "Screen capture failed. Check PIL is installed (pip install pillow)."

    if save:
        dest = Path.home() / "Desktop" / f"nova_{source}_{int(time.time())}.png"
        try:
            shutil.copy(image_path, dest)
        except Exception:
            pass

    if save_reference:
        try:
            shutil.copy(image_path, REFERENCE_FACE_PATH)
            result = "Reference face saved. I can now identify you in future camera checks."
        except Exception as e:
            result = f"Could not save reference face: {e}"
        try:
            os.remove(image_path)
        except Exception:
            pass
        return result

    if identify_user:
        if not REFERENCE_FACE_PATH.exists():
            try:
                os.remove(image_path)
            except Exception:
                pass
            return "No reference face stored. Ask me to 'save a photo of me' first so I can recognise you."
        try:
            ref_bytes = REFERENCE_FACE_PATH.read_bytes()
            cam_bytes = image_path.read_bytes()
            client    = genai.Client(api_key=GEMINI_API_KEY)
            response  = _gemini_generate_with_delay(
                client,
                model=VISION_MODEL,
                contents=[
                    (
                        "I will show you two images. Image 1 is my stored reference photo of the user. "
                        "Image 2 is a new camera capture. Are they the same person? "
                        "Answer with YES or NO and a brief reason."
                    ),
                    gtypes.Part.from_bytes(data=ref_bytes, mime_type="image/jpeg"),
                    gtypes.Part.from_bytes(data=cam_bytes, mime_type="image/png"),
                ]
            )
            # FIX: guard against response.text being None
            result = f"Identity check: {(response.text or '').strip()}"
        except Exception as e:
            result = f"Identity check failed: {e}"
        try:
            os.remove(image_path)
        except Exception:
            pass
        return result

    if detect_faces:
        fd = _detect_faces(image_path)
        try:
            os.remove(image_path)
        except Exception:
            pass
        if "error" in fd:
            return f"Face detection error: {fd['error']}"
        count = fd["count"]
        if count == 0:
            return "No faces detected in the camera view."
        elif count == 1:
            return "One face detected. Looks like someone is there."
        else:
            return f"{count} faces detected in the camera view."

    if ocr_only:
        ocr_question = (
            "Extract ALL text visible in this image exactly as it appears. "
            "Preserve line breaks and formatting. Return only the extracted text — "
            "no commentary, no explanation."
        )
        if HAS_GEMINI and GEMINI_API_KEY:
            result = _gemini_vision(image_path, ocr_question)
        elif HAS_PYTESSERACT:
            try:
                from PIL import Image as _PILImage
                text   = _pytesseract.image_to_string(_PILImage.open(image_path)).strip()
                result = f"Text extracted:\n{text or 'No text detected.'}"
            except Exception as e:
                result = f"OCR failed: {e}"
        else:
            result = (
                "OCR unavailable: no Gemini API key and pytesseract not installed. "
                "Add GEMINI_API_KEY to .env or run: pip install pytesseract"
            )
        try:
            os.remove(image_path)
        except Exception:
            pass
        return f"Text from {source}:\n{result}"

    if not question:
        question = "Describe in detail what you see."
    result = _gemini_vision(image_path, question)
    full   = f"Looking at your {source}:\n{result}"
    try:
        os.remove(image_path)
    except Exception:
        pass
    return full


