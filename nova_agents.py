"""
NOVA Autonomous Agent System — v3.4
Browser Agent | Meeting Agent | Surveillance Agent | Spawn Agent
"""

import os
import time
import json
import re
import tempfile
import hashlib
import shutil
import threading
import importlib.util
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Optional, Dict, List, Set, Any
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import requests

# Import from main nova module (circular import handled by lazy import)
_vision_analyze = None
_execute_tool_sync = None
_speak_offline = None
HAS_GEMINI = False
GEMINI_API_KEY = None
HAS_PIL = False
HAS_PYTESSERACT = False
HAS_FASTER_WHISPER = False
_stt_model = None
GROQ_API_KEY = None
GROQ_MODEL = "llama-3.3-70b-versatile"


def _init_refs():
    """Lazy import to avoid circular dependency."""
    global _vision_analyze, _execute_tool_sync, _speak_offline
    global HAS_GEMINI, GEMINI_API_KEY, HAS_PIL, HAS_PYTESSERACT
    global HAS_FASTER_WHISPER, _stt_model, GROQ_API_KEY, GROQ_MODEL
    try:
        import nova
        # Use getattr to avoid static-analysis errors if attributes aren't present
        _vision_analyze = getattr(nova, '_vision_analyze', None)
        _execute_tool_sync = getattr(nova, '_execute_tool_sync', None)
        _speak_offline = getattr(nova, 'speak_offline', None)
        HAS_GEMINI = getattr(nova, 'HAS_GEMINI', False)
        GEMINI_API_KEY = getattr(nova, 'GEMINI_API_KEY', None)
        HAS_PIL = getattr(nova, 'HAS_PIL', False)
        HAS_PYTESSERACT = getattr(nova, 'HAS_PYTESSERACT', False)
        HAS_FASTER_WHISPER = getattr(nova, 'HAS_FASTER_WHISPER', False)
        _stt_model = getattr(nova, '_stt_model', None)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  BASE TYPES — defined here; nova.py imports from this file
# ══════════════════════════════════════════════════════════════════════════════

class AgentType(Enum):
    ORCHESTRATOR = "orchestrator"
    VISION       = "vision"
    CREATIVE     = "creative"
    RESEARCH     = "research"
    CODE         = "code"
    MEMORY       = "memory"
    BROWSER      = "browser"
    MEETING      = "meeting"
    SURVEILLANCE = "surveillance"
    SPAWN        = "spawn"


@dataclass
class AgentTask:
    task_id:     str
    agent_type:  AgentType
    description: str
    context:     Dict[str, Any] = field(default_factory=dict)
    result:      Optional[str]  = None
    status:      str            = "pending"


class BaseAgent:
    def __init__(self, agent_type: AgentType, system_prompt: str) -> None:
        self.agent_type    = agent_type
        self.system_prompt = system_prompt
        self.tools: Set[str] = set()

    def can_handle(self, task_description: str) -> float:
        return 0.0

    def execute(self, task: Any, meta: dict) -> str:
        desc = task.description if hasattr(task, "description") else task.get("description", "")
        return f"Agent {self.agent_type.value} executed: {desc}"


# ══════════════════════════════════════════════════════════════════════════════
#  BROWSER AGENT
# ══════════════════════════════════════════════════════════════════════════════

