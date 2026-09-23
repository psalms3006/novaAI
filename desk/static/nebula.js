/* nebula.js — the deep-space backdrop NOVA's orb floats in.
 *
 * A separate renderer from orb3d.js, on purpose: the background drifts
 * slowly enough that it can run at half resolution and half the frame
 * rate with nobody noticing, while the orb -- the thing actually being
 * looked at -- stays full quality. Mixing the two into one scene would
 * mean either paying full cost for a background nobody is watching
 * closely, or dropping the orb's own quality to afford it.
 *
 * Single fullscreen shader plane: a dark gradient base, two octaves of
 * drifting colored noise for nebula cloud, two layers of twinkling stars
 * at different densities, and a soft glow pooled behind the orb that
 * tracks the orb's live color -- so the orb reads as something actually
 * lighting the space behind it, not a sprite pasted over a static image.
 */
import * as THREE from "three";

const VERT = /* glsl */ `
varying vec2 vUv;
void main(){
  vUv = uv;
  gl_Position = vec4(position.xy, 0.0, 1.0);
}`;

// Cheap 2D hash/value noise -- this does not need simplex-quality
// smoothness at this scale; it is blurred by the octave blending and
// viewed from a distance (the intent is soft cloud, not detail).
const NOISE2D = /* glsl */ `
float hash21(vec2 p){
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}
float noise2d(vec2 p){
  vec2 i = floor(p), f = fract(p);
  float a = hash21(i), b = hash21(i + vec2(1.0, 0.0));
  float c = hash21(i + vec2(0.0, 1.0)), d = hash21(i + vec2(1.0, 1.0));
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}
float fbm(vec2 p){
  float v = 0.0, amp = 0.5;
  for (int i = 0; i < 4; i++) {
    v += amp * noise2d(p);
    p *= 2.02;
    amp *= 0.55;
  }
  return v;
}`;

const FRAG = /* glsl */ `
precision highp float;
varying vec2 vUv;
uniform float uTime;
uniform vec2 uResolution;
uniform vec3 uOrbColor;
uniform float uOrbGlow;
${NOISE2D}

// Star layer: bright, sparse points from a hashed grid, twinkling on
// their own per-cell phase so they do not all pulse in lockstep.
float stars(vec2 uv, float density, float twinkleSpeed, float seed){
  vec2 grid = uv * density;
  vec2 cell = floor(grid);
  vec2 f = fract(grid);
  float h = hash21(cell + seed);
  if (h < 0.90) return 0.0;                 // most cells: no star
  vec2 starPos = vec2(hash21(cell + seed + 1.0), hash21(cell + seed + 2.0));
  float d = length(f - starPos);
  float core = smoothstep(0.06, 0.0, d);
  float twinkle = 0.55 + 0.45 * sin(uTime * twinkleSpeed + h * 62.0);
  return core * twinkle * smoothstep(0.90, 1.0, h);
}

void main(){
  vec2 uv = vUv;
  vec2 centered = uv - 0.5;
  centered.x *= uResolution.x / uResolution.y;

  // Base: dark, slightly brighter center, vignette toward the corners.
  float radial = length(centered);
  vec3 base = mix(vec3(0.02, 0.025, 0.05), vec3(0.005, 0.006, 0.012),
                  smoothstep(0.0, 0.9, radial));
  float vignette = smoothstep(1.1, 0.35, radial);
  base *= mix(0.55, 1.0, vignette);

  // Nebula: two independently-drifting fbm layers, different colors.
  vec2 driftA = centered * 1.6 + vec2(uTime * 0.012, uTime * 0.007);
  vec2 driftB = centered * 2.3 - vec2(uTime * 0.008, uTime * 0.015);
  float cloudA = fbm(driftA);
  float cloudB = fbm(driftB + 4.7);
  vec3 nebulaColorA = vec3(0.10, 0.35, 0.30);   // cool green
  vec3 nebulaColorB = vec3(0.22, 0.10, 0.32);   // touch of magenta
  vec3 nebula = nebulaColorA * pow(cloudA, 2.2) * 0.55
              + nebulaColorB * pow(cloudB, 2.4) * 0.40;
  nebula *= 0.5 + 0.5 * vignette;

  // Stars: dense/fine layer + sparse/larger layer.
  float starsFine = stars(uv, 140.0, 1.6, 11.0) * 0.7;
  float starsCoarse = stars(uv, 55.0, 0.9, 53.0) * 1.0;
  vec3 starColor = vec3(0.85, 0.92, 1.0) * (starsFine + starsCoarse);

  // Glow pooled behind the orb, tracking its live color -- the orb
  // reads as though it is actually lighting the space around it.
  float orbGlow = smoothstep(0.55, 0.0, radial) * uOrbGlow;
  vec3 glow = uOrbColor * orbGlow * 0.5;

  vec3 color = base + nebula + starColor + glow;
  gl_FragColor = vec4(color, 1.0);
}`;

