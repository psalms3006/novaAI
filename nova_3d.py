#!/usr/bin/env python3
"""
NOVA 3D Desktop Application
A futuristic 3D-responsive voice assistant UI.
Inspired by sci-fi HUDs but with a unique NOVA identity.

Features:
- Rotating 3D wireframe sphere with particle field
- Animated concentric HUD rings that respond to voice
- Glowing neon cyan/magenta color scheme
- System monitor panels (CPU, RAM, NET, GPU)
- Activity log with typewriter effect
- Full voice integration (STT + TTS + AI)
- Fullscreen support (F11)

Run: python nova_3d.py
"""

import sys
import os
import threading
import queue
import json
import time
import random
import math
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple

# --- Audio & Speech ---
try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import pyaudio
except ImportError:
    pyaudio = None

# --- AI Backend ---
try:
    import openai
except ImportError:
    openai = None

try:
    from groq import Groq
except ImportError:
    Groq = None

# --- GUI & 3D ---
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QLineEdit, QFrame, QComboBox, QSlider, QCheckBox, QSystemTrayIcon, QMenu, QSizePolicy
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QPointF, QRectF,
    pyqtSlot, Q_ARG
)
from PyQt6.QtGui import (
    QIcon, QFont, QFontDatabase, QColor, QLinearGradient,
    QPainter, QBrush, QPen, QPolygonF, QKeySequence, QShortcut,
    QRadialGradient, QPixmap
)

# ================================================================
# CONFIGURATION
# ================================================================

CONFIG_FILE = Path.home() / ".nova" / "config.json"
HISTORY_FILE = Path.home() / ".nova" / "history.json"
FONTS_DIR = Path.home() / ".nova" / "fonts"

def ensure_fonts():
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    # User should place Equinox-Regular.otf or .ttf here
    return FONTS_DIR

DEFAULT_CONFIG = {
    "mic_threshold": 0.05,
    "wake_word": "nova",
    "ai_provider": "groq",
    "groq_api_key": "",
    "openai_api_key": "",
    "groq_model": "llama3-8b-8192",
    "openai_model": "gpt-4o-mini",
    "voice_rate": 180,
    "voice_id": None,
    "theme": "nova_dark",
    "auto_start_listening": False,
    "save_history": True,
    "window_opacity": 0.98,
    "always_on_top": False,
    "fullscreen": False,
    "hud_color": "#00d4ff",
    "accent_color": "#ff00a0",
    "equinox_font_path": "",
}

# ================================================================
# AUDIO ENGINE
# ================================================================

class AudioEngine:
    def __init__(self, config):
        self.config = config
        self.tts_queue = queue.Queue()
        self.tts_thread = None
        self.tts_engine = None
        self.is_speaking = False
        self._init_tts()
        self._start_tts_thread()

    def _init_tts(self):
        if pyttsx3 is None:
            return
        try:
            self.tts_engine = pyttsx3.init()
            self.tts_engine.setProperty('rate', self.config.get('voice_rate', 180))
            voices = self.tts_engine.getProperty('voices')
            # Ensure voices is iterable (some environments may return a single object)
            if voices is None:
                voices_list = []
            elif hasattr(voices, '__iter__') and not isinstance(voices, (str, bytes)):
                try:
                    voices_list = list(voices)
                except Exception:
                    # Defensive fallback if voices isn't actually iterable at runtime
                    voices_list = [voices]
            else:
                voices_list = [voices]

            voice_id = self.config.get('voice_id')
            if voice_id and any(getattr(v, 'id', None) == voice_id for v in voices_list):
                self.tts_engine.setProperty('voice', voice_id)
            elif voices_list:
                for v in voices_list:
                    name = getattr(v, 'name', '') or ''
                    if 'female' in name.lower() or 'zira' in name.lower():
                        vid = getattr(v, 'id', None)
                        if vid is not None:
                            self.tts_engine.setProperty('voice', vid)
                            break
        except Exception as e:
            print(f"TTS init error: {e}")

    def _start_tts_thread(self):
        self.tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self.tts_thread.start()

    def _tts_worker(self):
        while True:
            text = self.tts_queue.get()
            if text is None:
                break
            self.is_speaking = True
            try:
                if self.tts_engine:
                    self.tts_engine.say(text)
                    self.tts_engine.runAndWait()
            except Exception as e:
                print(f"TTS error: {e}")
            self.is_speaking = False
            self.tts_queue.task_done()

    def speak(self, text):
        self.tts_queue.put(text)

    def stop(self):
        self.tts_queue.put(None)


# ================================================================
# AI BACKEND
# ================================================================

