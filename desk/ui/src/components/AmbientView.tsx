import React, { useEffect, useRef } from 'react';
import { postJSON } from '../nova/api';
import { useNova } from '../context/NovaStateContext';
import { useRuntime } from '../nova/runtime';
import { phaseColor } from './TopHud';

/** Movement beyond this is a drag that repositions the orb, not a click. */
const DRAG_PX = 5;

/**
 * The ambient window (`/?mode=ambient`): a 44 px round orb, always on top,
 * that NOVA shrinks to while she works on the desktop. nova_desktop_app.py
 * clips the window to a circle and makes it easy_drag, so the shell moves it
 * natively while the pointer is down; all this page has to do is not mistake
 * the end of a drag for a click. A click brings the full window back.
 *
 * It hears NOVA through the event bus only (see NovaEventHub), and does not
 * touch screen watching: the desktop app turns that on and off with ambient
 * mode, as the one authority for it.
 */
export const AmbientView: React.FC = () => {
  const { theme } = useNova();
  const rt = useRuntime();
  const core = useRef<HTMLDivElement>(null);
  const rim = useRef<HTMLDivElement>(null);
  const press = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const color = phaseColor(rt.phase, theme.palette.accent);

  // Breathe with the voices, straight from the visual engine each frame --
  // no React re-render per frame.
  useEffect(() => {
    let raf = 0;
    const loop = () => {
      const f = rt.frame.current;
      const nova = f?.novaAudio ?? 0;
      const user = f?.userAudio ?? 0;
      const energy = f?.params.energy ?? 0.25;
      if (core.current) core.current.style.transform = `scale(${(0.72 + energy * 0.2 + nova * 0.25).toFixed(3)})`;
      if (rim.current) rim.current.style.opacity = String(Math.min(1, 0.25 + user * 1.5 + nova));
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [rt.frame]);

  return (
    <div
      className="w-screen h-screen rounded-full flex items-center justify-center cursor-pointer select-none overflow-hidden"
      style={{ backgroundColor: theme.palette.bgBase }}
      title={`NOVA — ${rt.phaseLabel}. Click to open, drag to move.`}
      role="button"
      aria-label={`NOVA: ${rt.phaseLabel}. Click to open.`}
      onPointerDown={(e) => {
        press.current = { x: e.screenX, y: e.screenY, moved: false };
      }}
      onPointerMove={(e) => {
        const p = press.current;
        if (p && (Math.abs(e.screenX - p.x) > DRAG_PX || Math.abs(e.screenY - p.y) > DRAG_PX)) p.moved = true;
      }}
      onPointerUp={() => {
        const p = press.current;
        press.current = null;
        if (!p || p.moved) return; // repositioned, not a click
        postJSON('/api/ambient', { mode: 'full' }).catch(() => undefined);
      }}
    >
      <div ref={rim} className="absolute inset-[2px] rounded-full border-2 transition-colors duration-300" style={{ borderColor: color }} />
      <div
        ref={core}
        className="w-3/5 h-3/5 rounded-full transition-colors duration-300"
        style={{ background: `radial-gradient(circle at 35% 35%, ${theme.palette.textPrimary}, ${color} 55%, ${theme.palette.bgBase} 100%)`, boxShadow: `0 0 14px ${color}` }}
      />
    </div>
  );
};
