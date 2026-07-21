"""
NOVA UI Module — System Tray + Floating Orb + Audio Waveform
Drop-in visual feedback for NOVA voice assistant
"""

import tkinter as tk
from tkinter import Canvas
import threading
import time
import math
import random
import pystray
from PIL import Image, ImageDraw

# ─── Configuration ──────────────────────────────────────────

ORB_SIZE = 160  # Slightly larger to fit waveform
CENTER = ORB_SIZE // 2

ORB_COLORS = {
    'idle': '#0088ff',      # Blue — listening
    'active': '#00ff64',    # Green — heard wake word
    'speaking': '#ffc800',  # Yellow — processing/speaking
    'error': '#ff3333',     # Red — error/offline
    'offline': '#555555',   # Gray — not running
}

TRAY_COLORS = {
    'idle': (0, 136, 255),
    'active': (0, 255, 100),
    'speaking': (255, 200, 0),
    'error': (255, 51, 51),
    'offline': (85, 85, 85),
}

# Waveform settings
WAVE_BASE_RADIUS = 45      # Distance from center to start of waveform
WAVE_MAX_HEIGHT = 25       # Max spike height
WAVE_POINTS = 64             # Number of points around the ring (more = smoother)
WAVE_SMOOTHING = 0.3         # Smoothing factor (0-1, higher = more reactive)
WAVE_COLOR_IDLE = '#004488'
WAVE_COLOR_ACTIVE = '#00aa44'


# ─── Floating Orb + Waveform ────────────────────────────────