class AIBackend:
    def __init__(self, config):
        self.config = config
        self.client = None
        self._init_client()

    def _init_client(self):
        provider = self.config.get('ai_provider', 'groq')
        if provider == 'groq' and Groq:
            key = self.config.get('groq_api_key', '') or os.getenv('GROQ_API_KEY', '')
            if key:
                self.client = Groq(api_key=key)
        elif provider == 'openai' and openai:
            key = self.config.get('openai_api_key', '') or os.getenv('OPENAI_API_KEY', '')
            if key:
                self.client = openai.OpenAI(api_key=key)

    def chat(self, messages, model=None):
        if self.client is None:
            return "AI not configured. Please add your API key in Settings."
        provider = self.config.get('ai_provider', 'groq')
        if model is None:
            model = self.config.get(f'{provider}_model', 'llama3-8b-8192')
        try:
            response = self.client.chat.completions.create(
                model=model, messages=messages, max_tokens=1024, temperature=0.7
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"AI Error: {str(e)}"


# ================================================================
# VOICE RECOGNITION THREAD
# ================================================================

class VoiceThread(QThread):
    text_ready = pyqtSignal(str)
    listening_state = pyqtSignal(bool)
    error_signal = pyqtSignal(str)
    audio_level = pyqtSignal(float)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.running = True
        self.recognizer = None
        self.microphone = None
        self._init_recognizer()

    def _init_recognizer(self):
        if sr is None:
            return
        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold = self.config.get('mic_threshold', 0.05) * 4000
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.pause_threshold = 0.8
        try:
            self.microphone = sr.Microphone()
        except Exception as e:
            self.error_signal.emit(f"Microphone error: {e}")

    def run(self):
        if self.recognizer is None or self.microphone is None:
            self.error_signal.emit("Speech recognition not available")
            return
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1)
        while self.running:
            try:
                self.listening_state.emit(True)
                with self.microphone as source:
                    audio = self.recognizer.listen(source, timeout=None, phrase_time_limit=10)
                self.listening_state.emit(False)
                try:
                    recognize_google = getattr(self.recognizer, "recognize_google", None)
                    if recognize_google is None:
                        raise AttributeError("recognize_google not available")
                    text = recognize_google(audio)
                    if text:
                        self.text_ready.emit(text)
                except Exception as e:
                    if sr and isinstance(e, sr.UnknownValueError):
                        pass
                    elif sr and isinstance(e, sr.RequestError):
                        self.error_signal.emit(f"Speech API error: {e}")
                    elif isinstance(e, AttributeError):
                        self.error_signal.emit(f"Speech recognition error: {e}")
                    else:
                        pass
            except Exception as e:
                if self.running:
                    self.error_signal.emit(f"Listen error: {e}")
                time.sleep(0.5)

    def stop(self):
        self.running = False
        self.wait(2000)


# ================================================================
# 3D SPHERE & PARTICLE SYSTEM
# ================================================================

@dataclass
class Particle:
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
    life: float
    max_life: float

@dataclass
class Star:
    x: float
    y: float
    z: float
    brightness: float
    speed: float

