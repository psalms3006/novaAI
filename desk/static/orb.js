/* orb.js — NOVA's presence.
 *
 * A fluid, waveform-driven organic blob: a closed loop whose radius is
 * displaced by layered sine bands (the "waveform") plus slow value noise (the
 * "fluid"), rendered as a filled body, a bright rim, and a set of concentric
 * offset contours that read as a mesh shell. Particles drift in the field
 * around it and are pushed outward when NOVA speaks.
 *
 * Motion model — states retarget an eased scale/halo on an interval, ring
 * bands counter-rotate at per-state speeds, expanding pulses spawn
 * probabilistically and are culled past a radius limit, and rim particles decay
 * with damped velocity. (Adapted from the reference HudCanvas behaviour and
 * re-expressed on canvas2d rather than QPainter.)
 *
 * Live audio drives it when available: setAmplitude() is fed by the Gemini Live
 * playback path, so the surface genuinely tracks NOVA's voice instead of
 * animating on a timer while she talks.
 */

const STATES = {
  idle:       { hue: 172, sat: 72, amp: 0.06, speed: 0.10, spin: 0.55, glow: 0.55, particles: 26 },
  listening:  { hue: 186, sat: 85, amp: 0.16, speed: 0.22, spin: 0.90, glow: 0.85, particles: 44 },
  hearing:  { hue: 186, sat: 85, amp: 0.16, speed: 0.22, spin: 0.90, glow: 0.85, particles: 44 },
  thinking:   { hue: 205, sat: 80, amp: 0.11, speed: 0.34, spin: 1.60, glow: 0.75, particles: 38 },
  speaking:   { hue: 168, sat: 90, amp: 0.30, speed: 0.40, spin: 1.30, glow: 1.00, particles: 60 },
  working:    { hue: 152, sat: 78, amp: 0.14, speed: 0.28, spin: 1.15, glow: 0.80, particles: 46 },
  delegating: { hue: 268, sat: 76, amp: 0.18, speed: 0.30, spin: 1.45, glow: 0.85, particles: 52 },
  offline:    { hue: 210, sat: 10, amp: 0.03, speed: 0.05, spin: 0.20, glow: 0.25, particles: 12 },
  error:      { hue: 8,   sat: 82, amp: 0.09, speed: 0.16, spin: 0.45, glow: 0.70, particles: 20 },
};
STATES.executing = STATES.working;
STATES.connecting = STATES.thinking;

/* Deterministic value noise — cheap, smooth, no dependency. */
function makeNoise(seed = 1) {
  const p = new Float32Array(256);
  let s = seed;
  for (let i = 0; i < 256; i++) {
    s = (s * 16807) % 2147483647;
    p[i] = s / 2147483647;
  }
  return (x) => {
    const i = Math.floor(x);
    const f = x - i;
    const a = p[((i % 256) + 256) % 256];
    const b = p[(((i + 1) % 256) + 256) % 256];
    const u = f * f * (3 - 2 * f); // smoothstep
    return a + (b - a) * u;
  };
}

