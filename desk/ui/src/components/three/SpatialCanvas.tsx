// NOVA's presence: a particle orb, or a wireframe figure. It draws what the
// visual engine says is happening (nova/visual.ts, fed by real backend
// events) -- her voice level, the user's voice, thinking, working, errors --
// never a simulation.
//
// Performance: particle count, pixel ratio, antialiasing and frame cap come
// from the quality preset; with ambient motion off the canvas drops to a few
// frames a second whenever nothing is happening. Everything created here is
// disposed on unmount or when the quality/theme rebuild it.

import React, { useEffect, useRef } from 'react';
import * as THREE from 'three';
import type { PresenceType, ThemeDefinition } from '../../types/nova';
import type { VisualFrame } from '../../nova/visual';

interface QualitySpec {
  particles: number;
  pixelRatio: number;
  antialias: boolean;
  fps: number;
}

interface SpatialCanvasProps {
  presenceType: PresenceType;
  theme: ThemeDefinition;
  frame: React.MutableRefObject<VisualFrame | null>;
  quality: QualitySpec;
  /** Ambient motion off: hold still (and draw rarely) when nothing is happening. */
  still?: boolean;
}

const ERROR_COLOR = new THREE.Color('#ef4444');

function disposeTree(root: THREE.Object3D): void {
  root.traverse((obj) => {
    const o = obj as THREE.Mesh;
    o.geometry?.dispose();
    const mat = o.material as THREE.Material | THREE.Material[] | undefined;
    if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
    else mat?.dispose();
  });
}

