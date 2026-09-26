import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNova } from '../context/NovaStateContext';
import { useRuntime, fmtAgo, type ActivityKind } from '../nova/runtime';
import { SpatialCanvas } from '../components/three/SpatialCanvas';
import { phaseColor } from '../components/TopHud';

const AGENT_ICON: Record<string, string> = {
  orchestrator: 'fa-diagram-project',
  research: 'fa-compass',
  code: 'fa-code',
  vision: 'fa-eye',
  browser: 'fa-window-maximize',
  memory: 'fa-brain',
  creative: 'fa-wand-magic-sparkles',
  meeting: 'fa-users',
  surveillance: 'fa-shield-halved',
  spawn: 'fa-layer-group',
};

const FILTERS: { id: string; label: string; kinds: ActivityKind[] | null }[] = [
  { id: 'all', label: 'all', kinds: null },
  { id: 'tools', label: 'tools', kinds: ['tool', 'result', 'agent', 'vision'] },
  { id: 'tasks', label: 'tasks', kinds: ['task'] },
  { id: 'voice', label: 'voice', kinds: ['voice'] },
  { id: 'errors', label: 'errors', kinds: ['error'] },
];

const TASK_DOT: Record<string, string> = {
  RUNNING: '#f59e0b',
  VERIFYING: '#f59e0b',
  QUEUED: '#94a3b8',
  PLANNED: '#94a3b8',
  WAITING: '#94a3b8',
  COMPLETED: '#10b981',
  FAILED: '#ef4444',
  CANCELLED: '#64748b',
  PARTIALLY_COMPLETED: '#f59e0b',
  UNVERIFIED: '#f59e0b',
};