class NovaOrb {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);

    this.state = "idle";
    this.params = { ...STATES.idle };
    this.target = { ...STATES.idle };

    this.t = 0;
    this.spin = 0;
    this.amplitude = 0;      // live audio level, 0..1
    this._ampDecay = 0;

    this.scale = 1;
    this.tgtScale = 1;
    this.halo = 0.55;
    this.tgtHalo = 0.55;
    this._lastRetarget = 0;

    this.pulses = [0.15, 0.45, 0.75];
    this.particles = [];
    this.noise = [makeNoise(7), makeNoise(31), makeNoise(101)];

    this._running = false;
    this._frame = null;
    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
    this.resize();
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(1, rect.width || this.canvas.width);
    const h = Math.max(1, rect.height || this.canvas.height);
    this.canvas.width = w * this.dpr;
    this.canvas.height = h * this.dpr;
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.scale(this.dpr, this.dpr);
    this.w = w;
    this.h = h;
    this.cx = w / 2;
    this.cy = h / 2;
    this.baseR = Math.min(w, h) * 0.24;
  }

  setState(next) {
    if (!STATES[next] || next === this.state) return;
    this.state = next;
    this.target = { ...STATES[next] };
    this._seedParticles();
  }

  /** Live audio level (0..1) from the Live playback path. */
  setAmplitude(level) {
    const v = Math.max(0, Math.min(1, level || 0));
    if (v > this.amplitude) this.amplitude = v;   // attack fast
    this._ampDecay = 0.92;                        // release slow
  }

  start() {
    if (this._running) return;
    this._running = true;
    this._tick();
  }

  stop() {
    this._running = false;
    if (this._frame) cancelAnimationFrame(this._frame);
    window.removeEventListener("resize", this._onResize);
  }

  _seedParticles() {
    const want = this.target.particles;
    while (this.particles.length < want) {
      const a = Math.random() * Math.PI * 2;
      const r = this.baseR * (1.05 + Math.random() * 1.5);
      this.particles.push({
        a, r,
        drift: (Math.random() - 0.5) * 0.0016,
        vr: 0,
        size: 0.6 + Math.random() * 1.9,
        life: 0.35 + Math.random() * 0.65,
        hueShift: (Math.random() - 0.5) * 60,
      });
    }
    if (this.particles.length > want) this.particles.length = want;
  }

  _tick() {
    if (!this._running) return;
    this.t += 1;

    // ── ease params toward the state's targets ───────────────────────────
    const k = 0.06;
    for (const key of ["hue", "sat", "amp", "speed", "spin", "glow"]) {
      this.params[key] += (this.target[key] - this.params[key]) * k;
    }

    // ── retarget scale/halo on an interval; faster + wider when speaking ──
    const now = this.t / 60;
    const interval = this.state === "speaking" ? 0.12 : 0.5;
    if (now - this._lastRetarget > interval) {
      if (this.state === "speaking") {
        this.tgtScale = 1.05 + Math.random() * 0.09;
        this.tgtHalo = 0.85 + Math.random() * 0.35;
      } else if (this.state === "offline") {
        this.tgtScale = 0.995 + Math.random() * 0.006;
        this.tgtHalo = 0.18 + Math.random() * 0.1;
      } else {
        this.tgtScale = 1.0 + Math.random() * 0.02;
        this.tgtHalo = 0.45 + Math.random() * 0.25;
      }
      this._lastRetarget = now;
    }
    const ease = this.state === "speaking" ? 0.34 : 0.12;
    this.scale += (this.tgtScale - this.scale) * ease;
    this.halo += (this.tgtHalo - this.halo) * ease;

    this.spin += this.params.spin * 0.0022;
    this.amplitude *= this._ampDecay || 0.9;

    // ── expanding pulse rings ────────────────────────────────────────────
    const limit = 1.9;
    const pulseSpeed = this.state === "speaking" ? 0.010 : 0.005;
    this.pulses = this.pulses.map((r) => r + pulseSpeed).filter((r) => r < limit);
    const spawnChance = this.state === "speaking" ? 0.05 : 0.014;
    if (this.pulses.length < 3 && Math.random() < spawnChance) this.pulses.push(0.1);

    this._draw();
    this._frame = requestAnimationFrame(() => this._tick());
  }

  /** Radius of the fluid surface at angle `a`. */
  _surface(a, t) {
    const p = this.params;
    const drive = p.amp + this.amplitude * 0.45;
    // Layered waveform bands — the "voice" of the shape.
    let d =
      Math.sin(a * 3 + t * 1.1) * 0.34 +
      Math.sin(a * 5 - t * 0.7) * 0.22 +
      Math.sin(a * 8 + t * 1.6) * 0.13;
    // Fluid noise — slow, organic wander so it never looks periodic.
    d += (this.noise[0](a * 1.6 + t * 0.5) - 0.5) * 0.9;
    d += (this.noise[1](a * 3.1 - t * 0.31) - 0.5) * 0.5;
    return this.baseR * this.scale * (1 + d * drive);
  }

  _ringPath(ctx, t, inflate) {
    const STEPS = 168;
    ctx.beginPath();
    for (let i = 0; i <= STEPS; i++) {
      const a = (i / STEPS) * Math.PI * 2;
      const r = this._surface(a + this.spin, t) * inflate;
      const x = this.cx + Math.cos(a) * r;
      const y = this.cy + Math.sin(a) * r;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
  }

  _draw() {
    const ctx = this.ctx;
    const p = this.params;
    const t = this.t * 0.01 * (0.5 + p.speed);
    const H = p.hue;
    const S = p.sat;

    ctx.clearRect(0, 0, this.w, this.h);
    ctx.globalCompositeOperation = "lighter";

    // ── ambient halo ─────────────────────────────────────────────────────
    const haloR = this.baseR * (2.6 + this.halo);
    const halo = ctx.createRadialGradient(this.cx, this.cy, 0, this.cx, this.cy, haloR);
    halo.addColorStop(0, `hsla(${H}, ${S}%, 56%, ${0.16 * p.glow})`);
    halo.addColorStop(0.5, `hsla(${H}, ${S}%, 45%, ${0.05 * p.glow})`);
    halo.addColorStop(1, "hsla(0,0%,0%,0)");
    ctx.fillStyle = halo;
    ctx.fillRect(0, 0, this.w, this.h);

    // ── expanding pulses ─────────────────────────────────────────────────
    for (const pr of this.pulses) {
      const alpha = Math.max(0, (1 - pr / 1.9)) * 0.22 * p.glow;
      if (alpha <= 0.002) continue;
      ctx.strokeStyle = `hsla(${H}, ${S}%, 68%, ${alpha})`;
      ctx.lineWidth = 1;
      this._ringPath(ctx, t, 1 + pr);
      ctx.stroke();
    }

    // ── particle field ───────────────────────────────────────────────────
    const push = this.state === "speaking" ? 0.35 : 0.06;
    for (const q of this.particles) {
      q.a += q.drift * (1 + p.spin);
      q.vr += (Math.random() - 0.5) * 0.05 + this.amplitude * push;
      q.vr *= 0.97;                                   // damping
      q.r += q.vr;
      const min = this.baseR * 1.02;
      const max = this.baseR * 2.9;
      if (q.r < min) { q.r = min; q.vr = Math.abs(q.vr) * 0.5; }
      if (q.r > max) { q.r = max; q.vr = -Math.abs(q.vr) * 0.5; }
      const x = this.cx + Math.cos(q.a) * q.r;
      const y = this.cy + Math.sin(q.a) * q.r;
      const fall = 1 - (q.r - min) / (max - min);
      const alpha = q.life * fall * 0.85 * p.glow;
      ctx.fillStyle = `hsla(${H + q.hueShift}, ${S}%, 72%, ${alpha})`;
      ctx.beginPath();
      ctx.arc(x, y, q.size, 0, Math.PI * 2);
      ctx.fill();
    }

    // ── mesh shell: offset contours ──────────────────────────────────────
    for (let i = 4; i >= 1; i--) {
      const inflate = 1 + i * 0.075;
      const alpha = (0.10 - i * 0.015) * p.glow + 0.02;
      ctx.strokeStyle = `hsla(${H + i * 6}, ${S}%, 62%, ${alpha})`;
      ctx.lineWidth = 1;
      this._ringPath(ctx, t - i * 0.05, inflate);
      ctx.stroke();
    }

    // ── body ─────────────────────────────────────────────────────────────
    this._ringPath(ctx, t, 1);
    const body = ctx.createRadialGradient(
      this.cx, this.cy, this.baseR * 0.15,
      this.cx, this.cy, this.baseR * 1.25,
    );
    body.addColorStop(0, `hsla(${H + 8}, ${S}%, 62%, ${0.42 * p.glow})`);
    body.addColorStop(0.55, `hsla(${H}, ${S}%, 46%, ${0.20 * p.glow})`);
    body.addColorStop(1, `hsla(${H - 10}, ${S}%, 34%, 0.02)`);
    ctx.fillStyle = body;
    ctx.fill();

    // rim
    ctx.strokeStyle = `hsla(${H}, ${S}%, 78%, ${0.65 * p.glow})`;
    ctx.lineWidth = 1.6;
    ctx.stroke();

    // ── core ─────────────────────────────────────────────────────────────
    const coreR = this.baseR * (0.30 + this.amplitude * 0.16);
    const core = ctx.createRadialGradient(this.cx, this.cy, 0, this.cx, this.cy, coreR);
    core.addColorStop(0, `hsla(${H + 14}, 100%, 92%, ${0.80 * p.glow})`);
    core.addColorStop(0.45, `hsla(${H + 6}, ${S}%, 70%, ${0.30 * p.glow})`);
    core.addColorStop(1, "hsla(0,0%,0%,0)");
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(this.cx, this.cy, coreR, 0, Math.PI * 2);
    ctx.fill();

    ctx.globalCompositeOperation = "source-over";
  }
}

window.NovaOrb = NovaOrb;