class HUD3DWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Animation state
        self.rotation_x = 0
        self.rotation_y = 0
        self.rotation_z = 0
        self.pulse_phase = 0
        self.is_listening = False
        self.audio_intensity = 0.0

        # 3D Sphere vertices (icosahedron approximation)
        self.sphere_radius = 120
        self.sphere_vertices = self._generate_sphere_vertices(3)
        self.sphere_edges = self._generate_sphere_edges()

        # Particles
        self.particles: List[Particle] = []
        self.max_particles = 80

        # Starfield
        self.stars: List[Star] = []
        self._init_stars(200)

        # HUD rings
        self.ring_rotation = 0

        # Timer for animation
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(16)  # ~60fps

        self._time = 0

    def _generate_sphere_vertices(self, subdivisions: int) -> List[Tuple[float, float, float]]:
        """Generate vertices for an icosahedron sphere."""
        phi = (1 + math.sqrt(5)) / 2
        vertices = [
            (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
            (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
            (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1)
        ]
        # Normalize
        vertices = [(x/ math.sqrt(x*x + y*y + z*z), 
                     y/ math.sqrt(x*x + y*y + z*z), 
                     z/ math.sqrt(x*x + y*y + z*z)) for x, y, z in vertices]
        return vertices

    def _generate_sphere_edges(self) -> List[Tuple[int, int]]:
        """Generate edges for icosahedron."""
        edges = [
            (0,1), (0,5), (0,7), (0,10), (0,11),
            (1,5), (1,7), (1,8), (1,9),
            (2,3), (2,4), (2,6), (2,10), (2,11),
            (3,4), (3,6), (3,8), (3,9),
            (4,5), (4,9), (4,11),
            (5,9), (5,11),
            (6,7), (6,8), (6,10),
            (7,8), (7,10),
            (8,9),
            (10,11)
        ]
        return edges

    def _init_stars(self, count: int):
        for _ in range(count):
            self.stars.append(Star(
                x=random.uniform(-1000, 1000),
                y=random.uniform(-1000, 1000),
                z=random.uniform(-500, 500),
                brightness=random.uniform(0.3, 1.0),
                speed=random.uniform(0.2, 1.5)
            ))

    def set_listening(self, listening: bool):
        self.is_listening = listening

    def set_audio_intensity(self, intensity: float):
        self.audio_intensity = intensity

    def _animate(self):
        self._time += 0.016

        # Rotate sphere
        speed = 0.8 if self.is_listening else 0.3
        self.rotation_y += speed
        self.rotation_x += speed * 0.3
        self.rotation_z += speed * 0.1

        # Pulse phase
        self.pulse_phase += 0.05
        if self.is_listening:
            self.pulse_phase += 0.1

        # Rotate HUD rings
        self.ring_rotation += 0.5

        # Spawn particles when listening
        if self.is_listening and len(self.particles) < self.max_particles:
            if random.random() < 0.3:
                angle = random.uniform(0, 2 * math.pi)
                dist = random.uniform(80, 150)
                self.particles.append(Particle(
                    x=math.cos(angle) * dist,
                    y=math.sin(angle) * dist,
                    z=random.uniform(-50, 50),
                    vx=random.uniform(-2, 2),
                    vy=random.uniform(-2, 2),
                    vz=random.uniform(-1, 1),
                    life=1.0,
                    max_life=random.uniform(1.0, 3.0)
                ))

        # Update particles
        new_particles = []
        for p in self.particles:
            p.x += p.vx
            p.y += p.vy
            p.z += p.vz
            p.life -= 0.016 / p.max_life
            if p.life > 0:
                new_particles.append(p)
        self.particles = new_particles

        # Update stars
        for s in self.stars:
            s.z += s.speed
            if s.z > 500:
                s.z = -500
                s.x = random.uniform(-1000, 1000)
                s.y = random.uniform(-1000, 1000)

        self.update()

    def _project_3d(self, x: float, y: float, z: float, cx: float, cy: float, fov: float = 400) -> Tuple[float, float, float]:
        """Project 3D point to 2D with perspective."""
        if z + fov <= 0:
            z = -fov + 1
        scale = fov / (z + fov)
        return cx + x * scale, cy + y * scale, scale

    def _rotate_point(self, x: float, y: float, z: float, rx: float, ry: float, rz: float) -> Tuple[float, float, float]:
        """Rotate point around all three axes."""
        # Rotate around X
        cos_x, sin_x = math.cos(rx), math.sin(rx)
        y1 = y * cos_x - z * sin_x
        z1 = y * sin_x + z * cos_x

        # Rotate around Y
        cos_y, sin_y = math.cos(ry), math.sin(ry)
        x2 = x * cos_y + z1 * sin_y
        z2 = -x * sin_y + z1 * cos_y

        # Rotate around Z
        cos_z, sin_z = math.cos(rz), math.sin(rz)
        x3 = x2 * cos_z - y1 * sin_z
        y3 = x2 * sin_z + y1 * cos_z

        return x3, y3, z2

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2

        # Dark background with subtle gradient
        bg_gradient = QRadialGradient(cx, cy, max(w, h) / 2)
        bg_gradient.setColorAt(0, QColor("#0a0a1a"))
        bg_gradient.setColorAt(0.5, QColor("#050510"))
        bg_gradient.setColorAt(1, QColor("#020208"))
        painter.fillRect(self.rect(), bg_gradient)

        # Draw starfield
        self._draw_stars(painter, cx, cy)

        # Draw HUD rings (behind sphere)
        self._draw_hud_rings(painter, cx, cy)

        # Draw 3D wireframe sphere
        self._draw_sphere(painter, cx, cy)

        # Draw particles
        self._draw_particles(painter, cx, cy)

        # Draw NOVA text in center
        self._draw_center_text(painter, cx, cy)

        # Draw scan line
        self._draw_scan_line(painter, w, h)

        painter.end()

    def _draw_stars(self, painter: QPainter, cx: float, cy: float):
        for s in self.stars:
            px, py, scale = self._project_3d(s.x, s.y, s.z, cx, cy)
            if 0 <= px <= self.width() and 0 <= py <= self.height():
                size = max(1, scale * 2)
                alpha = int(s.brightness * 255 * min(1, (s.z + 500) / 200))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(200, 220, 255, alpha)))
                painter.drawEllipse(QPointF(px - size/2, py - size/2), size, size)

    def _draw_hud_rings(self, painter: QPainter, cx: float, cy: float):
        hud_color = QColor("#00d4ff")
        accent_color = QColor("#ff00a0")

        ring_configs = [
            (180, 2, 0.3, hud_color),
            (220, 1.5, -0.2, QColor("#00d4ff")),
            (260, 1, 0.15, accent_color),
            (300, 0.8, -0.1, QColor("#ff00a0")),
        ]

        for radius, width, rot_speed, color in ring_configs:
            points = []
            segments = 120
            tilt = math.sin(self._time * rot_speed) * 0.3

            for i in range(segments + 1):
                angle = (i / segments) * 2 * math.pi + math.radians(self.ring_rotation * rot_speed)
                x = math.cos(angle) * radius
                y = math.sin(angle) * radius * math.cos(tilt)
                z = math.sin(angle) * radius * math.sin(tilt)

                px, py, scale = self._project_3d(x, y, z, cx, cy)
                points.append(QPointF(px, py))

            # Draw ring with glow
            for glow in range(3, 0, -1):
                pen = QPen(QColor(color.red(), color.green(), color.blue(), 30 * glow))
                pen.setWidthF(width * glow * 2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                if len(points) > 1:
                    painter.drawPolyline(QPolygonF(points))

            # Core ring
            pen = QPen(color)
            pen.setWidthF(width)
            painter.setPen(pen)
            if len(points) > 1:
                painter.drawPolyline(QPolygonF(points))

            # Tick marks
            for i in range(0, segments, 10):
                angle = (i / segments) * 2 * math.pi + math.radians(self.ring_rotation * rot_speed)
                x1 = math.cos(angle) * (radius - 8)
                y1 = math.sin(angle) * (radius - 8) * math.cos(tilt)
                z1 = math.sin(angle) * (radius - 8) * math.sin(tilt)
                x2 = math.cos(angle) * (radius + 8)
                y2 = math.sin(angle) * (radius + 8) * math.cos(tilt)
                z2 = math.sin(angle) * (radius + 8) * math.sin(tilt)

                px1, py1, _ = self._project_3d(x1, y1, z1, cx, cy)
                px2, py2, _ = self._project_3d(x2, y2, z2, cx, cy)
                painter.drawLine(QPointF(px1, py1), QPointF(px2, py2))

    def _draw_sphere(self, painter: QPainter, cx: float, cy: float):
        hud_color = QColor("#00d4ff")
        accent_color = QColor("#ff00a0")

        # Pulse effect
        pulse = 1.0 + 0.1 * math.sin(self.pulse_phase)
        if self.is_listening:
            pulse += 0.15 * math.sin(self.pulse_phase * 3)

        radius = self.sphere_radius * pulse

        # Project all vertices
        projected = []
        for vx, vy, vz in self.sphere_vertices:
            rx, ry, rz = self._rotate_point(vx * radius, vy * radius, vz * radius,
                                            math.radians(self.rotation_x),
                                            math.radians(self.rotation_y),
                                            math.radians(self.rotation_z))
            px, py, scale = self._project_3d(rx, ry, rz, cx, cy)
            projected.append((px, py, scale, rz))

        # Sort edges by average Z (painter's algorithm)
        edge_depths = []
        for i, j in self.sphere_edges:
            avg_z = (projected[i][3] + projected[j][3]) / 2
            edge_depths.append((avg_z, i, j))
        edge_depths.sort(key=lambda x: x[0], reverse=True)

        # Draw edges with depth-based alpha
        for depth, i, j in edge_depths:
            px1, py1, s1, _ = projected[i]
            px2, py2, s2, _ = projected[j]

            # Depth-based color
            depth_factor = (depth + radius) / (2 * radius)
            alpha = int(40 + 180 * depth_factor)

            if self.is_listening:
                color = accent_color if random.random() > 0.7 else hud_color
            else:
                color = hud_color

            # Glow effect
            for glow in range(2, 0, -1):
                pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha // (glow + 1)))
                pen.setWidthF(glow * 1.5)
                painter.setPen(pen)
                painter.drawLine(QPointF(px1, py1), QPointF(px2, py2))

            # Core line
            pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha))
            pen.setWidthF(1)
            painter.setPen(pen)
            painter.drawLine(QPointF(px1, py1), QPointF(px2, py2))

        # Draw vertices as glowing dots
        for px, py, scale, depth in projected:
            depth_factor = (depth + radius) / (2 * radius)
            alpha = int(100 + 155 * depth_factor)
            size = 2 + depth_factor * 3

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(hud_color.red(), hud_color.green(), hud_color.blue(), alpha)))
            painter.drawEllipse(QPointF(px - size/2, py - size/2), size, size)

    def _draw_particles(self, painter: QPainter, cx: float, cy: float):
        accent_color = QColor("#ff00a0")
        for p in self.particles:
            px, py, scale = self._project_3d(p.x, p.y, p.z, cx, cy)
            alpha = int(255 * p.life)
            size = 2 + (1 - p.life) * 4

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(accent_color.red(), accent_color.green(), accent_color.blue(), alpha)))
            painter.drawEllipse(QPointF(px - size/2, py - size/2), size, size)

    def _draw_center_text(self, painter: QPainter, cx: float, cy: float):
        # Try Equinox font first, fallback to Segoe UI
        font = QFont("Equinox", 28, QFont.Weight.Light)
        try:
            if not font.exactMatch():
                raise RuntimeError
        except Exception:
            font = QFont("Segoe UI", 24, QFont.Weight.Light)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 8)

        painter.setFont(font)

        # Glow effect
        for glow in range(3, 0, -1):
            painter.setPen(QPen(QColor("#00d4ff").lighter(150), glow))
            painter.drawText(QRectF(cx - 100, cy - 20, 200, 40), 
                           Qt.AlignmentFlag.AlignCenter, "N O V A")

        # Core text
        painter.setPen(QPen(QColor("#ffffff"), 1))
        painter.drawText(QRectF(cx - 100, cy - 20, 200, 40), 
                        Qt.AlignmentFlag.AlignCenter, "N O V A")

        # Subtitle
        sub_font = QFont("Segoe UI", 8)
        sub_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4)
        painter.setFont(sub_font)
        painter.setPen(QPen(QColor("#00d4ff"), 1))
        status = "LISTENING" if self.is_listening else "ONLINE"
        painter.drawText(QRectF(cx - 100, cy + 25, 200, 20), 
                        Qt.AlignmentFlag.AlignCenter, f"◉  {status}")

    def _draw_scan_line(self, painter: QPainter, w: float, h: float):
        scan_y = (self._time * 60) % (h * 2) - h / 2
        gradient = QLinearGradient(0, scan_y - 30, 0, scan_y + 30)
        transparent_color = QColor("#00d4ff")
        transparent_color.setAlpha(0)
        semi_transparent_color = QColor("#00d4ff")
        semi_transparent_color.setAlpha(20)
        gradient.setColorAt(0, transparent_color)
        gradient.setColorAt(0.5, semi_transparent_color)
        gradient.setColorAt(1, transparent_color)
        painter.fillRect(QRectF(0, scan_y - 30, w, 60), gradient)


