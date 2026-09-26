import React, { useEffect, useState } from 'react';
import { postJSON } from '../nova/api';
import { useNova } from '../context/NovaStateContext';
import { useRuntime } from '../nova/runtime';
import { phaseColor } from './TopHud';

/**
 * The ambient window (`/?mode=ambient`): a 44 px always-on-top strip NOVA
 * shrinks to while she works on the desktop. It listens on the event bus only
 * (see NovaEventHub), asks her to watch the screen, and a click brings the
 * full window back.
 */
export const AmbientView: React.FC = () => {
  const { theme } = useNova();
  const rt = useRuntime();
  const [watching, setWatching] = useState(false);

  // Watching the screen is the point of ambient mode. The voice session may
  // still be connecting when the strip appears, so retry briefly.
  useEffect(() => {
    let cancelled = false;
    let tries = 0;
    const attempt = async () => {
      if (cancelled || tries++ >= 6) return;
      try {
        const j = await postJSON<{ ok: boolean; watching: boolean }>('/api/live/screen', { watching: true });
        if (!cancelled && j.ok && j.watching) {
          setWatching(true);
          return;
        }
      } catch {
        /* retry below */
      }
      if (!cancelled) setTimeout(attempt, 1500);
    };
    attempt();
    return () => {
      cancelled = true;
    };
  }, []);

  const expand = () => {
    postJSON('/api/ambient', { mode: 'full' }).catch(() => undefined);
  };

  const color = phaseColor(rt.phase, theme.palette.accent);
  const last = rt.turns[rt.turns.length - 1];
  const recent = rt.activity[0];
  const line = last?.live ? last.text : recent && Date.now() - recent.at < 8000 ? recent.text : rt.phaseLabel;

  return (
    <button
      onClick={expand}
      className="w-screen h-screen flex items-center gap-3 px-4 border cursor-pointer select-none backdrop-blur-2xl"
      style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary }}
      title="Open NOVA"
      aria-label={`NOVA: ${rt.phaseLabel}. Click to open.`}
    >
      <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${rt.phase === 'idle' ? '' : 'animate-pulse'}`} style={{ backgroundColor: color, boxShadow: `0 0 12px ${color}` }} />
      <span className="text-xs font-semibold tracking-[0.18em] shrink-0">NOVA</span>
      <span className="text-xs truncate flex-1 text-left" style={{ color: theme.palette.textSecondary }}>
        {line}
      </span>
      {watching && (
        <span className="text-[10px] font-mono shrink-0 flex items-center gap-1" style={{ color: theme.palette.textMuted }}>
          <i className="fa-solid fa-eye text-[9px]" /> watching
        </span>
      )}
    </button>
  );
};