class NOVAOrb:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-transparentcolor', 'black')
        
        # Position: bottom-right of screen
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{ORB_SIZE}x{ORB_SIZE}+{sw-ORB_SIZE-40}+{sh-ORB_SIZE-80}")
        
        self.canvas = Canvas(
            self.root, 
            width=ORB_SIZE, 
            height=ORB_SIZE, 
            bg='black', 
            highlightthickness=0
        )
        self.canvas.pack()
        
        # ── Waveform Ring ──
        self.waveform_id = None
        self.wave_values = [0.0] * WAVE_POINTS  # Current smoothed values
        self.wave_target = [0.0] * WAVE_POINTS   # Target values (from audio)
        self._wave_enabled = True
        
        # ── Main Orb Circle ──
        orb_r = 32
        self.orb = self.canvas.create_oval(
            CENTER-orb_r, CENTER-orb_r,
            CENTER+orb_r, CENTER+orb_r,
            fill=ORB_COLORS['idle'], 
            outline=''
        )
        
        # ── Glow Rings ──
        self.glow_rings = []
        for i in range(3):
            gr = orb_r + 8 + i * 7
            ring = self.canvas.create_oval(
                CENTER-gr, CENTER-gr,
                CENTER+gr, CENTER+gr,
                fill='', outline=ORB_COLORS['idle'],
                width=2
            )
            self.glow_rings.append(ring)
        
        # ── Status Text ──
        self.label = self.canvas.create_text(
            CENTER, ORB_SIZE - 12,
            text="NOVA",
            fill='white',
            font=('Segoe UI', 9, 'bold')
        )
        
        # State tracking
        self.current_state = 'idle'
        self.pulse_speed = 2.0
        self._pulse_active = True
        
        # Drag to move
        self._drag_data = {'x': 0, 'y': 0}
        self.canvas.bind('<Button-1>', self._start_drag)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<Button-3>', lambda e: self._show_menu(e))
        
        # Start animations
        self._pulse()
        self._animate_waveform()
    
    # ── Waveform Methods ──
    
    def _draw_waveform(self):
        """Draw the audio waveform as a polygon around the orb"""
        if self.waveform_id:
            self.canvas.delete(self.waveform_id)
        
        points = []
        for i, value in enumerate(self.wave_values):
            angle = (2 * math.pi * i) / WAVE_POINTS - math.pi / 2  # Start from top
            r = WAVE_BASE_RADIUS + value * WAVE_MAX_HEIGHT
            
            x = CENTER + r * math.cos(angle)
            y = CENTER + r * math.sin(angle)
            points.extend([x, y])
        
        # Color based on state
        if self.current_state == 'idle':
            color = WAVE_COLOR_IDLE
            outline_color = '#0066cc'
        elif self.current_state == 'active':
            color = '#00ff64'
            outline_color = '#00cc44'
        elif self.current_state == 'speaking':
            color = '#ffc800'
            outline_color = '#cc9900'
        else:
            color = WAVE_COLOR_IDLE
            outline_color = '#0066cc'
        
        # Draw filled polygon with transparency effect (simulated with outline)
        if len(points) >= 6:
            self.waveform_id = self.canvas.create_polygon(
                points,
                fill=color,
                outline=outline_color,
                width=2,
                smooth=True
            )
    
    def _animate_waveform(self):
        """Update waveform animation"""
        if not self._wave_enabled:
            self.root.after(16, self._animate_waveform)
            return
        
        # Smooth interpolation toward target values
        for i in range(WAVE_POINTS):
            self.wave_values[i] += (self.wave_target[i] - self.wave_values[i]) * WAVE_SMOOTHING
        
        self._draw_waveform()
        self.root.after(16, self._animate_waveform)  # ~60fps
    
    def set_audio_level(self, levels):
        """
        Call this from your audio thread with audio amplitude data.
        levels: list of float 0.0-1.0, length should match WAVE_POINTS
        """
        if len(levels) != WAVE_POINTS:
            # Interpolate or truncate to match
            levels = self._resample(levels, WAVE_POINTS)
        
        self.wave_target = list(levels)
    
    def _resample(self, data, target_len):
        """Simple resampling to match point count"""
        if len(data) == 0:
            return [0.0] * target_len
        result = []
        for i in range(target_len):
            idx = int(i * len(data) / target_len)
            result.append(data[min(idx, len(data)-1)])
        return result
    
    def _simulate_audio(self):
        """Generate fake waveform for demo/visual testing"""
        t = time.time()
        simulated = []
        for i in range(WAVE_POINTS):
            # Multiple sine waves for organic look
            freq1 = math.sin(t * 3 + i * 0.3) * 0.5 + 0.5
            freq2 = math.sin(t * 7 + i * 0.7) * 0.3
            freq3 = math.sin(t * 11 + i * 1.1) * 0.2
            noise = random.uniform(-0.05, 0.05)
            
            value = max(0, min(1, (freq1 + freq2 + freq3) * 0.5 + noise))
            
            # Boost when active/speaking
            if self.current_state == 'active':
                value *= 1.5
            elif self.current_state == 'speaking':
                value *= 2.0
            
            simulated.append(min(1.0, value))
        
        self.set_audio_level(simulated)
    
    # ── Core Animation ──
    
    def _pulse(self):
        """Breathing animation + simulated audio when idle"""
        if not self._pulse_active:
            return
        
        # Simulate audio for visual effect when in relevant states
        if self.current_state in ('idle', 'active', 'speaking'):
            self._simulate_audio()
        else:
            self.wave_target = [0.0] * WAVE_POINTS
        
        # Orb pulse
        t = time.time()
        if self.current_state == 'idle':
            scale = 0.88 + 0.12 * abs(math.sin(t * self.pulse_speed))
        elif self.current_state == 'active':
            scale = 0.9 + 0.2 * abs(math.sin(t * 4))
        elif self.current_state == 'speaking':
            scale = 0.85 + 0.3 * abs(math.sin(t * 6))
        else:
            scale = 0.9 + 0.1 * abs(math.sin(t * 1))
        
        base_r = 32 * scale
        self.canvas.coords(
            self.orb,
            CENTER - base_r, CENTER - base_r,
            CENTER + base_r, CENTER + base_r
        )
        
        # Update glow rings
        for i, ring in enumerate(self.glow_rings):
            gr = base_r + 8 + i * 7
            self.canvas.coords(
                ring,
                CENTER - gr, CENTER - gr,
                CENTER + gr, CENTER + gr
            )
        
        self.root.after(30, self._pulse)
    
    def set_state(self, state: str, status_text: str | None = None):
        self.current_state = state
        color = ORB_COLORS[state]
        
        self.canvas.itemconfig(self.orb, fill=color)
        for ring in self.glow_rings:
            self.canvas.itemconfig(ring, outline=color)
        
        if status_text:
            self.canvas.itemconfig(self.label, text=status_text)
        
        speeds = {'idle': 2.0, 'active': 4.0, 'speaking': 6.0, 'error': 1.0, 'offline': 0.5}
        self.pulse_speed = speeds.get(state, 2.0)
    
    def _start_drag(self, event):
        self._drag_data['x'] = event.x_root - self.root.winfo_x()
        self._drag_data['y'] = event.y_root - self.root.winfo_y()
    
    def _on_drag(self, event):
        x = event.x_root - self._drag_data['x']
        y = event.y_root - self._drag_data['y']
        self.root.geometry(f"+{x}+{y}")
    
    def _show_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Exit NOVA", command=self.shutdown)
        menu.post(event.x_root, event.y_root)
    
    def shutdown(self):
        self._pulse_active = False
        self._wave_enabled = False
        self.root.destroy()
    
    def run(self):
        self.root.mainloop()


