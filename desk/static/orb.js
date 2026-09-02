/**
 * NOVA Orb — Canvas-rendered state-driven visual anchor.
 *
 * States: idle, listening, thinking, speaking, executing, offline, error
 * Renders: teal/green core, orbital rings, particle system, waveform reactivity
 * All animations GPU-accelerated (transform/opacity), target 60fps.
 */
"use strict";

const OrbState = {
  IDLE: "idle",
  LISTENING: "listening",
  THINKING: "thinking",
  SPEAKING: "speaking",
  EXECUTING: "executing",
  OFFLINE: "offline",
  ERROR: "error",
};

class NovaOrb {
  constructor(canvas, size = 120) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.size = size;
    this.dpr = window.devicePixelRatio || 1;
    this.state = OrbState.IDLE;
    this.prevState = OrbState.IDLE;
    this.transitionProgress = 1; // 0..1, 1 = complete
    this.transitionDuration = 300; // ms
    this.transitionStart = 0;
    this.time = 0;
    this.audioAmplitude = 0; // 0..1, driven by real audio data
    this.particles = [];
    this.rings = [];
    this._animFrame = null;
    this._resize();
    this._initParticles(60);
    this._initRings(3);
  }

  _resize() {
    const w = this.size;
    const h = this.size;
    this.canvas.width = w * this.dpr;
    this.canvas.height = h * this.dpr;
    this.canvas.style.width = w + "px";
    this.canvas.style.height = h + "px";
    this.ctx.scale(this.dpr, this.dpr);
    this.cx = w / 2;
    this.cy = h / 2;
    this.radius = Math.min(w, h) * 0.28;
  }

  _initParticles(count) {
    this.particles = [];
    for (let i = 0; i < count; i++) {
      const angle = (Math.PI * 2 * i) / count + Math.random() * 0.3;
      const dist = this.radius * (1.2 + Math.random() * 1.0);
      this.particles.push({
        angle,
        dist,
        speed: 0.0003 + Math.random() * 0.0008,
        size: 0.8 + Math.random() * 1.5,
        alpha: 0.2 + Math.random() * 0.5,
        drift: (Math.random() - 0.5) * 0.001,
      });
    }
  }

  _initRings(count) {
    this.rings = [];
    for (let i = 0; i < count; i++) {
      this.rings.push({
        radius: this.radius * (1.1 + i * 0.35),
        speed: 0.0002 * (i % 2 === 0 ? 1 : -1) * (1 + i * 0.3),
        width: 0.5 + Math.random() * 0.8,
        alpha: 0.08 + Math.random() * 0.12,
        dashOffset: Math.random() * 100,
      });
    }
  }

  setState(newState) {
    if (newState === this.state) return;
    this.prevState = this.state;
    this.state = newState;
    this.transitionProgress = 0;
    this.transitionStart = performance.now();
  }

  setAudioAmplitude(amp) {
    this.audioAmplitude = Math.max(0, Math.min(1, amp));
  }

  start() {
    this._tick();
  }

  stop() {
    if (this._animFrame) {
      cancelAnimationFrame(this._animFrame);
      this._animFrame = null;
    }
  }

  _tick() {
    const now = performance.now();
    const dt = Math.min(now - (this._lastFrame || now), 33); // cap at ~30fps min
    this._lastFrame = now;
    this.time += dt;

    // Transition progress
    if (this.transitionProgress < 1) {
      const elapsed = now - this.transitionStart;
      this.transitionProgress = Math.min(1, elapsed / this.transitionDuration);
    }

    this._draw(dt);
    this._animFrame = requestAnimationFrame(() => this._tick());
  }

  _draw(dt) {
    const ctx = this.ctx;
    const w = this.size;
    const h = this.size;
    const t = this.time;

    ctx.clearRect(0, 0, w, h);

    // Eased transition factor
    const ease = this._easeInOutCubic(this.transitionProgress);

    // State-specific parameters
    const params = this._getStateParams(ease);

    // Draw layers
    this._drawOuterGlow(ctx, params);
    this._drawRings(ctx, params, t);
    this._drawCore(ctx, params, t);
    this._drawWaveform(ctx, params, t);
    this._drawParticles(ctx, params, t);
    this._drawKernel(ctx, params, t);
  }

  _getStateParams(ease) {
    const base = {
      coreRadius: this.radius,
      coreAlpha: 1,
      coreHue: 160, // teal
      coreSat: 70,
      coreLight: 55,
      glowSize: this.radius * 0.6,
      glowAlpha: 0.15,
      particleSpeed: 1,
      particleAlpha: 1,
      ringAlpha: 1,
      waveform: false,
      rotationSpeed: 0.0003,
      pulseSpeed: 0.001,
      pulseAmount: 0.03,
    };

    switch (this.state) {
      case OrbState.IDLE:
        base.pulseSpeed = 0.0008;
        base.pulseAmount = 0.02;
        base.rotationSpeed = 0.0002;
        break;

      case OrbState.LISTENING:
        base.waveform = true;
        base.coreLight = 60;
        base.glowAlpha = 0.25;
        base.pulseSpeed = 0.0012;
        base.pulseAmount = 0.04;
        base.particleAlpha = 0.8 + this.audioAmplitude * 0.2;
        base.rotationSpeed = 0.0004;
        break;

      case OrbState.THINKING:
        base.coreHue = 170;
        base.coreLight = 60;
        base.glowAlpha = 0.3;
        base.pulseSpeed = 0.002;
        base.pulseAmount = 0.05;
        base.rotationSpeed = 0.001;
        base.particleSpeed = 1.5;
        break;

      case OrbState.SPEAKING:
        base.waveform = true;
        base.coreHue = 155;
        base.coreLight = 65;
        base.glowAlpha = 0.35;
        base.pulseSpeed = 0.0015;
        base.pulseAmount = 0.06;
        base.particleSpeed = 1.2;
        base.rotationSpeed = 0.0005;
        break;

      case OrbState.EXECUTING:
        base.coreHue = 140;
        base.coreSat = 80;
        base.coreLight = 58;
        base.glowAlpha = 0.4;
        base.glowSize = this.radius * 0.8;
        base.pulseSpeed = 0.0025;
        base.pulseAmount = 0.07;
        base.particleSpeed = 2;
        base.rotationSpeed = 0.0015;
        break;

      case OrbState.OFFLINE:
        base.coreSat = 10;
        base.coreLight = 30;
        base.glowAlpha = 0.05;
        base.particleAlpha = 0.15;
        base.ringAlpha = 0.1;
        base.pulseSpeed = 0;
        base.pulseAmount = 0;
        base.rotationSpeed = 0;
        break;

      case OrbState.ERROR:
        base.coreHue = 0;
        base.coreSat = 70;
        base.coreLight = 50;
        base.glowAlpha = 0.3;
        base.glowSize = this.radius * 0.7;
        base.pulseSpeed = 0.003;
        base.pulseAmount = 0.08;
        break;
    }

    // Apply pulse
    const pulse = Math.sin(t * base.pulseSpeed) * base.pulseAmount;
    base.coreRadius = this.radius * (1 + pulse);

    return base;
  }

  _drawOuterGlow(ctx, params) {
    const grad = ctx.createRadialGradient(
      this.cx, this.cy, params.coreRadius * 0.5,
      this.cx, this.cy, params.coreRadius + params.glowSize
    );
    const hue = params.coreHue;
    const sat = params.coreSat;
    grad.addColorStop(0, `hsla(${hue}, ${sat}%, ${params.coreLight}%, ${params.glowAlpha * 0.6})`);
    grad.addColorStop(0.5, `hsla(${hue}, ${sat}%, ${params.coreLight}%, ${params.glowAlpha * 0.2})`);
    grad.addColorStop(1, `hsla(${hue}, ${sat}%, ${params.coreLight}%, 0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(this.cx, this.cy, params.coreRadius + params.glowSize, 0, Math.PI * 2);
    ctx.fill();
  }

  _drawCore(ctx, params, t) {
    const r = params.coreRadius;
    const hue = params.coreHue;
    const sat = params.coreSat;
    const light = params.coreLight;

    // Core gradient
    const grad = ctx.createRadialGradient(
      this.cx - r * 0.2, this.cy - r * 0.2, 0,
      this.cx, this.cy, r
    );
    grad.addColorStop(0, `hsla(${hue}, ${sat + 10}%, ${light + 15}%, 0.95)`);
    grad.addColorStop(0.6, `hsla(${hue}, ${sat}%, ${light}%, 0.85)`);
    grad.addColorStop(1, `hsla(${hue}, ${sat - 10}%, ${light - 10}%, 0.6)`);

    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(this.cx, this.cy, r, 0, Math.PI * 2);
    ctx.fill();

    // Inner highlight
    const hlGrad = ctx.createRadialGradient(
      this.cx - r * 0.3, this.cy - r * 0.3, 0,
      this.cx, this.cy, r * 0.7
    );
    hlGrad.addColorStop(0, `hsla(${hue}, 60%, 80%, 0.25)`);
    hlGrad.addColorStop(1, `hsla(${hue}, 60%, 80%, 0)`);
    ctx.fillStyle = hlGrad;
    ctx.beginPath();
    ctx.arc(this.cx, this.cy, r * 0.7, 0, Math.PI * 2);
    ctx.fill();
  }

  _drawKernel(ctx, params, t) {
    if (this.state === OrbState.OFFLINE) return;

    // Central bright point
    const kr = params.coreRadius * 0.15;
    const hue = params.coreHue;
    const alpha = 0.5 + Math.sin(t * 0.002) * 0.2;

    const grad = ctx.createRadialGradient(this.cx, this.cy, 0, this.cx, this.cy, kr);
    grad.addColorStop(0, `hsla(${hue}, 50%, 90%, ${alpha})`);
    grad.addColorStop(1, `hsla(${hue}, 50%, 90%, 0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(this.cx, this.cy, kr, 0, Math.PI * 2);
    ctx.fill();
  }

  _drawRings(ctx, params, t) {
    if (this.state === OrbState.OFFLINE) return;

    ctx.save();
    ctx.translate(this.cx, this.cy);
    ctx.rotate(t * 0.0001);

    for (const ring of this.rings) {
      const r = ring.radius * (1 + Math.sin(t * 0.0005) * 0.02);
      const alpha = ring.alpha * params.ringAlpha;
      const hue = params.coreHue;

      ctx.strokeStyle = `hsla(${hue}, 60%, 60%, ${alpha})`;
      ctx.lineWidth = ring.width;
      ctx.setLineDash([4 + ring.dashOffset * 0.1, 8]);
      ctx.lineDashOffset = t * ring.speed * 10;
      ctx.beginPath();
      ctx.arc(0, 0, r, 0, Math.PI * 2);
      ctx.stroke();
    }

    ctx.setLineDash([]);
    ctx.restore();
  }

  _drawParticles(ctx, params, t) {
    if (this.state === OrbState.OFFLINE && params.particleAlpha < 0.1) return;

    const hue = params.coreHue;

    for (const p of this.particles) {
      const angle = p.angle + t * p.speed * params.particleSpeed + p.drift * t;
      const dist = p.dist + Math.sin(t * 0.001 + p.angle) * 3;
      const x = this.cx + Math.cos(angle) * dist;
      const y = this.cy + Math.sin(angle) * dist;
      const alpha = p.alpha * params.particleAlpha;

      ctx.fillStyle = `hsla(${hue}, 60%, 70%, ${alpha})`;
      ctx.beginPath();
      ctx.arc(x, y, p.size, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  _drawWaveform(ctx, params, t) {
    if (!params.waveform) return;

    const hue = params.coreHue;
    const amp = this.audioAmplitude;
    const r = params.coreRadius * 1.15;
    const segments = 64;

    ctx.save();
    ctx.translate(this.cx, this.cy);
    ctx.rotate(t * 0.0003);

    ctx.strokeStyle = `hsla(${hue}, 70%, 65%, ${0.3 + amp * 0.4})`;
    ctx.lineWidth = 1.5;
    ctx.beginPath();

    for (let i = 0; i <= segments; i++) {
      const angle = (Math.PI * 2 * i) / segments;
      const wave = Math.sin(angle * 8 + t * 0.005) * amp * 12;
      const wave2 = Math.sin(angle * 13 - t * 0.003) * amp * 6;
      const rr = r + wave + wave2;
      const x = Math.cos(angle) * rr;
      const y = Math.sin(angle) * rr;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }

    ctx.closePath();
    ctx.stroke();
    ctx.restore();
  }

  _easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }
}

// Export for ES modules and global
if (typeof module !== "undefined" && module.exports) {
  module.exports = { NovaOrb, OrbState };
} else {
  window.NovaOrb = NovaOrb;
  window.OrbState = OrbState;
}