export const PresenceScreen: React.FC = () => {
  const { presenceType, setPresenceType, theme, prefs, setCurrentScreen, quality } = useNova();
  const rt = useRuntime();
  const [openAgent, setOpenAgent] = useState<string | null>(null);
  const [filter, setFilter] = useState('all');
  const [asking, setAsking] = useState(false);
  const [ask, setAsk] = useState('');
  const transcriptEnd = useRef<HTMLDivElement>(null);

  const panel = {
    backgroundColor: theme.palette.glassSurface,
    borderColor: theme.palette.glassBorder,
    boxShadow: `0 15px 35px -10px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
  };

  const tools = rt.status?.tools || {};
  const toolCount = Object.keys(tools).length;
  const toolsOn = Object.values(tools).filter(Boolean).length;
  const agents = rt.system?.agents || [];
  const tasks = rt.system?.tasks || [];
  const kinds = FILTERS.find((f) => f.id === filter)?.kinds;
  const activity = useMemo(() => (kinds ? rt.activity.filter((a) => kinds.includes(a.kind)) : rt.activity), [rt.activity, kinds]);
  const color = phaseColor(rt.phase, theme.palette.accent);
  const turns = rt.turns.slice(-6);

  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ block: 'end', behavior: prefs.ambient_motion ? 'smooth' : 'auto' });
  }, [rt.turns, prefs.ambient_motion]);

  const submitAsk = (e: React.FormEvent) => {
    e.preventDefault();
    const t = ask.trim();
    if (!t) return;
    rt.sendText(t);
    setAsk('');
    setAsking(false);
  };

  return (
    <div className="relative w-full h-full flex items-center justify-center overflow-hidden select-none">
      {/* ================= AMBIENT SATELLITES (decorative) ================= */}
      {[
        { pos: 'left-[26%] top-[14%]', spin: 'animate-[spin_60s_linear_infinite]' },
        { pos: 'right-[27%] top-[12%]', spin: 'animate-[spin_50s_linear_infinite_reverse]' },
      ].map((s) => (
        <div key={s.pos} className={`absolute ${s.pos} w-32 h-32 pointer-events-none opacity-40 z-0`} aria-hidden>
          <div className="relative w-full h-full flex items-center justify-center">
            <div className="absolute inset-0 rounded-full blur-md" style={{ backgroundColor: `${theme.palette.accent}08` }} />
            <div className={`absolute inset-2 rounded-full border border-dashed ${s.spin}`} style={{ borderColor: `${theme.palette.accent}20` }} />
            <div className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: theme.palette.accent }} />
          </div>
        </div>
      ))}

      {/* ================= LEFT: NOVA STATUS & AGENTS ================= */}
      <div className="absolute left-20 top-6 bottom-8 flex flex-col space-y-3 w-64 pointer-events-auto z-20">
        <div className="p-4 rounded-2xl border shadow-xl backdrop-blur-2xl transition-all duration-300" style={panel}>
          <div className="flex items-center justify-between text-xs mb-2.5">
            <span className="font-semibold uppercase tracking-wider text-[10px]" style={{ color: theme.palette.accent }}>
              NOVA
            </span>
            <span className="text-[10px] font-mono opacity-60" style={{ color: theme.palette.textSecondary }}>
              {rt.status ? `${rt.status.serving} · ${rt.status.model}` : '—'}
            </span>
          </div>
          <div className="space-y-1.5 text-[11px] font-sans">
            <div className="flex items-center justify-between" style={{ color: theme.palette.textSecondary }}>
              <span className="flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: theme.palette.accent }} />
                Tools available
              </span>
              <span className="font-semibold font-mono" style={{ color: theme.palette.textPrimary }}>
                {rt.status ? `${toolsOn}/${toolCount}` : '—'}
              </span>
            </div>
            <div className="flex items-center justify-between" style={{ color: theme.palette.textSecondary }}>
              <span className="flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: rt.status?.memory.enabled ? '#10b981' : theme.palette.textMuted }} />
                Memory
              </span>
              <span className="font-medium font-mono" style={{ color: theme.palette.textPrimary }}>
                {rt.status ? (rt.status.memory.enabled ? `${rt.status.memory.facts} facts` : 'off') : '—'}
              </span>
            </div>
          </div>
          <div className="mt-3 pt-2.5 border-t flex items-center justify-between text-[10px] font-sans" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textMuted }}>
            <span>Last reply</span>
            <span style={{ color: theme.palette.textSecondary }}>
              {rt.system?.intelligence?.last_turn_ms ? `${rt.system.intelligence.last_turn_ms} ms` : 'no turns yet'}
            </span>
          </div>
        </div>

        <div className="p-3.5 rounded-2xl border shadow-xl backdrop-blur-2xl flex-1 flex flex-col transition-all duration-300 overflow-hidden" style={panel}>
          <div className="flex items-center justify-between pb-2 border-b mb-2 text-xs" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}>
            <span className="uppercase tracking-wider text-[9px] font-semibold flex items-center gap-1.5" style={{ color: theme.palette.textPrimary }}>
              <i className="fa-solid fa-microchip text-[10px] opacity-70" /> Agents
            </span>
            <span className="text-[10px] opacity-60 font-mono">{agents.filter((a) => a.state === 'active').length} active</span>
          </div>

          <div className="space-y-1.5 overflow-y-auto pr-0.5 flex-1">
            {agents.length === 0 && (
              <div className="text-[11px] p-2" style={{ color: theme.palette.textMuted }}>
                {rt.backendReachable ? 'Waiting for the agent roster…' : 'NOVA is not reachable.'}
              </div>
            )}
            {agents.map((a) => {
              const open = openAgent === a.id;
              const active = a.state === 'active';
              return (
                <button
                  key={a.id}
                  onClick={() => setOpenAgent(open ? null : a.id)}
                  className="w-full text-left p-2.5 rounded-xl transition-all cursor-pointer border group"
                  style={{ backgroundColor: open ? theme.palette.bgElevated : 'transparent', borderColor: open ? theme.palette.accentBorder : 'transparent' }}
                  aria-expanded={open}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium flex items-center gap-1.5 truncate" style={{ color: theme.palette.textPrimary }}>
                      <i className={`fa-solid ${AGENT_ICON[a.id] || 'fa-circle'} text-[10px] opacity-60 group-hover:opacity-100 transition-opacity`} />
                      {a.label}
                    </span>
                    <span className={`w-1.5 h-1.5 rounded-full ${active ? 'animate-pulse' : ''}`} style={{ backgroundColor: active ? '#10b981' : theme.palette.textMuted }} />
                  </div>
                  <div className="flex items-center justify-between mt-1 text-[10px] font-sans" style={{ color: theme.palette.textMuted }}>
                    <span className="truncate">{active ? a.action || 'working' : 'standby'}</span>
                  </div>
                  {open && (
                    <div className="mt-2 pt-1.5 border-t text-[10px] leading-relaxed animate-fade-in font-sans" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}>
                      {a.role}
                    </div>
                  )}
                </button>
              );
            })}
          </div>

          <div className="mt-2 pt-2 border-t flex items-center justify-between text-[10px] font-sans" style={{ borderColor: theme.palette.glassBorder }}>
            <span style={{ color: theme.palette.textMuted }}>Live from NOVA</span>
            <button
              onClick={() => setCurrentScreen('runtime')}
              className="transition-colors flex items-center gap-1 cursor-pointer font-medium hover:opacity-100"
              style={{ color: theme.palette.accent }}
            >
              Tools & permissions <i className="fa-solid fa-chevron-right text-[8px]" />
            </button>
          </div>
        </div>
      </div>

      {/* ================= CENTRE: PRESENCE + CONVERSATION ================= */}
      <div className="relative flex flex-col items-center justify-center z-10 select-none -mt-10">
        <div className="relative w-72 h-72 md:w-[380px] md:h-[380px] flex items-center justify-center entity-float bg-transparent">
          <div className="absolute inset-4 rounded-full blur-3xl ambient-glow pointer-events-none transition-all duration-700" style={{ backgroundColor: `${theme.palette.accent}0a` }} />
          <div className="absolute inset-0 z-10 pointer-events-none">
            <SpatialCanvas presenceType={presenceType} theme={theme} frame={rt.frame} quality={quality} still={!prefs.ambient_motion} />
          </div>
          <div className="absolute inset-8 rounded-full border pointer-events-none transition-colors duration-500 opacity-20" style={{ borderColor: theme.palette.orbWireframe }} />
        </div>

        <div className="flex items-center space-x-2.5 -mt-2 pointer-events-auto z-20">
          <button
            onClick={() => setPresenceType(presenceType === 'orb' ? 'humanoid' : 'orb')}
            className="w-6 h-6 rounded-full border flex items-center justify-center text-[10px] transition-all hover:scale-105 cursor-pointer"
            style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}
            title={presenceType === 'orb' ? 'Show her figure' : 'Show the orb'}
            aria-label="Switch presence"
          >
            <i className="fa-solid fa-chevron-left" />
          </button>
          <div
            className="flex items-center space-x-2 px-3.5 py-1.5 rounded-full border backdrop-blur-2xl text-[10px] font-sans shadow-md"
            style={{ ...panel, color: theme.palette.textPrimary }}
            role="status"
          >
            <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />
            <span className="tracking-wide">{rt.visualLabel}</span>
          </div>
          <button
            onClick={() => setPresenceType(presenceType === 'orb' ? 'humanoid' : 'orb')}
            className="w-6 h-6 rounded-full border flex items-center justify-center text-[10px] transition-all hover:scale-105 cursor-pointer"
            style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}
            title={presenceType === 'orb' ? 'Show her figure' : 'Show the orb'}
            aria-label="Switch presence"
          >
            <i className="fa-solid fa-chevron-right" />
          </button>
        </div>

        {prefs.show_transcript && (
          <div className="mt-4 w-[min(520px,42vw)] max-h-[22vh] overflow-y-auto pointer-events-auto space-y-1.5 px-1 select-text" aria-live="polite" aria-label="Conversation">
            {turns.length === 0 ? (
              <div className="text-center text-[11px] opacity-60" style={{ color: theme.palette.textMuted }}>
                {rt.voiceRunning ? 'Say something — your words and hers appear here.' : 'Start voice or type below to talk to NOVA.'}
              </div>
            ) : (
              turns.map((t) => (
                <div key={t.id} className={`flex ${t.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <div
                    className="max-w-[85%] px-3 py-1.5 rounded-2xl border text-xs leading-relaxed backdrop-blur-2xl"
                    style={{
                      backgroundColor: t.role === 'user' ? theme.palette.bgElevated : theme.palette.glassSurface,
                      borderColor: t.role === 'nova' ? theme.palette.accentBorder : theme.palette.glassBorder,
                      color: t.role === 'user' ? theme.palette.textSecondary : theme.palette.textPrimary,
                      opacity: t.live ? 0.85 : 1,
                    }}
                  >
                    {t.text}
                    {t.live && <span className="inline-block w-1 h-3 ml-1 align-middle animate-pulse" style={{ backgroundColor: theme.palette.accent }} />}
                  </div>
                </div>
              ))
            )}
            <div ref={transcriptEnd} />
          </div>
        )}
      </div>

      {/* ================= RIGHT: TASKS & ACTIVITY ================= */}
      <div className="absolute right-6 top-6 bottom-8 flex flex-col justify-between w-80 max-w-[340px] pointer-events-auto z-20 space-y-3">
        <div className="p-4 rounded-2xl border shadow-xl backdrop-blur-2xl transition-all duration-300" style={panel}>
          <div className="flex items-center justify-between pb-2 border-b mb-2.5" style={{ borderColor: theme.palette.glassBorder }}>
            <span className="uppercase tracking-wider text-[9px] font-semibold flex items-center gap-1.5" style={{ color: theme.palette.accent }}>
              <i className="fa-solid fa-bolt text-[10px]" /> Tasks
            </span>
            <div className="flex items-center gap-2">
              <span className="text-[10px] opacity-60 font-mono" style={{ color: theme.palette.textSecondary }}>
                {rt.taskBusy ? 'working' : tasks.length ? `${tasks.length} recent` : 'idle'}
              </span>
              <button
                onClick={() => setAsking(true)}
                className="w-4 h-4 rounded-md border flex items-center justify-center text-[9px] hover:opacity-100 cursor-pointer"
                style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}
                title="Give NOVA a task"
                aria-label="Give NOVA a task"
              >
                <i className="fa-solid fa-plus" />
              </button>
            </div>
          </div>

          {asking && (
            <form onSubmit={submitAsk} className="mb-2.5 p-2.5 rounded-xl border space-y-2 animate-fade-in" style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder }}>
              <input
                type="text"
                value={ask}
                onChange={(e) => setAsk(e.target.value)}
                placeholder="What should NOVA do?"
                autoFocus
                maxLength={2000}
                aria-label="Task for NOVA"
                className="w-full text-xs bg-transparent outline-none border-b pb-1 font-sans placeholder:opacity-40 select-text"
                style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary }}
              />
              <div className="flex items-center justify-between pt-1">
                <span className="text-[10px] opacity-60" style={{ color: theme.palette.textMuted }}>
                  She plans it and asks before anything risky.
                </span>
                <div className="flex gap-1.5">
                  <button type="button" onClick={() => setAsking(false)} className="px-2 py-0.5 rounded text-[10px] opacity-60 hover:opacity-100 cursor-pointer" style={{ color: theme.palette.textSecondary }}>
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="px-2.5 py-0.5 rounded text-[10px] font-medium border cursor-pointer"
                    style={{ backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#fff', borderColor: theme.palette.accentBorder }}
                  >
                    Ask
                  </button>
                </div>
              </div>
            </form>
          )}

          <div className="space-y-2 max-h-[180px] overflow-y-auto">
            {tasks.length === 0 ? (
              <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
                No tasks. Ask NOVA to do something that takes several steps and it will appear here.
              </div>
            ) : (
              tasks.map((t) => {
                const s = String(t.status).toUpperCase();
                const running = s === 'RUNNING' || s === 'VERIFYING';
                return (
                  <div key={t.id} className="text-xs p-1 rounded-lg" style={{ color: theme.palette.textSecondary }}>
                    <div className="flex items-center space-x-2">
                      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${running ? 'animate-pulse' : ''}`} style={{ backgroundColor: TASK_DOT[s] || theme.palette.textMuted }} />
                      <span className="truncate flex-1" style={{ color: theme.palette.textPrimary }} title={t.title}>
                        {t.title}
                      </span>
                      <span className="text-[9px] font-mono opacity-60 shrink-0">{s.toLowerCase().replace('_', ' ')}</span>
                    </div>
                    {running && t.progress != null && (
                      <div className="mt-1 ml-3.5 h-0.5 rounded-full overflow-hidden" style={{ backgroundColor: theme.palette.glassBorder }}>
                        <div className="h-full transition-all" style={{ width: `${Math.round(t.progress * 100)}%`, backgroundColor: theme.palette.accent }} />
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>

        <div className="p-4 rounded-2xl border shadow-xl backdrop-blur-2xl flex-1 max-h-[330px] min-h-0 flex flex-col transition-all duration-300" style={panel}>
          <div className="flex items-center justify-between pb-2 border-b text-xs mb-2" style={{ borderColor: theme.palette.glassBorder }}>
            <div className="flex items-center space-x-2 text-[10px] uppercase tracking-wider">
              <span className="font-semibold" style={{ color: theme.palette.textPrimary }}>
                Activity
              </span>
            </div>
            <span className="text-[10px] font-mono flex items-center gap-1" style={{ color: rt.liveAttached ? '#10b981' : '#f59e0b' }}>
              <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: rt.liveAttached ? '#10b981' : '#f59e0b' }} />
              {rt.liveAttached ? 'live' : 'reconnecting'}
            </span>
          </div>

          <div className="flex items-center gap-1 mb-2 font-mono text-[9px]">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                onClick={() => setFilter(f.id)}
                className={`px-2 py-0.5 rounded-md capitalize transition-colors cursor-pointer ${filter === f.id ? 'font-semibold border' : 'opacity-50 hover:opacity-100'}`}
                style={{
                  backgroundColor: filter === f.id ? theme.palette.bgElevated : 'transparent',
                  borderColor: filter === f.id ? theme.palette.glassBorder : 'transparent',
                  color: filter === f.id ? theme.palette.textPrimary : theme.palette.textSecondary,
                }}
                aria-pressed={filter === f.id}
              >
                {f.label}
              </button>
            ))}
          </div>

          <div className="my-1 space-y-2 overflow-y-auto pr-1 text-xs flex-1 min-h-0">
            {activity.length === 0 ? (
              <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
                Nothing yet. Tools, tasks and voice events show here as they happen.
              </div>
            ) : (
              activity.slice(0, 40).map((a) => (
                <div key={a.id} className="p-2 rounded-xl border transition-colors" style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder }}>
                  <div className="flex items-center justify-between text-[10px] mb-0.5 font-mono" style={{ color: theme.palette.textMuted }}>
                    <span className="font-medium" style={{ color: a.kind === 'error' ? '#ef4444' : theme.palette.accent }}>
                      {a.kind}
                    </span>
                    <span>{fmtAgo(a.at)}</span>
                  </div>
                  <p className="text-xs leading-relaxed font-sans select-text" style={{ color: theme.palette.textSecondary }}>
                    {a.text}
                  </p>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
