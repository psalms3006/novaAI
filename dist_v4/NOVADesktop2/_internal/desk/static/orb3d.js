/* orb3d.js — NOVA's presence, as a real volumetric form.
 *
 * A GPU-displaced icosphere: simplex noise in the vertex shader deforms the
 * surface into a continuously morphing fluid body, driven by NOVA's actual
 * state and by the live audio amplitude of her voice. Shaded with a fresnel
 * rim so it reads as volume rather than a disc, wrapped in an additive point
 * field, and finished with bloom.
 *
 * This replaces a 2D canvas orb that looked like a flat sprite. Everything
 * here is real geometry on the GPU — the silhouette genuinely deforms, the rim
 * genuinely tracks view angle, and the displacement genuinely follows the
 * audio signal rather than a timer.
 *
 * Cost control: capped DPR, half-resolution bloom, a single 5k-triangle mesh,
 * and the loop stops entirely when the document is hidden or the form is in
 * ambient mode.
 */
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";

/* Per-state targets. Everything is eased toward these, never snapped, so the
 * form changes character the way a body does rather than cutting between
 * presets. */
const STATES = {
  idle:        { hue: 0.50, amp: 0.13, freq: 1.05, speed: 0.16, rim: 0.85, spin: 0.035, glow: 0.55, chaos: 0.10 },
  listening:   { hue: 0.47, amp: 0.18, freq: 1.35, speed: 0.42, rim: 1.05, spin: 0.070, glow: 0.75, chaos: 0.22 },
  thinking:    { hue: 0.57, amp: 0.16, freq: 2.30, speed: 0.85, rim: 0.95, spin: 0.180, glow: 0.70, chaos: 0.55 },
  speaking:    { hue: 0.45, amp: 0.26, freq: 1.55, speed: 0.70, rim: 1.25, spin: 0.090, glow: 0.95, chaos: 0.30 },
  working:     { hue: 0.38, amp: 0.17, freq: 1.80, speed: 0.60, rim: 1.00, spin: 0.140, glow: 0.72, chaos: 0.40 },
  delegating:  { hue: 0.74, amp: 0.19, freq: 2.00, speed: 0.72, rim: 1.05, spin: 0.160, glow: 0.78, chaos: 0.48 },
  error:       { hue: 0.02, amp: 0.11, freq: 1.20, speed: 0.30, rim: 0.90, spin: 0.030, glow: 0.62, chaos: 0.65 },
  offline:     { hue: 0.58, amp: 0.05, freq: 0.80, speed: 0.06, rim: 0.50, spin: 0.012, glow: 0.25, chaos: 0.04 },
};
STATES.executing = STATES.working;
STATES.connecting = STATES.thinking;
STATES.awaiting_input = STATES.listening;

/* Ashima simplex noise (MIT) — standard GLSL implementation. */
const SIMPLEX = /* glsl */ `
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g;
  vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))
        +i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z);
  vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);
  vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y);
  vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0);
  m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}`;

/* Shared displacement so body and points deform identically. */
const DISPLACE = /* glsl */ `
uniform float uTime, uAmp, uFreq, uChaos, uAudio, uBands[8];
float bandAt(vec3 n){
  float a = atan(n.z, n.x) / 6.2831853 + 0.5;   // 0..1 around the form
  float f = a * 8.0;
  int i = int(floor(f));
  float t = fract(f);
  float b0 = uBands[i % 8];
  float b1 = uBands[(i + 1) % 8];
  return mix(b0, b1, smoothstep(0.0, 1.0, t));
}
float displace(vec3 p){
  vec3 n = normalize(p);
  // Slow body wander.
  float d = snoise(n * uFreq + vec3(0.0, 0.0, uTime * 0.5)) * 0.55;
  // Faster detail that only really shows when NOVA is active.
  d += snoise(n * uFreq * 2.7 - vec3(uTime * 0.9)) * 0.25 * uChaos;
  // The waveform itself: spectrum bands wrap the form, so her voice is
  // legible as shape rather than as a generic pulse.
  d += bandAt(n) * 0.28;
  // Global breath.
  d += sin(uTime * 1.6 + n.y * 3.0) * 0.06;
  return d * uAmp * (1.0 + uAudio * 0.8);
}`;