export class NovaNebula {
  constructor(container) {
    const cv = document.createElement("canvas");
    cv.id = "nebula-canvas";
    cv.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block;z-index:0;";
    (container || document.body).insertBefore(cv, container?.firstChild || null);
    this.canvas = cv;
    this.clock = new THREE.Clock();
    this._running = false;
    this._ok = false;
    this._frameSkip = false;
    this._frameCounter = 0;
    //: Eased toward the orb's live color/glow, same "never snap"
    //: principle as orb3d.js -- a hard color cut here would read as a
    //: flash behind the orb every time it changes state.
    this.color = new THREE.Color(0x4fd8e8);
    this.glow = 0.4;
    this._targetColor = this.color.clone();
    this._targetGlow = 0.4;

    try {
      this._init();
      this._ok = true;
    } catch (e) {
      console.warn("[NOVA] nebula background unavailable:", e);
      this._ok = false;
    }
  }

  get available() { return this._ok; }

  _init() {
    const r = this.canvas.getBoundingClientRect();
    const w = Math.max(2, r.width), h = Math.max(2, r.height);

    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas, antialias: false, alpha: false,
      powerPreference: "low-power",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.25));
    this.renderer.setSize(w, h, false);

    this.scene = new THREE.Scene();
    this.camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

    this.uniforms = {
      uTime: { value: 0 },
      uResolution: { value: new THREE.Vector2(w, h) },
      uOrbColor: { value: this.color.clone() },
      uOrbGlow: { value: this.glow },
    };

    const geo = new THREE.PlaneGeometry(2, 2);
    const mat = new THREE.ShaderMaterial({
      vertexShader: VERT, fragmentShader: FRAG, uniforms: this.uniforms,
      depthTest: false, depthWrite: false,
    });
    this.mesh = new THREE.Mesh(geo, mat);
    this.scene.add(this.mesh);

    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
    try {
      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(this.canvas);
    } catch (e) {
      requestAnimationFrame(() => this.resize());
    }
    this.resize();
  }

  resize() {
    if (!this._ok) return;
    const r = this.canvas.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    this.renderer.setSize(r.width, r.height, false);
    this.uniforms.uResolution.value.set(r.width, r.height);
  }

  /** Called from the orb's own state loop so the glow behind it tracks
   * what the orb is actually doing, without this module needing to know
   * anything about orb states itself. */
  setOrbColor(hexOrColor, glowLevel) {
    if (hexOrColor != null) {
      if (typeof hexOrColor === "number") this._targetColor.setHex(hexOrColor);
      else this._targetColor.set(hexOrColor);
    }
    if (glowLevel != null) this._targetGlow = Math.max(0, Math.min(1.5, glowLevel));
  }

  /** Halves the render rate (every other frame skipped) and caps DPR
   * lower still, for weaker devices -- the background drifts slowly
   * enough that this is invisible. */
  setPerformanceMode(on) {
    this._frameSkip = !!on;
    if (this._ok) {
      this.renderer.setPixelRatio(on ? 0.75 : Math.min(window.devicePixelRatio || 1, 1.25));
    }
  }

  start() {
    if (!this._ok || this._running) return;
    this._running = true;
    this._loop();
  }

  stop() {
    this._running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    window.removeEventListener("resize", this._onResize);
    try { if (this._ro) { this._ro.disconnect(); this._ro = null; } } catch (e) {}
  }

  dispose() {
    this.stop();
    try {
      this.mesh.geometry.dispose();
      this.mesh.material.dispose();
      this.renderer.dispose();
    } catch (e) {}
  }

  _loop() {
    if (!this._running) return;
    this._raf = requestAnimationFrame(() => this._loop());
    if (document.hidden) return;

    this._frameCounter++;
    if (this._frameSkip && (this._frameCounter % 2 === 0)) return;

    const dt = Math.min(this.clock.getDelta(), 0.1) * (this._frameSkip ? 2 : 1);
    const k = 1 - Math.pow(0.02, dt);
    this.color.lerp(this._targetColor, k);
    this.glow += (this._targetGlow - this.glow) * k;

    this.uniforms.uTime.value = this.clock.elapsedTime;
    this.uniforms.uOrbColor.value.copy(this.color);
    this.uniforms.uOrbGlow.value = this.glow;

    this.renderer.render(this.scene, this.camera);
  }
}

window.NovaNebula = NovaNebula;
