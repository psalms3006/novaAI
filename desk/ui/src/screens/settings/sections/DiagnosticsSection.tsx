import React, { useEffect, useState } from 'react';
import { getJSON } from '../../../nova/api';
import { fmtBps, fmtPct, useRuntime } from '../../../nova/runtime';
import { useNova } from '../../../context/NovaStateContext';
import { Button, Card, SectionHeader, Stat } from '../primitives';

type Diag = Record<string, unknown>;

/** Frames per second the window is actually drawing, measured over ~1 s. */
function useMeasuredFps(): number | null {
  const [fps, setFps] = useState<number | null>(null);
  useEffect(() => {
    let raf = 0;
    let frames = 0;
    let start = performance.now();
    const tick = (now: number) => {
      frames++;
      if (now - start >= 1000) {
        setFps(Math.round((frames * 1000) / (now - start)));
        frames = 0;
        start = now;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return fps;
}

function summarise(value: unknown): string {
  if (value == null) return '—';
  if (typeof value !== 'object') return String(value);
  const o = value as Record<string, unknown>;
  for (const k of ['state', 'status', 'available', 'ok', 'running', 'provider', 'backend']) {
    if (k in o && (typeof o[k] !== 'object' || o[k] === null)) return `${k}: ${String(o[k])}`;
  }
  return JSON.stringify(o).slice(0, 80);
}

export const DiagnosticsSection: React.FC = () => {
  const { theme, prefs, addToast } = useNova();
  const rt = useRuntime();
  const fps = useMeasuredFps();
  const [diag, setDiag] = useState<Diag | null>(null);
  const [running, setRunning] = useState(false);
  const v = rt.system?.vitals;

  const runSelfTest = async () => {
    setRunning(true);
    try {
      const [health, d] = await Promise.all([
        getJSON<{ ok: boolean; ready: boolean; brain_ready: boolean }>('/api/health', 8000),
        getJSON<Diag>('/api/local-intelligence/diagnostics', 20000),
      ]);
      setDiag({ backend: health, ...d });
      addToast('Self-test finished', health.ready ? 'Backend ready.' : 'Backend still starting.', health.ready ? 'success' : 'warning');
    } catch (e) {
      addToast('Self-test failed', (e as Error).message, 'warning');
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Diagnostics" subtitle="Live measurements from this computer and NOVA's backend. Nothing here is estimated." />

      <Card title="This computer" right={<span className="text-[10px] font-mono opacity-50" style={{ color: theme.palette.textMuted }}>updates every 2.5 s</span>}>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <Stat label="CPU" value={fmtPct(v?.cpu_pct)} tone={v?.cpu_pct != null && v.cpu_pct > 85 ? 'warn' : undefined} />
          <Stat label="Memory" value={v?.mem_used_gb != null ? `${v.mem_used_gb.toFixed(1)} / ${v.mem_total_gb?.toFixed(0)} GB` : fmtPct(v?.mem_pct)} />
          <Stat label="Download" value={fmtBps(v?.down_bps)} />
          <Stat label="Upload" value={fmtBps(v?.up_bps)} />
          <Stat label="NOVA uptime" value={rt.system ? `${Math.floor(rt.system.uptime_s / 60)} min` : '—'} />
          <Stat label="Last turn" value={rt.system?.intelligence?.last_turn_ms ? `${rt.system.intelligence.last_turn_ms} ms` : '—'} />
          <Stat label="Window FPS" value={fps ?? '—'} tone={fps != null && fps < 30 ? 'warn' : undefined} />
          <Stat label="Render quality" value={prefs.quality} />
        </div>
      </Card>

      <Card title="Self-test" right={<Button onClick={runSelfTest} disabled={running}>{running ? 'Testing…' : 'Run self-test'}</Button>}>
        {!diag ? (
          <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
            Checks the backend, network, model router, local runtime and speech components.
          </div>
        ) : (
          <div className="space-y-1.5">
            {Object.entries(diag).map(([k, val]) => (
              <div key={k} className="flex justify-between gap-3 text-[11px] font-mono">
                <span style={{ color: theme.palette.textSecondary }}>{k}</span>
                <span className="truncate text-right" style={{ color: theme.palette.textPrimary }}>
                  {summarise(val)}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="Event stream"
        right={
          <button className="text-[10px] font-mono opacity-60 hover:opacity-100 cursor-pointer" style={{ color: theme.palette.textMuted }} onClick={rt.clearActivity}>
            clear
          </button>
        }
      >
        <div className="max-h-72 overflow-y-auto space-y-1 font-mono text-[10px] select-text">
          {rt.activity.length === 0 ? (
            <div style={{ color: theme.palette.textMuted }}>Nothing yet. Events from the voice session and the agent appear here as they happen.</div>
          ) : (
            rt.activity.map((a) => (
              <div key={a.id} className="flex gap-2">
                <span className="opacity-50 shrink-0" style={{ color: theme.palette.textMuted }}>
                  {new Date(a.at).toLocaleTimeString([], { hour12: prefs.time_format === '12h' })}
                </span>
                <span className="shrink-0 w-12" style={{ color: a.kind === 'error' ? '#ef4444' : theme.palette.accent }}>
                  {a.kind}
                </span>
                <span style={{ color: theme.palette.textSecondary }}>{a.text}</span>
              </div>
            ))
          )}
        </div>
      </Card>
    </div>
  );
};