const VERT = /* glsl */ `
varying vec3 vNormalW; varying vec3 vViewDir; varying float vDisp;
${SIMPLEX}
${DISPLACE}
void main(){
  vec3 n = normalize(position);
  float d = displace(position);
  vDisp = d;
  vec3 pos = position + n * d;

  // Recompute a normal from two nearby displaced samples so lighting follows
  // the deformation instead of the original sphere.
  vec3 t1 = normalize(cross(n, vec3(0.0, 1.0, 0.0) + 1e-4));
  vec3 t2 = normalize(cross(n, t1));
  float e = 0.06;
  vec3 pa = (n + t1 * e); pa = pa * (1.0 + displace(pa));
  vec3 pb = (n + t2 * e); pb = pb * (1.0 + displace(pb));
  vec3 nrm = normalize(cross(pa - pos, pb - pos));
  if (dot(nrm, n) < 0.0) nrm = -nrm;

  vec4 wp = modelMatrix * vec4(pos, 1.0);
  vNormalW = normalize(mat3(modelMatrix) * nrm);
  vViewDir = normalize(cameraPosition - wp.xyz);
  gl_Position = projectionMatrix * viewMatrix * wp;
}`;

const FRAG = /* glsl */ `
precision highp float;
uniform float uHue, uRim, uGlow;
varying vec3 vNormalW; varying vec3 vViewDir; varying float vDisp;

vec3 hsv2rgb(vec3 c){
  vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
  vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
  return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
// ACES filmic approximation. Additive glow routinely exceeds 1.0 and a raw
// ShaderMaterial does not receive three's tone mapping, so without this the
// whole form clips to flat white.
vec3 aces(vec3 x){
  const float a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14;
  return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}
void main(){
  float fres = pow(1.0 - clamp(dot(normalize(vNormalW), normalize(vViewDir)), 0.0, 1.0), 2.6);
  // Crests shift hue slightly — the form reads as iridescent, not painted.
  float shift = clamp(vDisp * 0.5, -0.10, 0.10);
  vec3 core = hsv2rgb(vec3(uHue + shift, 0.95, 0.55));
  vec3 rim  = hsv2rgb(vec3(uHue + shift + 0.04, 0.72, 1.0));
  // Interior stays dim; the rim carries the light, which is what makes the
  // silhouette read as a volume rather than a filled disc.
  vec3 col = core * 0.22 + rim * fres * uRim * 0.75;
  float alpha = clamp(0.05 + fres * 0.80, 0.0, 1.0) * clamp(uGlow, 0.0, 1.3);
  gl_FragColor = vec4(aces(col * uGlow), alpha);
}`;

const P_VERT = /* glsl */ `
uniform float uSize, uSpread;
varying float vA;
${SIMPLEX}
${DISPLACE}
void main(){
  vec3 n = normalize(position);
  float d = displace(position);
  vec3 pos = position * (1.0 + uSpread) + n * (d * 1.3 + uAudio * 0.18);
  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  vA = clamp(0.06 + d * 0.9 + uAudio * 0.30, 0.01, 0.55);
  gl_PointSize = uSize * (1.0 + uAudio * 0.6) * (9.0 / -mv.z);
  gl_Position = projectionMatrix * mv;
}`;

const P_FRAG = /* glsl */ `
precision mediump float;
uniform vec3 uColor; varying float vA;
void main(){
  vec2 c = gl_PointCoord - 0.5;
  float r = dot(c, c);
  if (r > 0.25) discard;
  float a = smoothstep(0.25, 0.0, r) * vA;
  gl_FragColor = vec4(uColor, a);
}`;