# ================================================================
# SYSTEM MONITOR WIDGET
# ================================================================

class SystemMonitorWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(180)
        self.setMaximumWidth(220)
        self.cpu_usage = 0
        self.ram_usage = 0
        self.net_speed = "0KB/s"
        self.gpu_usage = "N/A"
        self.temp = "N/A"
        self.uptime = "00:00"
        self.processes = 0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_stats)
        self.timer.start(1000)

        try:
            import psutil
            self.psutil = psutil
        except ImportError:
            self.psutil = None

    def _update_stats(self):
        if self.psutil:
            self.cpu_usage = self.psutil.cpu_percent()
            self.ram_usage = self.psutil.virtual_memory().percent
            self.processes = len(self.psutil.pids())
            boot_time = self.psutil.boot_time()
            uptime_sec = time.time() - boot_time
            hours = int(uptime_sec // 3600)
            mins = int((uptime_sec % 3600) // 60)
            self.uptime = f"{hours:02d}:{mins:02d}"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()

        # Background
        painter.fillRect(self.rect(), QColor("#0a0a1a"))

        # Title
        font = QFont("Segoe UI", 10, QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#00d4ff"), 1))
        painter.drawText(QRectF(10, 10, w - 20, 20), Qt.AlignmentFlag.AlignLeft, "◉ SYS MONITOR")

        # Separator
        sep_color = QColor("#00d4ff")
        sep_color.setAlpha(60)
        painter.setPen(QPen(sep_color, 1))
        painter.drawLine(10, 35, w - 10, 35)

        # Stats
        y = 50
        stats = [
            ("CPU", f"{self.cpu_usage:.0f}%", self._color_for_percent(self.cpu_usage)),
            ("MEM", f"{self.ram_usage:.0f}%", self._color_for_percent(self.ram_usage)),
            ("NET", self.net_speed, "#00ff88"),
            ("GPU", self.gpu_usage, "#ffaa00"),
            ("TMP", self.temp, "#ff6666"),
        ]

        for label, value, color in stats:
            painter.setPen(QPen(QColor("#8888aa"), 1))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(QRectF(15, y, 60, 18), Qt.AlignmentFlag.AlignLeft, label)

            painter.setPen(QPen(QColor(color), 1))
            painter.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            painter.drawText(QRectF(w - 80, y, 70, 18), Qt.AlignmentFlag.AlignRight, value)

            # Progress bar background
            painter.fillRect(QRectF(15, y + 20, w - 30, 4), QColor("#1a1a2e"))
            # Progress bar fill
            if label in ("CPU", "MEM"):
                fill_width = (w - 30) * (float(value.rstrip('%')) / 100)
                gradient = QLinearGradient(15, 0, w - 15, 0)
                gradient.setColorAt(0, QColor(color))
                gradient.setColorAt(1, QColor(color).lighter(150))
                painter.fillRect(QRectF(15, y + 20, fill_width, 4), gradient)

            y += 45

        # Uptime & processes
        c = QColor("#00d4ff")
        c.setAlpha(80)
        painter.setPen(QPen(c, 1))
        painter.drawLine(10, y, w - 10, y)
        y += 10

        info_font = QFont("Consolas", 9)
        painter.setFont(info_font)
        painter.setPen(QPen(QColor("#00d4ff"), 1))
        painter.drawText(QRectF(15, y, w - 30, 18), Qt.AlignmentFlag.AlignLeft, f"UP  {self.uptime}")
        y += 18
        painter.drawText(QRectF(15, y, w - 30, 18), Qt.AlignmentFlag.AlignLeft, f"PROC  {self.processes}")
        y += 18
        painter.drawText(QRectF(15, y, w - 30, 18), Qt.AlignmentFlag.AlignLeft, "OS  WIN")

        painter.end()

    def _color_for_percent(self, pct: float) -> str:
        if pct < 50:
            return "#00ff88"
        elif pct < 80:
            return "#ffaa00"
        return "#ff3366"


# ================================================================
# ACTIVITY LOG WIDGET
# ================================================================

class ActivityLogWidget(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMinimumWidth(250)
        self.setMaximumWidth(350)
        self.setFrameStyle(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.setStyleSheet("""
            QTextEdit {
                background-color: #0a0a1a;
                color: #00d4ff;
                border: 1px solid #00d4ff30;
                border-radius: 4px;
                padding: 10px;
                font-family: "Consolas", monospace;
                font-size: 11px;
            }
            QScrollBar:vertical {
                background: #0a0a1a;
                width: 6px;
            }
            QScrollBar::handle:vertical {
                background: #00d4ff60;
                border-radius: 3px;
            }
        """)

    def add_log(self, sender: str, text: str, color: str = "#00d4ff"):
        timestamp = time.strftime("%H:%M:%S")
        if sender == "SYS":
            html = f'<span style="color: #ffaa00;">[{timestamp}] SYS:</span> <span style="color: {color};">{text}</span><br>'
        elif sender == "YOU":
            html = f'<span style="color: #00ff88;">[{timestamp}] You:</span> <span style="color: #e0e0e0;">{text}</span><br>'
        else:
            html = f'<span style="color: #ff00a0;">[{timestamp}] NOVA:</span> <span style="color: #e0e0e0;">{text}</span><br>'

        self.append(html)
        scrollbar = self.verticalScrollBar()
        if scrollbar is not None:
            scrollbar.setValue(scrollbar.maximum())


# ================================================================
# SETTINGS DIALOG
# ================================================================

class SettingsDialog(QWidget):
    def __init__(self, config, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.config = config.copy()
        self.setWindowTitle("NOVA SETTINGS")
        self.setMinimumSize(450, 550)
        self._setup_ui()
        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #0a0a1a;
                color: #00d4ff;
                font-family: "Segoe UI", sans-serif;
            }
            QLabel {
                color: #00d4ff;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 2px;
            }
            QLineEdit {
                background-color: #0f0f2a;
                color: #e0e0e0;
                border: 1px solid #00d4ff40;
                border-radius: 4px;
                padding: 8px 12px;
                font-family: "Consolas", monospace;
            }
            QLineEdit:focus {
                border: 1px solid #00d4ff;
            }
            QComboBox {
                background-color: #0f0f2a;
                color: #e0e0e0;
                border: 1px solid #00d4ff40;
                border-radius: 4px;
                padding: 6px;
            }
            QSlider::groove:horizontal {
                height: 4px;
                background: #1a1a2e;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 14px;
                height: 14px;
                background: #00d4ff;
                border-radius: 7px;
                margin: -5px 0;
            }
            QCheckBox {
                color: #8888aa;
                font-size: 11px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 1px solid #00d4ff40;
            }
            QCheckBox::indicator:checked {
                background-color: #00d4ff;
            }
            QPushButton {
                background-color: #00d4ff20;
                color: #00d4ff;
                border: 1px solid #00d4ff60;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
                letter-spacing: 2px;
            }
            QPushButton:hover {
                background-color: #00d4ff40;
            }
        """)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(25, 25, 25, 25)

        # Title
        title = QLabel("◉ SYSTEM CONFIGURATION")
        title_font = QFont("Segoe UI", 14, QFont.Weight.Bold)
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        title.setFont(title_font)
        layout.addWidget(title)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #00d4ff30;")
        sep.setFixedHeight(1)
        layout.addWidget(sep)

        # AI Provider
        layout.addWidget(QLabel("AI PROVIDER"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["groq", "openai"])
        self.provider_combo.setCurrentText(self.config.get('ai_provider', 'groq'))
        layout.addWidget(self.provider_combo)

        # API Keys
        layout.addWidget(QLabel("GROQ API KEY"))
        self.groq_key = QLineEdit()
        self.groq_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.groq_key.setText(self.config.get('groq_api_key', ''))
        layout.addWidget(self.groq_key)

        layout.addWidget(QLabel("OPENAI API KEY"))
        self.openai_key = QLineEdit()
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setText(self.config.get('openai_api_key', ''))
        layout.addWidget(self.openai_key)

        # Mic Threshold
        layout.addWidget(QLabel("MICROPHONE SENSITIVITY"))
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setRange(1, 20)
        self.threshold_slider.setValue(int(self.config.get('mic_threshold', 0.05) * 200))
        layout.addWidget(self.threshold_slider)

        # Voice Rate
        layout.addWidget(QLabel("VOICE SPEED"))
        self.voice_slider = QSlider(Qt.Orientation.Horizontal)
        self.voice_slider.setRange(100, 300)
        self.voice_slider.setValue(self.config.get('voice_rate', 180))
        layout.addWidget(self.voice_slider)

        # Equinox Font Path
        layout.addWidget(QLabel("EQUINOX FONT PATH (.otf/.ttf)"))
        font_layout = QHBoxLayout()
        self.font_path = QLineEdit()
        self.font_path.setText(self.config.get('equinox_font_path', ''))
        self.font_path.setPlaceholderText("C:\\Path\\To\\Equinox-Regular.otf")
        font_layout.addWidget(self.font_path)
        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(40)
        browse_btn.clicked.connect(self._browse_font)
        font_layout.addWidget(browse_btn)
        layout.addLayout(font_layout)

        # Toggles
        self.auto_listen = QCheckBox("AUTO-START LISTENING ON LAUNCH")
        self.auto_listen.setChecked(self.config.get('auto_start_listening', False))
        layout.addWidget(self.auto_listen)

        self.always_top = QCheckBox("ALWAYS ON TOP")
        self.always_top.setChecked(self.config.get('always_on_top', False))
        layout.addWidget(self.always_top)

        self.save_hist = QCheckBox("SAVE CONVERSATION HISTORY")
        self.save_hist.setChecked(self.config.get('save_history', True))
        layout.addWidget(self.save_hist)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("SAVE")
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.clicked.connect(self.close)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _browse_font(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, "Select Equinox Font", "", "Font Files (*.otf *.ttf)")
        if path:
            self.font_path.setText(path)

    def _save(self):
        self.config['ai_provider'] = self.provider_combo.currentText()
        self.config['groq_api_key'] = self.groq_key.text()
        self.config['openai_api_key'] = self.openai_key.text()
        self.config['mic_threshold'] = self.threshold_slider.value() / 200
        self.config['voice_rate'] = self.voice_slider.value()
        self.config['equinox_font_path'] = self.font_path.text()
        self.config['auto_start_listening'] = self.auto_listen.isChecked()
        self.config['always_on_top'] = self.always_top.isChecked()
        self.config['save_history'] = self.save_hist.isChecked()
        self.close()

    def get_config(self):
        return self.config

    def exec(self):
        self.show()
        from PyQt6.QtCore import QEventLoop
        loop = QEventLoop()
        self.destroyed.connect(loop.quit)
        loop.exec()
        return 1


# ================================================================
# MAIN WINDOW
# ================================================================

class NovaMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = self._load_config()
        self.history = self._load_history()

        self.audio = AudioEngine(self.config)
        self.ai = AIBackend(self.config)
        self.voice_thread = None

        self.messages = [{"role": "system", "content": 
            "You are NOVA, a futuristic AI voice assistant. Keep responses concise and natural for speech."}]

        self._load_equinox_font()
        self._setup_ui()
        self._setup_shortcuts()
        self._setup_tray()

        if self.config.get('auto_start_listening', False):
            self.toggle_listening()

    def _load_equinox_font(self):
        font_path = self.config.get('equinox_font_path', '')
        if font_path and os.path.exists(font_path):
            QFontDatabase.addApplicationFont(font_path)
        else:
            # Try common locations
            possible_paths = [
                str(FONTS_DIR / "Equinox-Regular.otf"),
                str(FONTS_DIR / "Equinox-Regular.ttf"),
                str(FONTS_DIR / "Equinox.otf"),
                str(FONTS_DIR / "Equinox.ttf"),
                "C:/Windows/Fonts/Equinox-Regular.otf",
                os.path.expanduser("~/AppData/Local/Microsoft/Windows/Fonts/Equinox-Regular.otf"),
            ]
            for path in possible_paths:
                if os.path.exists(path):
                    QFontDatabase.addApplicationFont(path)
                    self.config['equinox_font_path'] = path
                    break

    def _load_config(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    saved = json.load(f)
                    config = DEFAULT_CONFIG.copy()
                    config.update(saved)
                    return config
            except Exception:
                pass
        return DEFAULT_CONFIG.copy()

    def _save_config(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=2)

    def _load_history(self):
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_history(self):
        if not self.config.get('save_history', True):
            return
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, 'w') as f:
            json.dump(self.history[-100:], f, indent=2)

    def _setup_ui(self):
        self.setWindowTitle("N O V A")
        self.setMinimumSize(1200, 800)

        # Fullscreen if configured
        if self.config.get('fullscreen', False):
            self.showFullScreen()

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Left panel - System Monitor
        self.sys_monitor = SystemMonitorWidget()
        self.sys_monitor.setStyleSheet("background-color: #0a0a1a; border-right: 1px solid #00d4ff20;")
        main_layout.addWidget(self.sys_monitor)

        # Center panel - 3D HUD
        center_layout = QVBoxLayout()
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        # Top bar
        top_bar = QWidget()
        top_bar.setFixedHeight(50)
        top_bar.setStyleSheet("background-color: #0a0a1a; border-bottom: 1px solid #00d4ff20;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(20, 0, 20, 0)

        # Mark label
        mark_label = QLabel("MARK XXXIX")
        mark_font = QFont("Segoe UI", 9)
        mark_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        mark_label.setFont(mark_font)
        mark_label.setStyleSheet("color: #00d4ff60;")
        top_layout.addWidget(mark_label)

        top_layout.addStretch()

        # NOVA title with Equinox font
        title = QLabel("N O V A")
        title_font = QFont("Equinox", 20, QFont.Weight.Light)
        if "Equinox" not in QFontDatabase.families():
            title_font = QFont("Segoe UI", 18, QFont.Weight.Light)
            title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 10)
        title.setFont(title_font)
        title.setStyleSheet("color: #00d4ff;")
        top_layout.addWidget(title)

        top_layout.addStretch()

        # Clock
        self.clock_label = QLabel()
        self.clock_label.setFont(QFont("Consolas", 12))
        self.clock_label.setStyleSheet("color: #00d4ff;")
        top_layout.addWidget(self.clock_label)

        # Settings button
        settings_btn = QPushButton("⚙")
        settings_btn.setFixedSize(36, 36)
        settings_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #00d4ff;
                border: 1px solid #00d4ff40;
                border-radius: 4px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #00d4ff20;
                border: 1px solid #00d4ff;
            }
        """)
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.clicked.connect(self._show_settings)
        top_layout.addWidget(settings_btn)

        center_layout.addWidget(top_bar)

        # 3D HUD widget
        self.hud_3d = HUD3DWidget()
        center_layout.addWidget(self.hud_3d, stretch=1)

        # Bottom control bar
        bottom_bar = QWidget()
        bottom_bar.setFixedHeight(80)
        bottom_bar.setStyleSheet("background-color: #0a0a1a; border-top: 1px solid #00d4ff20;")
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(20, 10, 20, 10)

        # Text input
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Type a command or question...")
        self.text_input.setStyleSheet("""
            QLineEdit {
                background-color: #0f0f2a;
                color: #e0e0e0;
                border: 1px solid #00d4ff40;
                border-radius: 4px;
                padding: 10px 15px;
                font-family: "Segoe UI", sans-serif;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid #00d4ff;
            }
        """)
        self.text_input.returnPressed.connect(self._send_text)
        bottom_layout.addWidget(self.text_input, stretch=1)

        # Mic button
        self.mic_btn = QPushButton("🎤 MIC")
        self.mic_btn.setFixedSize(100, 40)
        self.mic_btn.setCheckable(True)
        self.mic_btn.setStyleSheet("""
            QPushButton {
                background-color: #00d4ff20;
                color: #00d4ff;
                border: 1px solid #00d4ff60;
                border-radius: 4px;
                font-weight: bold;
                letter-spacing: 2px;
            }
            QPushButton:hover {
                background-color: #00d4ff40;
            }
            QPushButton:checked {
                background-color: #ff00a040;
                color: #ff00a0;
                border: 1px solid #ff00a0;
            }
        """)
        self.mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_btn.clicked.connect(self.toggle_listening)
        bottom_layout.addWidget(self.mic_btn)

        # Send button
        send_btn = QPushButton("➤")
        send_btn.setFixedSize(50, 40)
        send_btn.setStyleSheet("""
            QPushButton {
                background-color: #00d4ff20;
                color: #00d4ff;
                border: 1px solid #00d4ff60;
                border-radius: 4px;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: #00d4ff40;
            }
        """)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.clicked.connect(self._send_text)
        bottom_layout.addWidget(send_btn)

        center_layout.addWidget(bottom_bar)

        main_layout.addLayout(center_layout, stretch=1)

        # Right panel - Activity Log
        right_panel = QVBoxLayout()
        right_panel.setContentsMargins(0, 0, 0, 0)
        right_panel.setSpacing(0)

        # Activity log title
        log_title = QLabel("◉ ACTIVITY LOG")
        log_title.setFixedHeight(50)
        log_title_font = QFont("Segoe UI", 10, QFont.Weight.Bold)
        log_title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        log_title.setFont(log_title_font)
        log_title.setStyleSheet("color: #00d4ff; background-color: #0a0a1a; padding-left: 15px; border-bottom: 1px solid #00d4ff20; border-left: 1px solid #00d4ff20;")
        right_panel.addWidget(log_title)

        self.activity_log = ActivityLogWidget()
        self.activity_log.setStyleSheet("border-left: 1px solid #00d4ff20;")
        right_panel.addWidget(self.activity_log, stretch=1)

        # Bottom status
        status_bar = QLabel("F1: Mute  |  F11: Fullscreen  |  SPACE: Toggle Mic")
        status_bar.setFixedHeight(30)
        status_bar.setFont(QFont("Consolas", 8))
        status_bar.setStyleSheet("color: #00d4ff60; background-color: #0a0a1a; padding-left: 15px; border-top: 1px solid #00d4ff20; border-left: 1px solid #00d4ff20;")
        right_panel.addWidget(status_bar)

        right_widget = QWidget()
        right_widget.setLayout(right_panel)
        right_widget.setMinimumWidth(280)
        right_widget.setMaximumWidth(350)
        main_layout.addWidget(right_widget)

        # Clock timer
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)
        self._update_clock()

        # Window settings
        self.setWindowOpacity(self.config.get('window_opacity', 0.98))
        if self.config.get('always_on_top', False):
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        # Initial log
        self.activity_log.add_log("SYS", "NOVA online.", "#00d4ff")
        self.activity_log.add_log("SYS", "Press SPACE or click MIC to start listening.", "#8888aa")

    def _update_clock(self):
        self.clock_label.setText(time.strftime("%H:%M:%S"))

    def _setup_shortcuts(self):
        self.space_shortcut = QShortcut(QKeySequence("Space"), self)
        self.space_shortcut.activated.connect(self._on_space_pressed)

        self.esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.esc_shortcut.activated.connect(self._stop_listening)

        self.f11_shortcut = QShortcut(QKeySequence("F11"), self)
        self.f11_shortcut.activated.connect(self._toggle_fullscreen)

        self.f1_shortcut = QShortcut(QKeySequence("F1"), self)
        self.f1_shortcut.activated.connect(self._stop_listening)

    def _on_space_pressed(self):
        if not self.text_input.hasFocus():
            self.toggle_listening()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self)
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setBrush(QBrush(QColor("#00d4ff")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(4, 4, 56, 56)
        painter.setBrush(QBrush(QColor("#0a0a1a")))
        painter.drawEllipse(20, 20, 24, 24)
        painter.end()
        self.tray.setIcon(QIcon(pixmap))
        self.tray.setToolTip("NOVA")

        tray_menu = QMenu()
        show_action = tray_menu.addAction("Show NOVA")
        if show_action is not None:
            show_action.triggered.connect(self.showNormal)
        listen_action = tray_menu.addAction("Toggle Mic")
        if listen_action is not None:
            listen_action.triggered.connect(self.toggle_listening)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Quit")
        if quit_action is not None:
            quit_action.triggered.connect(self._quit_app)
        self.tray.setContextMenu(tray_menu)
        self.tray.show()

    def toggle_listening(self):
        if self.voice_thread and self.voice_thread.isRunning():
            self._stop_listening()
        else:
            self._start_listening()

    def _start_listening(self):
        if sr is None:
            self.activity_log.add_log("SYS", "Speech recognition not installed.", "#ff6666")
            return

        self.voice_thread = VoiceThread(self.config)
        self.voice_thread.text_ready.connect(self._on_voice_text)
        self.voice_thread.listening_state.connect(self._on_listening_state)
        self.voice_thread.error_signal.connect(self._on_voice_error)
        self.voice_thread.start()

        self.mic_btn.setChecked(True)
        self.hud_3d.set_listening(True)
        self.activity_log.add_log("SYS", "Microphone active.", "#00ff88")

    def _stop_listening(self):
        if self.voice_thread:
            self.voice_thread.stop()
            self.voice_thread = None
        self.mic_btn.setChecked(False)
        self.hud_3d.set_listening(False)
        self.activity_log.add_log("SYS", "Microphone muted.", "#ffaa00")

    def _on_voice_text(self, text):
        self.activity_log.add_log("YOU", text, "#00ff88")
        self._process_ai_response(text)

    def _on_listening_state(self, is_listening):
        self.hud_3d.set_listening(is_listening)

    def _on_voice_error(self, error):
        self.activity_log.add_log("SYS", error, "#ff6666")
        self._stop_listening()

    def _send_text(self):
        text = self.text_input.text().strip()
        if not text:
            return
        self.text_input.clear()
        self.activity_log.add_log("YOU", text, "#00ff88")
        self._process_ai_response(text)

    def _process_ai_response(self, text):
        self.messages.append({"role": "user", "content": text})
        self.activity_log.add_log("SYS", "Processing...", "#ffaa00")

        thread = threading.Thread(target=self._ai_worker, daemon=True)
        thread.start()

    def _ai_worker(self):
        response = self.ai.chat(self.messages)
        from PyQt6.QtCore import QMetaObject
        QMetaObject.invokeMethod(self, "_on_ai_response", 
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, response))

    @pyqtSlot(str)
    def _on_ai_response(self, response):
        self.messages.append({"role": "assistant", "content": response})
        self.activity_log.add_log("NOVA", response, "#ff00a0")
        self.audio.speak(response)

        self.history.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user": self.messages[-2]["content"],
            "nova": response
        })
        self._save_history()

    def _show_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == 1:
            self.config = dialog.get_config()
            self._save_config()
            self.audio = AudioEngine(self.config)
            self.ai = AIBackend(self.config)
            self._load_equinox_font()

    def _quit_app(self):
        self._stop_listening()
        self.audio.stop()
        QApplication.quit()

    def closeEvent(self, event):
        if hasattr(self, 'tray'):
            event.ignore()
            self.hide()
            self.tray.showMessage("NOVA", "Running in system tray.", QSystemTrayIcon.MessageIcon.Information, 2000)
        else:
            self._quit_app()


# ================================================================
# MAIN ENTRY
# ================================================================

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Global font
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    window = NovaMainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()