class BrowserAgent(BaseAgent):
    """Autonomous web browser using Playwright."""

    def __init__(self) -> None:
        super().__init__(AgentType.BROWSER, "Browser Agent")
        self.playwright_available = importlib.util.find_spec("playwright") is not None

    def can_handle(self, task_description: str) -> float:
        keywords = ["go to", "visit", "browse", "check", "look at", "instagram", "twitter",
                    "facebook", "linkedin", "website", "page", "post", "url", "link",
                    "scroll", "click", "navigate", "web", "site", "online", "profile"]
        return sum(1 for k in keywords if k in task_description.lower()) / len(keywords)

    def execute(self, task: Any, meta: dict) -> str:
        if not self.playwright_available:
            return "Browser Agent: Playwright not installed. Run: pip install playwright && playwright install chromium"

        desc = task.description if hasattr(task, "description") else str(task.get("description", ""))

        # FIX: replaced type('obj',...) hack with a plain Optional[str]
        url_match = re.search(r'(https?://\S+)', desc)
        final_url: Optional[str] = url_match.group(1) if url_match else None

        if not final_url:
            domains = {
                "instagram": "https://instagram.com",
                "twitter":   "https://twitter.com",
                "x.com":     "https://x.com",
                "facebook":  "https://facebook.com",
                "linkedin":  "https://linkedin.com",
                "youtube":   "https://youtube.com",
                "github":    "https://github.com",
            }
            for kw, url in domains.items():
                if kw in desc.lower():
                    final_url = url
                    break

        if not final_url:
            return "Browser Agent: Please provide a URL or website name."

        return self._browse_and_analyze(final_url, desc)

    def _browse_and_analyze(self, url: str, query: str) -> str:
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)

                screenshot_path = Path(tempfile.gettempdir()) / f"nova_browser_{int(time.time())}.png"
                page.screenshot(path=str(screenshot_path), full_page=False)
                page_text = page.evaluate("() => document.body.innerText")[:3000]
                links  = page.evaluate("""() => Array.from(document.querySelectorAll('a')).slice(0,20).map(a => ({text: a.innerText?.trim()?.substring(0,100), href: a.href}))""")
                images = page.evaluate("""() => Array.from(document.querySelectorAll('img')).slice(0,10).map(img => ({src: img.src, alt: img.alt?.substring(0,100)}))""")
                browser.close()

                analysis = self._analyze_screenshot(screenshot_path, query, page_text)
                try:
                    os.remove(screenshot_path)
                except Exception:
                    pass

                return (
                    f"Browser Agent: {analysis}\n\n"
                    f"Page text: {page_text[:500]}...\n\n"
                    f"Found {len(links)} links, {len(images)} images."
                )
        except Exception as e:
            return f"Browser Agent: Browse failed: {e}"

    def _analyze_screenshot(self, screenshot_path: Path, query: str, page_text: str) -> str:
        _init_refs()
        if not (HAS_GEMINI and GEMINI_API_KEY):
            return "Screenshot captured. (Vision analysis requires Gemini API key)"
        try:
            from google import genai
            from google.genai import types as gtypes
            client = genai.Client(api_key=GEMINI_API_KEY)
            with open(screenshot_path, "rb") as f:
                img_bytes = f.read()
            prompt = (
                f"Analyze this webpage. User asked: {query}\n\n"
                f"Page text: {page_text[:1000]}\n\n"
                "Describe what you see, extract key info, and suggest implementations."
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash-preview-05-20",
                contents=[prompt, gtypes.Part.from_bytes(data=img_bytes, mime_type="image/png")]
            )
            # FIX: guard against response.text being None
            return (response.text or "").strip()
        except Exception as e:
            return f"Screenshot captured but analysis failed: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  MEETING AGENT
# ══════════════════════════════════════════════════════════════════════════════

