import React, { useRef } from 'react';
import { postJSON } from '../nova/api';
import { useNova } from '../context/NovaStateContext';
import { useRuntime } from '../nova/runtime';
import { NovaOrb } from './NovaOrb';

/** Movement beyond this is a drag that repositions the orb, not a click. */
const DRAG_PX = 5;

/**
 * The ambient page (`/?mode=ambient`): the same thinking-orb as the main
 * window, on a transparent page. The desktop app no longer loads it -- it
 * draws its ambient orb natively (desk/ambient_native.py), because a WebView2
 * window cannot be see-through -- so this serves a plain browser. A click
 * brings the full window back; a drag is not a click.
 *
 * It hears NOVA through the event bus only (see NovaEventHub).
 */
export const AmbientView: React.FC = () => {
  const { theme } = useNova();
  const rt = useRuntime();
  const press = useRef<{ x: number; y: number; moved: boolean } | null>(null);

  return (
    <div
      className="w-screen h-screen flex items-center justify-center cursor-pointer select-none overflow-hidden"
      style={{ background: 'transparent' }}
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
      <NovaOrb theme={theme} frame={rt.frame} />
    </div>
  );
};
