// NOVA's presence: a thinking-orb (vendor/thinking-orbs, MIT, Jakub Antalik)
// whose animation is whatever she is actually doing. The visual engine
// (nova/visual.ts, fed by real backend events) names the state; each state
// has its own orb -- listening is a waveform rolling through the rings,
// searching a scan sweeping a dotted globe, speaking an undulating sash --
// and her voice level swells and quickens it.
//
// At this size the library's base ("fine") profiles are used: they were
// tuned for a 300 pt frame, where the 64 px presets read sparse. The
// presets' speed and extra tuning still apply. A change of state crossfades
// over ~350 ms. Monochrome ink, light on dark themes and dark on light ones,
// on a transparent canvas.

import React, { useEffect, useRef } from 'react';
import type { ThemeDefinition } from '../types/nova';
import type { VisualFrame, VisualState } from '../nova/visual';
import { MODE_FRAMES } from '../vendor/thinking-orbs/engine/registry';
import { paintFrame } from '../vendor/thinking-orbs/engine/core';
import { BASE_PROFILES } from '../vendor/thinking-orbs/engine/profiles';
import { PRESETS, STATE_TO_MODE, resolvePreset, type ModeKey } from '../vendor/thinking-orbs/presets';
import type { OrbState } from '../vendor/thinking-orbs/types';

/** What she is doing -> which orb. */
export const ORB_FOR: Record<VisualState, OrbState> = {
  // Not 'shaping': with voice off the engine stays AWAKENING ("Coming
  // online"), and an endless loading shape at rest read as stuck.
  AWAKENING: 'breathing',
  IDLE: 'breathing',
  LISTENING: 'listening',
  UNDERSTANDING: 'breathing',
  THINKING: 'solving',
  MEMORY_RETRIEVAL: 'connecting',
  TOOL_SELECTION: 'shaping',
  TOOL_EXECUTION: 'working',
  VISION: 'searching',
  SEARCHING: 'searching',
  GENERATING: 'weaving',
  SPEAKING: 'composing',
  SUCCESS: 'breathing',
  ERROR: 'solving',
  SLEEPING: 'breathing',
};

interface Tuned {
  mode: ModeKey;
  speed: number;
  opts: Record<string, number | undefined>;
}

const tuned = new Map<OrbState, Tuned>();
function large(state: OrbState): Tuned {
  const hit = tuned.get(state);
  if (hit) return hit;
  const mode = STATE_TO_MODE[state];
  const preset = PRESETS[mode][64];
  // The morph outline's dot radius is a fraction of the frame, not a
  // sub-linear pixel size, so its base profile draws blobs at this size; it
  // keeps the preset's own scaling.
  const opts = mode === 'morph' ? resolvePreset(state, 64).opts : { ...BASE_PROFILES[mode], ...(preset.extra ?? {}) };
  const t = { mode, speed: preset.speed, opts };
  tuned.set(state, t);
  return t;
}

interface NovaOrbProps {
  frame: React.MutableRefObject<VisualFrame | null>;
  theme: ThemeDefinition;
  /** Frame cap from the quality preset. */
  fps?: number;
  /** Ambient motion off: hold nearly still when nothing is happening. */
  still?: boolean;
}

export const NovaOrb: React.FC<NovaOrbProps> = ({ frame, theme, fps = 60, still = false }) => {
  const ref = useRef<HTMLCanvasElement>(null);
  const stillRef = useRef(still);
  stillRef.current = still;

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dark = theme.isDark;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    // Measured from the parent, and the canvas's CSS size set to match.
    // Measuring the canvas itself read its default 300x150 before layout,
    // drew at 150 px and let CSS stretch that to 380 -- dots became blobs
    // (seen in the real window, 2026-10-01).
    const host = canvas.parentElement ?? canvas;
    let size = 0;
    const fit = () => {
      const s = Math.max(16, Math.floor(Math.min(host.clientWidth, host.clientHeight)));
      if (s !== size) {
        size = s;
        canvas.width = Math.round(s * dpr);
        canvas.height = Math.round(s * dpr);
        canvas.style.width = `${s}px`;
        canvas.style.height = `${s}px`;
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(host);

    let raf = 0;
    let clock = 0;
    let last = performance.now();
    let lastDraw = 0;
    let current: OrbState | null = null;
    let previous: OrbState | null = null;
    let fade = 1;

    const draw = (state: OrbState, t: number, swell: number, alpha: number) => {
      const { mode, opts } = large(state);
      const f = MODE_FRAMES[mode](size, t, opts);
      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.translate(size / 2, size / 2);
      ctx.scale(swell, swell);
      ctx.translate(-size / 2, -size / 2);
      paintFrame(ctx, f, dark);
      ctx.restore();
    };

    const loop = (now: number) => {
      raf = requestAnimationFrame(loop);
      const vf = frame.current;
      const busy = !!vf && vf.state !== 'IDLE' && vf.state !== 'SLEEPING';
      const gap = stillRef.current && !busy ? 250 : 1000 / fps;
      if (now - lastDraw < gap - 1) return;
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      lastDraw = now;
      const state = ORB_FOR[vf?.state ?? 'IDLE'] ?? 'breathing';
      if (state !== current) {
        previous = current;
        current = state;
        fade = 0;
      }
      const voice = vf?.novaAudio ?? 0;
      const sleep = vf?.params.sleep ?? 0;
      const tempo = (stillRef.current && !busy ? 0.15 : 1) * (1 + voice * 0.6) * (1 - sleep * 0.6);
      clock += dt * large(state).speed * tempo;
      fade = Math.min(1, fade + dt / 0.35);
      const swell = 1 + voice * 0.08 + (vf?.userAudio ?? 0) * 0.03;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size, size);
      const dim = 1 - sleep * 0.45;
      if (previous && fade < 1) draw(previous, clock, swell, (1 - fade) * dim);
      draw(state, clock, swell, (previous ? fade : 1) * dim);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [frame, fps, theme.isDark, theme.id]);

  return (
    <canvas
      ref={ref}
      className="block m-auto pointer-events-none"
      role="img"
      aria-label="NOVA"
      data-testid="nova-orb"
    />
  );
};