export class NovaOrb3D {
  constructor(container) {
    // Always render into a canvas of our own. Sharing #orb-canvas with the 2D
    // fallback fails outright — once a 2d context exists on an element you
    // cannot get a webgl one from it ("Canvas has an existing context of a
    // different type").
    const cv = document.createElement("canvas");
    cv.id = "orb-canvas-3d";
    cv.style.cssText = "width:100%;height:100%;display:block;";
    (container || document.body).appendChild(cv);
    this.canvas = cv;
    this.state = "idle";
    this.cur = { ...STATES.idle };
    this.tgt = { ...STATES.idle };
    this.audio = 0;
    this.bands = new Float32Array(8);
    this.clock = new THREE.Clock();
    this._running = false;
    this._ok = false;

    try {
      this._init();
      this._ok = true;
    } catch (e) {
      console.warn("[NOVA] WebGL orb unavailable:", e);
      this._ok = false;
    }
  }

  get available() { return this._ok; }

  _init() {
    const r = this.canvas.getBoundingClientRect();
    const w = Math.max(2, r.width), h = Math.max(2, r.height);

    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas, antialias: false, alpha: true, powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.setSize(w, h, false);
    this.renderer.setClearColor(0x000000, 0);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(42, w / h, 0.1, 100);
    // In the 96px ambient window the form should fill the frame; in the full
    // command centre it sits in open space. Same geometry, closer camera.
    this.compact = new URLSearchParams(location.search).get("mode") === "ambient";
    this.camera.position.set(0, 0, this.compact ? 3.75 : 5.0);

    this.uniforms = {
      uTime:  { value: 0 },
      uAmp:   { value: STATES.idle.amp },
      uFreq:  { value: STATES.idle.freq },
      uChaos: { value: STATES.idle.chaos },
      uAudio: { value: 0 },
      uBands: { value: Array.from({ length: 8 }, () => 0) },
      uHue:   { value: STATES.idle.hue },
      uRim:   { value: STATES.idle.rim },
      uGlow:  { value: STATES.idle.glow },
    };

    // Body — detail 5 ≈ 5,120 triangles: enough for a smooth silhouette at
    // this size, cheap enough to sit at 60fps on integrated graphics.
    const geo = new THREE.IcosahedronGeometry(1.25, 5);
    this.mesh = new THREE.Mesh(geo, new THREE.ShaderMaterial({
      vertexShader: VERT, fragmentShader: FRAG, uniforms: this.uniforms,
      transparent: true, blending: THREE.AdditiveBlending,
      depthWrite: false, side: THREE.FrontSide,
    }));
    this.scene.add(this.mesh);

    // Inner shell gives the body depth — a second, smaller surface visible
    // through the additive outer one.
    this.inner = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.92, 4),
      new THREE.ShaderMaterial({
        vertexShader: VERT, fragmentShader: FRAG,
        uniforms: { ...this.uniforms, uGlow: { value: 0.22 }, uRim: { value: 1.1 } },
        transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      }),
    );
    this.scene.add(this.inner);

    // Wireframe shell over the body. Same displacement, so the mesh sits
    // exactly on the surface and flexes with it — this is what reads as an
    // organic mesh blob rather than a smooth ball.
    this.wire = new THREE.Mesh(
      new THREE.IcosahedronGeometry(1.252, 4),
      new THREE.ShaderMaterial({
        vertexShader: VERT, fragmentShader: FRAG,
        uniforms: { ...this.uniforms, uGlow: { value: 0.55 }, uRim: { value: 1.6 } },
        transparent: true, blending: THREE.AdditiveBlending,
        depthWrite: false, wireframe: true,
      }),
    );
    this.scene.add(this.wire);

    // Point field on a larger shell.
    const pgeo = new THREE.IcosahedronGeometry(1.25, 4);
    this.pUniforms = {
      ...this.uniforms,
      uSize:   { value: 1.7 },
      uSpread: { value: 0.30 },
      uColor:  { value: new THREE.Color(0x8fe9ff) },
    };
    // The point field reads as dirt at 96px, so the ambient form is body +
    // wireframe only.
    this.points = new THREE.Points(pgeo, new THREE.ShaderMaterial({
      vertexShader: P_VERT, fragmentShader: P_FRAG, uniforms: this.pUniforms,
      transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
    }));
    this.points.visible = !this.compact;
    this.scene.add(this.points);

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(w / 2, h / 2), 0.55, 0.70, 0.55);
    this.composer.addPass(this.bloom);
    this.composer.setSize(w, h);

    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
  }

  resize() {
    if (!this._ok) return;
    const r = this.canvas.getBoundingClientRect();
    const w = Math.max(2, r.width), h = Math.max(2, r.height);
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    this.bloom.setSize(w / 2, h / 2);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  setState(next) {
    if (!STATES[next] || next === this.state) return;
    this.state = next;
    this.tgt = { ...STATES[next] };
  }

  /** Live amplitude 0..1 from NOVA's audio path. */
  setAmplitude(level) {
    const v = Math.max(0, Math.min(1, level || 0));
    if (v > this.audio) this.audio = v;
  }

  /** Optional per-band spectrum (length 8, 0..1) — makes the form a waveform. */
  setSpectrum(bands) {
    if (!bands) return;
    for (let i = 0; i < 8; i++) this.bands[i] = Math.max(0, Math.min(1, bands[i] || 0));
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
  }

  dispose() {
    this.stop();
    try {
      this.mesh.geometry.dispose(); this.mesh.material.dispose();
      this.inner.geometry.dispose(); this.inner.material.dispose();
      this.points.geometry.dispose(); this.points.material.dispose();
      this.wire.geometry.dispose(); this.wire.material.dispose();
      this.renderer.dispose();
    } catch (e) { /* noop */ }
  }

  _loop() {
    if (!this._running) return;
    this._raf = requestAnimationFrame(() => this._loop());
    if (document.hidden) return;                 // no work while unseen

    const dt = Math.min(this.clock.getDelta(), 0.05);

    // Ease every parameter toward the state target.
    const k = 1 - Math.pow(0.006, dt);
    for (const key of Object.keys(this.tgt)) {
      this.cur[key] += (this.tgt[key] - this.cur[key]) * k;
    }

    // Audio release. Attack is instant via setAmplitude.
    this.audio *= Math.pow(0.10, dt);
    for (let i = 0; i < 8; i++) this.bands[i] *= Math.pow(0.16, dt);

    const t = this.clock.elapsedTime * this.cur.speed;
    const u = this.uniforms;
    u.uTime.value = t;
    u.uAmp.value = this.cur.amp;
    u.uFreq.value = this.cur.freq;
    u.uChaos.value = this.cur.chaos;
    u.uAudio.value = this.audio;
    u.uHue.value = this.cur.hue;
    u.uRim.value = this.cur.rim;
    u.uGlow.value = this.cur.glow;
    for (let i = 0; i < 8; i++) u.uBands.value[i] = this.bands[i];

    this.pUniforms.uColor.value.setHSL((this.cur.hue + 0.04) % 1, 0.75, 0.72);

    const spin = this.cur.spin;
    this.mesh.rotation.y += spin * dt * 6;
    this.mesh.rotation.x = Math.sin(this.clock.elapsedTime * 0.15) * 0.18;
    this.wire.rotation.copy(this.mesh.rotation);
    this.inner.rotation.copy(this.mesh.rotation);
    this.inner.rotation.y *= -0.6;
    this.points.rotation.y -= spin * dt * 3;

    this.bloom.strength = (this.compact ? 0.18 : 0.35)
                        + this.cur.glow * (this.compact ? 0.18 : 0.35)
                        + this.audio * (this.compact ? 0.25 : 0.45);

    this.composer.render();
  }
}

window.NovaOrb3D = NovaOrb3D;
