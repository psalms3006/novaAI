"""
NOVA Action: vision
Captures screenshot or webcam frame, then analyses it with a vision LLM.

Online  → Groq Vision (llama-3.2-11b-vision-preview) — fast, free tier
Offline → Ollama LLaVA — local, no internet needed

Install dependencies:
  pip install pillow opencv-python
  ollama pull llava          ← for offline vision (~4GB)
"""

import os
import base64
import tempfile
import logging
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_VIS_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct" 
OLLAMA_VIS_MODEL = os.getenv("VISION_MODEL", "llava")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")


# ════════════════════════════════════════════════════════════════════════════
#  IMAGE CAPTURE
# ════════════════════════════════════════════════════════════════════════════

def _capture_screenshot() -> str | None:
    """
    Capture the full screen. Returns path to saved PNG or None on failure.
    Tries PIL.ImageGrab first (built-in), then pyautogui as fallback.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(tempfile.gettempdir()) / f"nova_screen_{timestamp}.png"

    # Method 1: PIL ImageGrab (Windows/Mac — no extra install beyond Pillow)
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img.save(str(dest))
        log.info(f"Screenshot saved: {dest}")
        return str(dest)
    except ImportError:
        log.warning("Pillow not installed: pip install pillow")
    except Exception as e:
        log.warning(f"PIL screenshot failed: {e}")

    # Method 2: pyautogui fallback
    try:
        import pyautogui
        img = pyautogui.screenshot()
        img.save(str(dest))
        log.info(f"Screenshot saved (pyautogui): {dest}")
        return str(dest)
    except ImportError:
        log.warning("pyautogui not installed: pip install pyautogui")
    except Exception as e:
        log.warning(f"pyautogui screenshot failed: {e}")

    # Method 3: mss fallback (fastest)
    try:
        import mss
        with mss.mss() as sct:
            sct.shot(output=str(dest))
        return str(dest)
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"mss screenshot failed: {e}")

    return None


def _capture_webcam(camera_index: int = 0) -> str | None:
    """
    Capture a single frame from the webcam.
    Returns path to saved PNG or None if webcam unavailable.
    """
    try:
        import cv2
    except ImportError:
        log.error("OpenCV not installed. Run: pip install opencv-python")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(tempfile.gettempdir()) / f"nova_cam_{timestamp}.png"

    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        log.error(f"Cannot open webcam (index {camera_index})")
        cap.release()
        return None

    try:
        # Discard first few frames — some cameras take time to warm up
        for _ in range(3):
            cap.read()

        ret, frame = cap.read()
        if not ret or frame is None:
            log.error("Webcam returned empty frame.")
            return None

        cv2.imwrite(str(dest), frame)
        log.info(f"Webcam frame saved: {dest}")
        return str(dest)
    except Exception as e:
        log.error(f"Webcam capture failed: {e}")
        return None
    finally:
        cap.release()


# ════════════════════════════════════════════════════════════════════════════
#  IMAGE → BASE64
# ════════════════════════════════════════════════════════════════════════════

def _image_to_base64(image_path: str) -> str | None:
    """Encode image file to base64 string."""
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        log.error(f"base64 encoding failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
#  VISION ANALYSIS — GROQ (Online)
# ════════════════════════════════════════════════════════════════════════════

def _analyse_groq(image_path: str, question: str) -> str | None:
    """
    Send image to Groq Vision API.
    Uses llama-3.2-11b-vision-preview (free tier, fast).
    """
    if not GROQ_API_KEY:
        log.warning("No GROQ_API_KEY — skipping Groq Vision.")
        return None

    b64 = _image_to_base64(image_path)
    if not b64:
        return None

    # Detect image format from extension
    ext = Path(image_path).suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp"}.get(ext, "image/png")

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json"
            },
            json={
                "model": GROQ_VIS_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{b64}"
                                }
                            },
                            {
                                "type": "text",
                                "text": question
                            }
                        ]
                    }
                ],
                "max_tokens": 600
            },
            timeout=15
        )
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"].strip()
        log.info("Groq Vision responded.")
        return result

    except requests.exceptions.Timeout:
        log.warning("Groq Vision timed out.")
        return None
    except requests.exceptions.HTTPError as e:
        response = e.response
        if response is not None:
            log.warning(
                f"Groq Vision HTTP error: {response.status_code} — {response.text[:200]}")
        else:
            log.warning(f"Groq Vision HTTP error: {e}")
        return None
    except Exception as e:
        log.warning(f"Groq Vision failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
#  VISION ANALYSIS — OLLAMA LLAVA (Offline)
# ════════════════════════════════════════════════════════════════════════════

def _analyse_ollama(image_path: str, question: str) -> str | None:
    """
    Send image to local Ollama LLaVA model.
    Requires: ollama pull llava
    """
    b64 = _image_to_base64(image_path)
    if not b64:
        return None

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model":  OLLAMA_VIS_MODEL,
                "stream": False,
                "messages": [
                    {
                        "role":    "user",
                        "content": question,
                        "images":  [b64]
                    }
                ]
            },
            timeout=120   # LLaVA is slower on CPU
        )
        response.raise_for_status()
        result = response.json()["message"]["content"].strip()
        log.info(f"Ollama {OLLAMA_VIS_MODEL} Vision responded.")
        return result

    except requests.exceptions.ConnectionError:
        log.warning("Ollama not running — cannot use offline vision.")
        return None
    except requests.exceptions.Timeout:
        log.warning(f"Ollama {OLLAMA_VIS_MODEL} timed out.")
        return None
    except Exception as e:
        log.warning(f"Ollama Vision failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
#  OCR — Read text visible in image (optional, no LLM needed)
# ════════════════════════════════════════════════════════════════════════════

def _ocr_image(image_path: str) -> str | None:
    """
    Extract text from image using pytesseract (optional).
    Install: pip install pytesseract  +  install Tesseract-OCR binary
    https://github.com/tesseract-ocr/tesseract
    """
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img).strip()
        return text if text else "No readable text found in image."
    except ImportError:
        return None
    except Exception as e:
        log.warning(f"OCR failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
#  CLEANUP
# ════════════════════════════════════════════════════════════════════════════

def _cleanup(image_path: str) -> None:
    """Delete temporary image file after analysis."""
    try:
        if image_path and os.path.exists(image_path):
            os.remove(image_path)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def execute(args: dict) -> str:
    """
    Main vision tool entry point.

    Args:
        angle   : "screen" (default) | "camera"
        question: What to ask about the image (default: "Describe what you see")
        ocr_only: True to skip LLM and just extract text via OCR
        save    : True to keep image on Desktop instead of deleting it

    Examples:
        {"angle": "screen",  "question": "What error is on my screen?"}
        {"angle": "camera",  "question": "What is in front of me?"}
        {"angle": "screen",  "question": "Read the text on screen", "ocr_only": true}
    """
    angle = args.get("angle",    "screen").lower()
    question = args.get(
        "question", "Describe everything you see in this image clearly.")
    ocr_only = args.get("ocr_only", False)
    save = args.get("save",     False)

    # ── 1. Capture image ─────────────────────────────────────────────────────
    print(f"📷 Capturing {angle}...")

    if angle == "camera":
        image_path = _capture_webcam()
        if not image_path:
            return ("Webcam is not available or not connected. "
                    "Make sure it is plugged in and not in use by another app.")
    else:
        image_path = _capture_screenshot()
        if not image_path:
            return ("Screenshot failed. "
                    "Install Pillow: pip install pillow")

    print(f"✅ Image captured: {image_path}")

    # ── 2. Optional: save to Desktop ─────────────────────────────────────────
    if save:
        desktop_dest = Path.home() / "Desktop" / Path(image_path).name
        try:
            import shutil
            shutil.copy2(image_path, str(desktop_dest))
            print(f"💾 Saved to Desktop: {desktop_dest.name}")
        except Exception:
            pass

    # ── 3. OCR-only path (no LLM needed) ────────────────────────────────────
    if ocr_only:
        ocr_text = _ocr_image(image_path)
        _cleanup(image_path)
        if ocr_text:
            return f"Text found in image:\n{ocr_text}"
        return ("OCR is not available. "
                "Install it with: pip install pytesseract "
                "and then install Tesseract-OCR from https://github.com/tesseract-ocr/tesseract")

    # ── 4. Check internet for Groq ────────────────────────────────────────────
    is_online = False
    try:
        requests.get("https://google.com", timeout=3)
        is_online = True
    except Exception:
        pass

    # ── 5. Try Groq Vision (online) ──────────────────────────────────────────
    result = None

    if is_online and GROQ_API_KEY:
        print(f"🌐 [Online] Groq Vision analysing {angle}...")
        result = _analyse_groq(image_path, question)
        if result:
            print("✅ Groq Vision responded.")

    # ── 6. Fallback: Ollama LLaVA (offline) ─────────────────────────────────
    if not result:
        print(f"🔌 [Offline] Ollama {OLLAMA_VIS_MODEL} analysing {angle}...")
        result = _analyse_ollama(image_path, question)
        if result:
            print(f"✅ Ollama {OLLAMA_VIS_MODEL} responded.")

    # ── 7. Final fallback: OCR only ──────────────────────────────────────────
    if not result:
        print("📝 Vision LLM unavailable. Attempting OCR...")
        ocr_text = _ocr_image(image_path)
        if ocr_text:
            result = f"Vision AI is offline. Here is the raw text I extracted:\n{ocr_text}"
        else:
            result = (
                f"I captured the {angle} but cannot analyse it right now. "
                f"Groq Vision needs internet and LLaVA needs Ollama running with: "
                f"ollama pull llava"
            )

    # ── 8. Cleanup ────────────────────────────────────────────────────────────
    if not save:
        _cleanup(image_path)

    return result