class MeetingAgent(BaseAgent):
    """Silent meeting transcription and analysis."""

    def __init__(self) -> None:
        super().__init__(AgentType.MEETING, "Meeting Agent")
        self.is_recording = False
        self.transcript_segments: List[Dict[str, Any]] = []
        self._record_thread: Optional[threading.Thread] = None

    def can_handle(self, task_description: str) -> float:
        keywords = ["meeting", "zoom", "teams", "meet", "transcribe", "attend", "join",
                    "record", "notes", "minutes", "conference", "call", "discussion",
                    "listen in", "take notes", "summarize meeting"]
        return sum(1 for k in keywords if k in task_description.lower()) / len(keywords)

    def execute(self, task: Any, meta: dict) -> str:
        desc = (task.description if hasattr(task, "description") else str(task.get("description", ""))).lower()
        if "start" in desc or "begin" in desc or "join" in desc:
            return self._start_recording()
        elif "stop" in desc or "end" in desc or "finish" in desc:
            return self._stop_recording()
        elif "status" in desc:
            return self._status()
        return "Meeting Agent: I can join meetings silently and transcribe. Say 'start meeting recording' to begin."

    def _start_recording(self) -> str:
        if self.is_recording:
            return "Meeting Agent: Already recording."
        self.is_recording = True
        self.transcript_segments = []
        self._record_thread = threading.Thread(target=self._record_loop, daemon=True)
        self._record_thread.start()
        return "Meeting Agent: Recording started. I will transcribe and summarize. Say 'stop meeting recording' when done."

    def _stop_recording(self) -> str:
        if not self.is_recording:
            return "Meeting Agent: Not recording."
        self.is_recording = False
        if self._record_thread:
            self._record_thread.join(timeout=2)
        summary = self._generate_summary()
        transcript_path = Path.home() / "Desktop" / f"nova_meeting_{int(time.time())}.txt"
        try:
            with open(transcript_path, "w", encoding="utf-8") as f:
                f.write(
                    f"NOVA Meeting Transcript\n"
                    f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{'='*50}\n\n"
                )
                for seg in self.transcript_segments:
                    f.write(f"[{seg.get('time', '??:??')}] {seg.get('text', '')}\n")
                f.write(f"\n{'='*50}\n\nSUMMARY:\n{summary}\n")
        except Exception as e:
            print(f"Failed to save transcript: {e}")
        return (
            f"Meeting Agent: Recording stopped.\n"
            f"📄 Saved to: {transcript_path}\n\n"
            f"📝 SUMMARY:\n{summary}"
        )

    def _record_loop(self) -> None:
        from scipy.io.wavfile import write as wav_write
        import sounddevice as sd
        fs             = 16000
        chunk_duration = 10
        chunk_samples  = fs * chunk_duration
        while self.is_recording:
            try:
                audio = sd.rec(chunk_samples, samplerate=fs, channels=1, dtype="float32")
                sd.wait()
                if not self.is_recording:
                    break
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp_path = tmp.name
                    wav_write(tmp_path, fs, (audio * 32767).astype(np.int16))
                _init_refs()
                if HAS_FASTER_WHISPER and _stt_model is not None:
                    try:
                        segments, _ = _stt_model.transcribe(
                            tmp_path, language="en", vad_filter=True,
                            vad_parameters=dict(min_silence_duration_ms=500),
                        )
                        for seg in segments:
                            self.transcript_segments.append({
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "text": seg.text.strip(),
                            })
                    except Exception:
                        pass
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            except Exception as e:
                print(f"Recording error: {e}")
                time.sleep(5)

    def _generate_summary(self) -> str:
        if not self.transcript_segments:
            return "No transcript captured."
        full_text = " ".join([seg["text"] for seg in self.transcript_segments])
        _init_refs()
        if GROQ_API_KEY:
            try:
                prompt = (
                    "Summarize this meeting. Extract decisions, action items, deadlines, next steps.\n\n"
                    f"{full_text[:4000]}"
                )
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}]},
                    timeout=15,
                )
                return r.json()["choices"][0]["message"]["content"].strip()
            except Exception:
                pass
        sentences  = full_text.split(". ")
        key_points = [
            s for s in sentences
            if any(kw in s.lower() for kw in [
                "decide", "decision", "action", "task", "deadline", "due",
                "next", "follow up", "agree", "confirm", "need to", "should", "must",
            ])
        ]
        return "Meeting Summary:\n" + "\n".join(f"• {s}" for s in key_points[:10])

    def _status(self) -> str:
        if self.is_recording:
            return f"Meeting Agent: Recording. {len(self.transcript_segments)} segments."
        return "Meeting Agent: Not recording."


# ══════════════════════════════════════════════════════════════════════════════
#  SURVEILLANCE AGENT
# ══════════════════════════════════════════════════════════════════════════════