export const SpatialCanvas: React.FC<SpatialCanvasProps> = ({ presenceType, theme, frame, quality, still = false }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const presenceRef = useRef(presenceType);
  const stillRef = useRef(still);
  presenceRef.current = presenceType;
  stillRef.current = still;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const width = container.clientWidth || 400;
    const height = container.clientHeight || 400;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
    camera.position.z = 120;

    const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: quality.antialias, powerPreference: 'high-performance' });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, quality.pixelRatio));
    container.appendChild(renderer.domElement);

    const palette = theme.palette;
    const primary = new THREE.Color(palette.orbParticlePrimary);
    const secondary = new THREE.Color(palette.orbParticleSecondary);
    const accent = new THREE.Color(palette.accent);
    const tint = new THREE.Color();

    const master = new THREE.Group();
    scene.add(master);

    // --- the orb ---
    const orbGroup = new THREE.Group();
    master.add(orbGroup);

    const count = quality.particles;
    const radius = 35;
    const positions = new Float32Array(count * 3);
    const originals = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    const mixed = new THREE.Color();
    for (let i = 0; i < count; i++) {
      // Fibonacci spiral: even density without clumping.
      const phi = Math.acos(1 - (2 * (i + 0.5)) / count);
      const th = Math.PI * (1 + Math.sqrt(5)) * (i + 0.5);
      const r = radius + (Math.random() - 0.5) * 3.5;
      const x = r * Math.sin(phi) * Math.cos(th);
      const y = r * Math.sin(phi) * Math.sin(th);
      const z = r * Math.cos(phi);
      positions.set([x, y, z], i * 3);
      originals.set([x, y, z], i * 3);
      mixed.copy(primary).lerp(secondary, Math.random() * 0.5);
      colors.set([mixed.r, mixed.g, mixed.b], i * 3);
    }
    const orbGeometry = new THREE.BufferGeometry();
    orbGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    orbGeometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

    const sprite = document.createElement('canvas');
    sprite.width = sprite.height = 64;
    const ctx = sprite.getContext('2d');
    if (ctx) {
      const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
      g.addColorStop(0, 'rgba(255,255,255,1)');
      g.addColorStop(0.25, 'rgba(255,255,255,0.7)');
      g.addColorStop(0.65, 'rgba(255,255,255,0.15)');
      g.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, 64, 64);
    }
    const particleTexture = new THREE.CanvasTexture(sprite);
    const baseOpacity = theme.isDark ? 0.85 : 0.65;
    const orbMaterial = new THREE.PointsMaterial({
      size: count > 8000 ? 1.5 : 1.9,
      vertexColors: true,
      map: particleTexture,
      transparent: true,
      blending: theme.isDark ? THREE.AdditiveBlending : THREE.NormalBlending,
      depthWrite: false,
      opacity: baseOpacity,
    });
    orbGroup.add(new THREE.Points(orbGeometry, orbMaterial));

    const ringMat = new THREE.MeshBasicMaterial({ color: primary, side: THREE.DoubleSide, transparent: true, opacity: 0.12 });
    const ring = new THREE.Mesh(new THREE.RingGeometry(43, 43.4, 96), ringMat);
    ring.rotation.x = Math.PI * 0.42;
    orbGroup.add(ring);

    // --- the figure ---
    const figure = new THREE.Group();
    master.add(figure);
    const contourMats: THREE.LineBasicMaterial[] = [];
    const contours = 26;
    for (let c = 0; c < contours; c++) {
      const t = (c / (contours - 1)) * 2 - 1;
      const ry = Math.sqrt(Math.max(0, 1 - t * t * 0.85)) * 27;
      const rx = ry * (0.75 + 0.08 * Math.sin(t * Math.PI));
      const pts: THREE.Vector3[] = [];
      for (let s = 0; s <= 44; s++) {
        const a = (s / 44) * Math.PI * 2;
        let fz = Math.cos(a) * rx;
        if (Math.cos(a) > 0) {
          if (t > -0.2 && t < 0.2) fz += Math.cos(a) * 6;
          else if (t < -0.4) fz -= Math.cos(a) * 2.5;
        }
        pts.push(new THREE.Vector3(Math.sin(a) * rx, t * 30, fz * 0.9));
      }
      const mat = new THREE.LineBasicMaterial({ color: primary, transparent: true, opacity: 0.14 + (1 - Math.abs(t)) * 0.28 });
      contourMats.push(mat);
      figure.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), mat));
    }
    const eyeMat = new THREE.MeshBasicMaterial({ color: primary, transparent: true, opacity: 0.7 });
    const eyeGeo = new THREE.SphereGeometry(1.0, 16, 16);
    for (const x of [-6, 6]) {
      const eye = new THREE.Mesh(eyeGeo, eyeMat);
      eye.position.set(x, 3.5, 14.5);
      figure.add(eye);
    }

    // --- pointer: gentle, inertial look-at ---
    const mouse = { x: 0, y: 0, tx: 0, ty: 0 };
    const onMove = (e: MouseEvent) => {
      const rect = container.getBoundingClientRect();
      mouse.tx = (((e.clientX - rect.left) / rect.width) * 2 - 1) * 0.45;
      mouse.ty = -(((e.clientY - rect.top) / rect.height) * 2 - 1) * 0.45;
    };
    window.addEventListener('mousemove', onMove);

    const resize = () => {
      const w = container.clientWidth || 400;
      const h = container.clientHeight || 400;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);

    // --- loop ---
    let raf = 0;
    let lastDraw = 0;
    let t = 0;
    let lastNow = performance.now();
    const minGap = 1000 / quality.fps;

    const animate = (now: number) => {
      raf = requestAnimationFrame(animate);
      const f = frame.current;
      const p = f?.params;
      const novaAudio = f?.novaAudio ?? 0;
      const userAudio = f?.userAudio ?? 0;
      const busy = novaAudio > 0.02 || userAudio > 0.02 || (f && f.state !== 'IDLE' && f.state !== 'SLEEPING');
      // Still mode: a quiet frame every half second while nothing happens.
      const gap = stillRef.current && !busy ? 500 : minGap;
      if (now - lastDraw < gap - 1) return;
      const dt = Math.min(0.1, (now - lastNow) / 1000);
      lastNow = now;
      lastDraw = now;
      const motion = stillRef.current && !busy ? 0 : 1;
      t += dt * (motion ? 1 : 0.2);

      mouse.x += (mouse.tx - mouse.x) * 0.04;
      mouse.y += (mouse.ty - mouse.y) * 0.04;

      const energy = p?.energy ?? 0.25;
      const think = p?.think ?? 0;
      const tool = p?.tool ?? 0;
      const error = p?.error ?? 0;
      const sleep = p?.sleep ?? 0;
      const offline = p?.offline ?? 0;
      const vision = p?.vision ?? 0;

      const isOrb = presenceRef.current === 'orb';
      orbGroup.visible = isOrb;
      figure.visible = !isOrb;

      // Colour: the theme's particles, warmed toward the accent while she
      // works, and toward red on an error.
      tint.copy(primary).lerp(accent, Math.min(1, think * 0.35 + tool * 0.5 + vision * 0.7)).lerp(ERROR_COLOR, error * 0.6);

      if (isOrb) {
        orbGroup.rotation.y = t * (0.06 + energy * 0.12 + think * 0.1) + mouse.x * 0.4;
        orbGroup.rotation.x = Math.sin(t * 0.08) * 0.08 + mouse.y * 0.3;
        // Reading the screen: the ring tilts upright and sweeps, like a scan.
        ring.rotation.z = t * (0.05 + tool * 0.3 + vision * 1.2);
        ring.rotation.x = Math.PI * (0.42 - vision * 0.3);
        const ringScale = 1 - (p?.listenRings ?? 0) * 0.06 + (p?.speakRings ?? 0) * 0.08 * (0.5 + novaAudio);
        ring.scale.setScalar(ringScale);
        ringMat.opacity = 0.1 + (p?.listenRings ?? 0) * 0.12 + (p?.speakRings ?? 0) * 0.1 + vision * 0.35;

        const pulse = 1 + novaAudio * 0.22 + userAudio * 0.1 + Math.sin(t * 1.8) * 0.015 * motion;
        const amp = (0.5 + energy * 1.2 + think * 0.8 + novaAudio * 2.5) * motion + 0.05;
        const phase = t * (2 + think * 2);
        const pos = orbGeometry.attributes.position as THREE.BufferAttribute;
        const arr = pos.array as Float32Array;
        for (let i = 0; i < count; i++) {
          const j = i * 3;
          const ox = originals[j];
          const oy = originals[j + 1];
          const oz = originals[j + 2];
          const d = Math.sqrt(ox * ox + oy * oy + oz * oz);
          const k = pulse + (Math.sin(d * 0.22 + phase + ox * 0.06) * amp) / radius;
          arr[j] = ox * k;
          arr[j + 1] = oy * k;
          arr[j + 2] = oz * k;
        }
        pos.needsUpdate = true;
        orbMaterial.color.lerp(tint, 0.08);
        orbMaterial.opacity = baseOpacity * (1 - sleep * 0.5) * (1 - offline * 0.25);
        ringMat.color.lerp(error > 0.1 ? ERROR_COLOR : secondary, 0.05);
      } else {
        figure.rotation.y = mouse.x * 0.7;
        figure.rotation.x = -mouse.y * 0.5;
        figure.rotation.z = Math.sin(t * 0.4) * 0.015 * motion;
        figure.scale.setScalar(1 + novaAudio * 0.08 + Math.sin(t * 2.0) * 0.01 * motion);
        eyeMat.color.lerp(tint, 0.08);
        eyeMat.opacity = 0.5 + novaAudio * 0.5;
        for (const m of contourMats) m.color.lerp(tint, 0.05);
      }

      renderer.render(scene, camera);
    };
    raf = requestAnimationFrame(animate);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
      window.removeEventListener('mousemove', onMove);
      disposeTree(scene);
      particleTexture.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    };
    // The scene is rebuilt only when what it is built from changes. Palette
    // colours are baked into vertex colours and blending, so a theme change
    // rebuilds rather than tinting muddily over the old colours.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quality.particles, quality.pixelRatio, quality.antialias, quality.fps, theme.id, theme.palette.accent]);

  return <div ref={containerRef} className="w-full h-full relative flex items-center justify-center pointer-events-none" style={{ overflow: 'hidden' }} />;
};