# ─── System Tray ──────────────────────────────────────────────

def _create_tray_image(color):
    width = 64
    height = 64
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    dc.ellipse((4, 4, width-4, height-4), fill=color)
    return image

class NOVATray:
    def __init__(self, orb: NOVAOrb):
        self.orb = orb
        self.icon = None
        self._current_color = TRAY_COLORS['idle']
    
    def _make_icon(self):
        return _create_tray_image(self._current_color)
    
    def _on_clicked(self, icon, item):
        action = str(item)
        if action == "Exit NOVA":
            self.shutdown()
    
    def set_state(self, state: str):
        self._current_color = TRAY_COLORS.get(state, TRAY_COLORS['idle'])
        if self.icon:
            self.icon.icon = self._make_icon()
            self.icon.title = f"NOVA — {state.upper()}"
    
    def run(self):
        menu = pystray.Menu(
            pystray.MenuItem("Status: IDLE", lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit NOVA", self._on_clicked),
        )
        
        self.icon = pystray.Icon(
            "NOVA",
            self._make_icon(),
            "NOVA — IDLE",
            menu=menu
        )
        self.icon.run()
    
    def shutdown(self):
        if self.icon:
            self.icon.stop()
        self.orb.shutdown()


# ─── Combined Manager ─────────────────────────────────────────

class NOVAUI:
    def __init__(self):
        self.orb = NOVAOrb()
        self.tray = NOVATray(self.orb)
        self._threads = []
    
    def start(self):
        orb_thread = threading.Thread(target=self.orb.run, daemon=True)
        orb_thread.start()
        self._threads.append(orb_thread)
        
        tray_thread = threading.Thread(target=self.tray.run, daemon=True)
        tray_thread.start()
        self._threads.append(tray_thread)
        
        time.sleep(0.5)
        return self
    
    def set_state(self, state: str, text: str | None = None):
        self.orb.root.after(0, lambda: self.orb.set_state(state, text))
        self.tray.set_state(state)
    
    def set_audio_levels(self, levels):
        """
        Wire your real audio data here!
        levels: list of floats 0.0-1.0 representing audio amplitude per frequency band
        """
        self.orb.set_audio_level(levels)
    
    def speak(self, text: str):
        self.set_state('speaking', f'Speaking: {text[:20]}...')
    
    def listen(self):
        self.set_state('idle', 'Listening...')
    
    def wake(self):
        self.set_state('active', 'Wake word detected!')
    
    def error(self, msg: str | None = None):
        self.set_state('error', msg or 'Error')
    
    def shutdown(self):
        self.tray.shutdown()


# ─── Singleton ────────────────────────────────────────────────

_ui_instance = None

def init_ui():
    global _ui_instance
    if _ui_instance is None:
        _ui_instance = NOVAUI().start()
    return _ui_instance

def get_ui():
    return _ui_instance