class SurveillanceAgent(BaseAgent):
    """Continuous screen/audio monitoring with smart triggers."""

    def __init__(self) -> None:
        super().__init__(AgentType.SURVEILLANCE, "Surveillance Agent")
        self.is_monitoring = False
        self.trigger_keywords: Set[str] = {"urgent", "error", "alert", "asap", "emergency", "critical", "failed"}
        self.event_log: deque = deque(maxlen=100)
        self._monitor_thread: Optional[threading.Thread] = None

    def can_handle(self, task_description: str) -> float:
        keywords = ["watch", "monitor", "surveillance", "record", "capture", "detect",
                    "screen", "audio", "listen", "alert", "notify", "track", "observe",
                    "silent", "background", "continuous", "auto"]
        return sum(1 for k in keywords if k in task_description.lower()) / len(keywords)

    def execute(self, task: Any, meta: dict) -> str:
        desc = (task.description if hasattr(task, "description") else str(task.get("description", ""))).lower()
        if "start" in desc or "enable" in desc or "begin" in desc:
            return self._start()
        elif "stop" in desc or "disable" in desc or "end" in desc:
            return self._stop()
        elif "status" in desc or "log" in desc:
            return self._status()
        elif "keyword" in desc or "trigger" in desc:
            raw = task.description if hasattr(task, "description") else str(task.get("description", ""))
            return self._set_triggers(raw)
        return "Surveillance Agent: I can monitor screen/audio silently. Say 'start surveillance' to begin."

    def _start(self) -> str:
        if self.is_monitoring:
            return "Surveillance Agent: Already monitoring."
        self.is_monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        return f"Surveillance Agent: 👁️ Monitoring started. Watching for: {', '.join(self.trigger_keywords)}"

    def _stop(self) -> str:
        if not self.is_monitoring:
            return "Surveillance Agent: Not monitoring."
        self.is_monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)
        return f"Surveillance Agent: Monitoring stopped.\n{self._report()}"

    def _monitor_loop(self) -> None:
        last_hash     = ""
        check_interval = 5
        while self.is_monitoring:
            try:
                _init_refs()
                if HAS_PIL:
                    from PIL import ImageGrab
                    screen      = ImageGrab.grab()
                    screen_bytes = screen.tobytes()
                    current_hash = hashlib.md5(screen_bytes).hexdigest()[:16]
                    if last_hash and current_hash != last_hash:
                        self._check_screen(screen)
                    last_hash = current_hash
                audio_level = self._check_audio()
                if audio_level > 0.5:
                    self._log_event("AUDIO_ANOMALY", f"Loud audio: {audio_level:.2f}")
                time.sleep(check_interval)
            except Exception as e:
                print(f"Surveillance error: {e}")
                time.sleep(check_interval)

    def _check_screen(self, screen: Any) -> None:
        temp_path = Path(tempfile.gettempdir()) / f"nova_surv_{int(time.time())}.png"
        screen.save(temp_path)
        if HAS_PYTESSERACT:
            try:
                import pytesseract
                text = pytesseract.image_to_string(screen).lower()
                for kw in self.trigger_keywords:
                    if kw in text:
                        self._log_event("KEYWORD", f"'{kw}' on screen")
                        self._save_screenshot(temp_path, f"kw_{kw}")
                        break
            except Exception:
                pass
        try:
            os.remove(temp_path)
        except Exception:
            pass

    def _check_audio(self) -> float:
        try:
            import sounddevice as sd
            chunk = sd.rec(1024, samplerate=16000, channels=1, dtype="float32")
            sd.wait()
            return float(np.sqrt(np.mean(chunk ** 2)))
        except Exception:
            return 0.0

    def _log_event(self, event_type: str, description: str) -> None:
        self.event_log.append({
            "time":        datetime.now().isoformat(),
            "type":        event_type,
            "description": description,
        })
        print(f"👁️  [{event_type}] {description}")
        _init_refs()
        if _speak_offline:
            try:
                _speak_offline(f"Alert: {description}")
            except Exception:
                pass

    def _save_screenshot(self, path: Path, name: str) -> None:
        try:
            dest = Path.home() / "Desktop" / f"nova_event_{name}_{int(time.time())}.png"
            shutil.copy(path, dest)
            print(f"📸 Saved: {dest}")
        except Exception as e:
            print(f"Save failed: {e}")

    def _report(self) -> str:
        if not self.event_log:
            return "No events detected."
        lines = ["Event Report:", "=" * 40]
        for ev in self.event_log:
            lines.append(f"[{str(ev['time'])[11:19]}] {ev['type']}: {ev['description']}")
        return "\n".join(lines)

    def _status(self) -> str:
        if self.is_monitoring:
            return f"Active. {len(self.event_log)} events. Triggers: {', '.join(self.trigger_keywords)}"
        return "Inactive."

    def _set_triggers(self, description: str) -> str:
        words = re.findall(r'"([^"]+)"', description)
        if words:
            self.trigger_keywords.update(w.lower() for w in words)
            return f"Triggers updated: {', '.join(self.trigger_keywords)}"
        return 'Use quotes: add trigger "deadline" "urgent"'


