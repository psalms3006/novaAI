import React, { useEffect, useState } from 'react';
import { useNova } from '../context/NovaStateContext';
import { fmtPct, useRuntime, type Phase } from '../nova/runtime';

/** The colour each phase shows in, so the state reads at a glance. */
export function phaseColor(phase: Phase, accent: string): string {
  switch (phase) {
    case 'offline':
    case 'error':
      return '#ef4444';
    case 'no-network':
    case 'recovering':
    case 'connecting':
    case 'awaiting-permission':
    case 'muted':
      return '#f59e0b';
    case 'listening':
    case 'speaking':
    case 'executing':
    case 'looking':
    case 'researching':
      return '#10b981';
    default:
      return accent;
  }
}

const ACTIVE: Phase[] = ['listening', 'speaking', 'thinking', 'executing', 'looking', 'researching', 'connecting', 'recovering'];

export const TopHud: React.FC = () => {
  const { currentScreen, theme, setThemeModalOpen, prefs } = useNova();
  const rt = useRuntime();
  const [timeString, setTimeString] = useState('');
  const [showStatus, setShowStatus] = useState(false);

  useEffect(() => {
    const update = () => setTimeString(new Date().toLocaleTimeString([], { hour12: prefs.time_format === '12h' }));
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [prefs.time_format]);

  const linkOk = rt.backendReachable && rt.busAttached;
  const linkLabel = !rt.backendReachable ? 'NOVA unreachable' : !rt.busAttached ? 'Connecting' : rt.status?.online === false ? 'Offline mode' : 'Connected';
  const linkColor = !rt.backendReachable ? '#ef4444' : !linkOk || rt.status?.online === false ? '#f59e0b' : '#10b981';
  const color = phaseColor(rt.phase, theme.palette.accent);
  const v = rt.system?.vitals;

  return (
    <header
      className="h-11 px-5 flex items-center justify-between z-30 shrink-0 font-sans text-xs border-b backdrop-blur-2xl transition-colors duration-300 relative select-none"
      style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary }}
    >
      <div className="flex items-center space-x-3">
        <span className="font-semibold tracking-[0.2em] uppercase text-[11px] flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full shadow-sm" style={{ backgroundColor: theme.palette.accent }} />
          NOVA
        </span>

        <span className="text-xs opacity-20">/</span>

        <div className="relative">
          <button
            onClick={() => setShowStatus(!showStatus)}
            className="text-[11px] flex items-center gap-1.5 font-normal px-2 py-1 rounded-lg border transition-all hover:brightness-110 cursor-pointer"
            style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}
            aria-expanded={showStatus}
          >
            <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: linkColor }} />
            <span className="font-sans">{linkLabel}</span>
            <i className="fa-solid fa-chevron-down text-[8px] opacity-40 ml-0.5" />
          </button>

          {showStatus && (
            <div
              className="absolute left-0 top-9 w-68 p-4 rounded-2xl border shadow-2xl backdrop-blur-3xl z-40 text-xs space-y-2.5 animate-fade-in"
              style={{
                backgroundColor: theme.palette.bgElevated,
                borderColor: theme.palette.glassBorder,
                boxShadow: `0 20px 45px -10px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
              }}
            >
              <div className="flex items-center justify-between font-mono text-[10px] pb-1 border-b" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textMuted }}>
                <span>CORE TELEMETRY</span>
                <span className="font-medium tracking-wider" style={{ color: linkColor }}>
                  {rt.backendReachable ? 'CONNECTED' : 'OFFLINE'}
                </span>
              </div>
              <div className="space-y-1.5 text-[11px]">
                {[
                  ['Network Status', rt.status ? (rt.status.online ? 'Online (REST + Live)' : 'Local Intelligence') : 'Connecting…'],
                  ['Active Model', rt.status ? `${rt.status.serving === 'cloud' ? 'Cloud' : 'Local'} · ${rt.status.model}` : '—'],
                  ['Voice Mode', rt.voiceRunning ? (rt.muted ? 'Running (Muted)' : 'Active Full-Duplex') : 'Standby / Inactive'],
                  ['System Vitals', `CPU ${fmtPct(v?.cpu_pct)} · RAM ${fmtPct(v?.mem_pct)}`],
                  ['Latency', rt.system?.intelligence?.last_turn_ms ? `${rt.system.intelligence.last_turn_ms} ms` : 'Standby'],
                ].map(([k, val]) => (
                  <div key={k} className="flex justify-between gap-3 items-center" style={{ color: theme.palette.textSecondary }}>
                    <span className="text-[10px] font-sans opacity-70">{k}</span>
                    <span className="font-mono text-[10px] font-medium truncate text-right" style={{ color: theme.palette.textPrimary }}>
                      {val}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <span className="hidden md:inline text-[11px] opacity-40 font-mono tracking-wider text-[10px] uppercase" style={{ color: theme.palette.textMuted }}>
          {currentScreen === 'substrate' && 'Presence'}
          {currentScreen === 'runtime' && 'Settings'}
          {currentScreen === 'synaptic' && 'Synaptic Map'}
          {currentScreen === 'library' && 'Library'}
        </span>
      </div>

      <div className="flex items-center space-x-2 px-3 py-1 rounded-full border text-[11px] font-sans shadow-sm" style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder }} role="status" aria-live="polite">
        <span className={`w-1.5 h-1.5 rounded-full transition-all ${ACTIVE.includes(rt.phase) ? 'animate-pulse' : 'opacity-80'}`} style={{ backgroundColor: color }} />
        <span style={{ color: rt.phase === 'idle' ? theme.palette.textSecondary : theme.palette.textPrimary }}>{rt.phaseLabel}</span>
      </div>

      <div className="flex items-center space-x-3 text-xs">
        <button
          onClick={() => setThemeModalOpen(true)}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded-xl border text-[11px] transition-all hover:brightness-110 cursor-pointer"
          style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}
          title="Change theme"
        >
          <span className="w-2 h-2 rounded-full border" style={{ backgroundColor: theme.palette.accent, borderColor: theme.palette.glassBorder }} />
          <span className="font-sans font-medium">{theme.name}</span>
          <i className="fa-solid fa-palette text-[9px] opacity-60 ml-0.5" />
        </button>

        <div className="h-3 w-px" style={{ backgroundColor: theme.palette.glassBorder }} />

        <span className="font-mono text-[11px] tabular-nums min-w-[70px] text-right" style={{ color: theme.palette.textSecondary }}>
          {timeString}
        </span>
      </div>
    </header>
  );
};