# ══════════════════════════════════════════════════════════════════════════════
#  SPAWN AGENT
# ══════════════════════════════════════════════════════════════════════════════

class SpawnAgent(BaseAgent):
    """Creates lightweight sub-LLMs for robotics and IoT."""

    def __init__(self) -> None:
        super().__init__(AgentType.SPAWN, "Spawn Agent")

    def can_handle(self, task_description: str) -> float:
        keywords = ["robot", "robotics", "iot", "esp32", "arduino", "raspberry", "jetson",
                    "microcontroller", "edge device", "sensor", "motor", "actuator",
                    "spawn", "create agent", "sub-agent", "deploy", "embedded",
                    "autonomous robot", "smart device", "hardware"]
        return sum(1 for k in keywords if k in task_description.lower()) / len(keywords)

    def execute(self, task: Any, meta: dict) -> str:
        desc = (task.description if hasattr(task, "description") else str(task.get("description", ""))).lower()
        if "create" in desc or "spawn" in desc or "make" in desc:
            raw = task.description if hasattr(task, "description") else str(task.get("description", ""))
            return self._spawn(raw)
        elif "list" in desc or "show" in desc:
            return self._list_templates()
        return "Spawn Agent: I create sub-LLMs for robots/IoT. Say 'spawn a navigation robot agent' to create one."

    def _spawn(self, description: str) -> str:
        platform = "raspberry_pi"
        if "esp32" in description.lower():
            platform = "esp32"
        elif "arduino" in description.lower():
            platform = "arduino"
        elif "jetson" in description.lower():
            platform = "jetson"

        purpose = "general"
        if any(w in description.lower() for w in ["navigate", "move"]):
            purpose = "navigation"
        elif any(w in description.lower() for w in ["vision", "see", "camera"]):
            purpose = "vision"
        elif any(w in description.lower() for w in ["arm", "grab", "pick"]):
            purpose = "manipulation"
        elif any(w in description.lower() for w in ["chat", "speak", "voice"]):
            purpose = "voice_assistant"
        elif any(w in description.lower() for w in ["sensor", "monitor"]):
            purpose = "sensor_hub"

        models = {
            "raspberry_pi": "Qwen2.5-0.5B-Instruct",
            "jetson":       "Llama-3.2-1B-Instruct",
            "esp32":        "TinyLlama-1.1B-q4",
            "arduino":      "TFLite Micro",
        }
        model = models.get(platform, "Qwen2.5-0.5B-Instruct")

        prompts = {
            "navigation":      "You are a robot navigation agent. Process sensor data and output movement commands in JSON.",
            "vision":          "You are a robot vision agent. Analyze camera feed and report objects with coordinates.",
            "manipulation":    "You are a robot arm control agent. Plan grasps and output joint commands.",
            "voice_assistant": "You are an embedded voice assistant. Keep responses under 2 sentences.",
            "sensor_hub":      "You are a sensor monitoring agent. Read sensors and report status in JSON.",
            "general":         "You are a general-purpose edge AI agent. Process inputs, make decisions, control outputs.",
        }
        prompt = prompts.get(purpose, prompts["general"])

        code_templates = {
            "raspberry_pi": (
                "import json, time, requests\n# Robot agent main loop\n"
                "while True:\n    data = read_sensors()\n"
                "    resp = requests.post('http://localhost:11434/api/generate',\n"
                f"        json={{'model': 'qwen2.5:0.5b', 'prompt': f'{prompt}\\n\\n{{data}}', 'stream': False}})\n"
                "    action = json.loads(resp.json()['response'])\n    execute(action)\n    time.sleep(1)"
            ),
            "esp32": (
                "# MicroPython for ESP32-S3\nimport machine, time, network\n"
                "# Connect WiFi, read sensors, send via MQTT\n"
                "while True:\n    data = read_sensors()\n"
                "    mqtt.publish('nova/sensors', json.dumps(data))\n    time.sleep(5)"
            ),
            "jetson": (
                "import jetson.inference, jetson.utils, requests\n"
                "net    = jetson.inference.detectNet('ssd-mobilenet-v2')\n"
                "camera = jetson.utils.videoSource('/dev/video0')\n"
                "while True:\n    img = camera.Capture()\n    detections = net.Detect(img)\n    print(detections)"
            ),
        }
        code = code_templates.get(platform, code_templates["raspberry_pi"])

        reqs = {
            "raspberry_pi": ["requests", "numpy", "pyserial"],
            "jetson":       ["jetson-inference"],
            "esp32":        [],
            "arduino":      [],
        }
        requirements = reqs.get(platform, ["requests"])

        package_dir = Path.home() / "Desktop" / f"nova_spawn_{purpose}_{int(time.time())}"
        package_dir.mkdir(exist_ok=True)

        config = {
            "name":    f"nova_{purpose}_agent",
            "platform": platform,
            "purpose":  purpose,
            "model":    model,
            "system_prompt": prompt,
            "loop_delay":    1.0,
            "communication": {
                "protocol": "mqtt",
                "broker":   "nova.local",
                "topic":    f"nova/agents/{purpose}",
            },
            "safety": {
                "emergency_stop": True,
                "max_speed":      0.5,
                "human_override": True,
            },
        }

        (package_dir / "agent_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        (package_dir / "system_prompt.txt").write_text(prompt, encoding="utf-8")
        (package_dir / "main.py").write_text(code, encoding="utf-8")
        (package_dir / "requirements.txt").write_text("\n".join(requirements), encoding="utf-8")

        readme = (
            f"# NOVA Spawn Agent — {purpose.title()} Agent\n"
            f"**Platform:** {platform} | **Model:** {model}\n"
            f"**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "## Setup\n1. Install: `pip install -r requirements.txt`\n"
            "2. Configure `agent_config.json`\n3. Run: `python main.py`\n\n"
            f"## Communication\n- MQTT protocol to NOVA main system\n- Topic: `nova/agents/{purpose}`\n\n"
            "## Safety\n- Emergency stop enabled\n- Human override active\n- Test in simulation first\n"
        )
        (package_dir / "README.md").write_text(readme, encoding="utf-8")

        return (
            f"Spawn Agent: Created {purpose} agent for {platform}.\n"
            f"📦 Package: {package_dir}\n\n"
            f"Model: {model}\nPurpose: {purpose}\nIncludes: config, code, requirements, README"
        )

    def _list_templates(self) -> str:
        return (
            "Spawn Agent: Available templates:\n"
            "• Navigation Agent — obstacle avoidance, path planning\n"
            "• Vision Agent — object detection, face recognition\n"
            "• Manipulation Agent — grasp planning, arm control\n"
            "• Voice Assistant Agent — wake word, command parsing\n"
            "• Sensor Hub Agent — multi-sensor monitoring, alerting\n\n"
            "Say 'spawn a [template] agent for [platform]' to create."
        )


# ══════════════════════════════════════════════════════════════════════════════
#  AGENT REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

def get_all_agents() -> Dict[AgentType, BaseAgent]:
    """Return all available autonomous agents keyed by AgentType."""
    return {
        AgentType.BROWSER:      BrowserAgent(),
        AgentType.MEETING:      MeetingAgent(),
        AgentType.SURVEILLANCE: SurveillanceAgent(),
        AgentType.SPAWN:        SpawnAgent(),
    }