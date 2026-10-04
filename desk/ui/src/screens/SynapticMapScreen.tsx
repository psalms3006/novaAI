import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { getJSON } from '../nova/api';
import { useRuntime } from '../nova/runtime';
import { useNova } from '../context/NovaStateContext';
import { forgetRecord } from './settings/sections/MemorySection';

interface MapNode {
  id: string;
  region: 'core' | 'memory' | 'working' | 'agents' | 'knowledge' | string;
  type: string;
  label: string;
  detail: string;
  size: number;
  accent: string;
  source_type?: string;
  updated?: number;
  confirmed?: boolean;
}

interface MapEdge {
  source: string;
  target: string;
  weight: number;
  type: 'semantic' | 'regional';
}

interface MindMap {
  nodes: MapNode[];
  edges: MapEdge[];
  stats: { total_nodes: number; total_edges: number; regions: Record<string, number> };
}

const W = 1200;
const H = 760;
const CX = W / 2;
const CY = H / 2;

/** Where each region sits around NOVA, as a centre angle (radians) and spread. */
const REGIONS: Record<string, { angle: number; spread: number; label: string }> = {
  memory: { angle: Math.PI, spread: 1.9, label: 'Memory' },
  working: { angle: -Math.PI / 2, spread: 1.3, label: 'Working' },
  agents: { angle: 0, spread: 1.35, label: 'Agents' },
  knowledge: { angle: Math.PI / 2, spread: 1.55, label: 'Tools & knowledge' },
};

/** Regions this small get every label; bigger ones label on hover or selection. */
const LABEL_ALL_BELOW = 7;

/** Deterministic radial layout: each region fans out in its own sector, on rings as it fills. */
function layout(nodes: MapNode[]): Map<string, { x: number; y: number; r: number }> {
  const pos = new Map<string, { x: number; y: number; r: number }>();
  const byRegion = new Map<string, MapNode[]>();
  for (const n of nodes) {
    if (n.region === 'core') {
      pos.set(n.id, { x: CX, y: CY, r: 22 });
      continue;
    }
    const list = byRegion.get(n.region) || [];
    list.push(n);
    byRegion.set(n.region, list);
  }
  for (const [region, list] of byRegion) {
    const spec = REGIONS[region] || { angle: Math.PI / 4, spread: 0.8 };
    const perRing = 10;
    list.forEach((n, i) => {
      const ring = Math.floor(i / perRing);
      const inRing = Math.min(perRing, list.length - ring * perRing);
      const slot = i % perRing;
      const frac = inRing === 1 ? 0.5 : slot / (inRing - 1);
      const a = spec.angle - spec.spread / 2 + frac * spec.spread + (ring % 2 ? spec.spread / (perRing * 2) : 0);
      const radius = 175 + ring * 70;
      pos.set(n.id, {
        x: CX + Math.cos(a) * radius * 1.55,
        y: CY + Math.sin(a) * radius * 0.98,
        r: 6 + Math.max(0, Math.min(1, n.size)) * 10,
      });
    });
  }
  return pos;
}

export const SynapticMapScreen: React.FC = () => {
  const { theme, addToast } = useNova();
  const rt = useRuntime();
  const [data, setData] = useState<MindMap | null>(null);
  const [error, setError] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [connections, setConnections] = useState<MapNode[]>([]);
  const [zoom, setZoom] = useState(1);
  const [query, setQuery] = useState('');
  const [remembering, setRemembering] = useState(false);
  const [fact, setFact] = useState('');
  const [confirmForget, setConfirmForget] = useState(false);
  const [hoverId, setHoverId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getJSON<MindMap>('/api/mind-map', 30000));
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(() => !document.hidden && load(), 30000);
    return () => clearInterval(t);
  }, [load]);

  const nodes = data?.nodes || [];
  const pos = useMemo(() => layout(nodes), [nodes]);
  const selected = nodes.find((n) => n.id === selectedId) || nodes.find((n) => n.region === 'core') || null;
  const q = query.trim().toLowerCase();
  const matches = (n: MapNode) => !q || n.label.toLowerCase().includes(q) || n.detail.toLowerCase().includes(q);
  const semantic = (data?.edges || []).some((e) => e.type === 'semantic');

  useEffect(() => {
    setConfirmForget(false);
    if (!selected) return;
    let cancelled = false;
    getJSON<{ connections: MapNode[] }>(`/api/mind-map/node/${encodeURIComponent(selected.id)}`, 15000)
      .then((j) => !cancelled && setConnections(j.connections || []))
      .catch(() => !cancelled && setConnections([]));
    return () => {
      cancelled = true;
    };
  }, [selected?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const forget = async () => {
    if (!selected || selected.updated == null) return;
    setConfirmForget(false);
    try {
      await forgetRecord({ text: selected.detail, updated: selected.updated });
      addToast('Forgotten', selected.label, 'success');
      setSelectedId(null);
      load();
    } catch (e) {
      addToast('Could not forget that', (e as Error).message, 'warning');
    }
  };

  const remember = (e: React.FormEvent) => {
    e.preventDefault();
    const t = fact.trim();
    if (!t) return;
    // Through NOVA, not around her: she stores it with her own memory tool,
    // the same way as when you tell her by voice.
    rt.sendText(`Please remember this: ${t}`);
    addToast('Sent to NOVA', 'She will store it and it will appear here.', 'info');
    setFact('');
    setRemembering(false);
    setTimeout(load, 6000);
  };

  const panel = {
    backgroundColor: theme.palette.glassSurface,
    borderColor: theme.palette.glassBorder,
    boxShadow: `0 15px 35px -10px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
  };
  const forgettable = selected?.region === 'memory' && selected.type === 'memory' && selected.updated != null;

  return (
    <div className="relative w-full h-full flex items-center justify-center select-none overflow-hidden font-sans">
      {/* ================= SEARCH & REMEMBER ================= */}
      <div className="absolute right-6 top-6 z-20 pointer-events-auto flex items-center gap-2">
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full border shadow-xl backdrop-blur-2xl text-xs" style={{ ...panel, color: theme.palette.textSecondary }}>
          <i className="fa-solid fa-magnifying-glass text-[11px] opacity-60" style={{ color: theme.palette.accent }} />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`Search ${nodes.length} nodes…`}
            aria-label="Search the memory map"
            className="bg-transparent outline-none w-44 text-xs font-sans placeholder:opacity-40 select-text"
            style={{ color: theme.palette.textPrimary }}
          />
        </div>
        <button
          onClick={() => setRemembering(true)}
          className="px-3 py-1.5 rounded-full border shadow-xl backdrop-blur-2xl text-xs flex items-center gap-1.5 font-medium transition-all hover:scale-105 cursor-pointer"
          style={{ backgroundColor: theme.palette.accent, borderColor: theme.palette.accentBorder, color: theme.isDark ? theme.palette.bgBase : '#ffffff' }}
        >
          <i className="fa-solid fa-plus text-[10px]" />
          <span>Remember</span>
        </button>
      </div>

      {remembering && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 backdrop-blur-xl animate-fade-in" style={{ backgroundColor: 'rgba(0,0,0,0.55)' }} onClick={() => setRemembering(false)}>
          <form onSubmit={remember} onClick={(e) => e.stopPropagation()} className="w-full max-w-md p-5 rounded-3xl border shadow-2xl space-y-3.5" style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder }}>
            <div className="flex items-center justify-between pb-2 border-b" style={{ borderColor: theme.palette.glassBorder }}>
              <h3 className="text-sm font-medium" style={{ color: theme.palette.textPrimary }}>
                Tell NOVA to remember
              </h3>
              <button type="button" onClick={() => setRemembering(false)} className="cursor-pointer opacity-60 hover:opacity-100" style={{ color: theme.palette.textSecondary }} aria-label="Close">
                <i className="fa-solid fa-xmark text-xs" />
              </button>
            </div>
            <textarea
              value={fact}
              onChange={(e) => setFact(e.target.value)}
              placeholder="e.g. I prefer meetings after 10am."
              required
              rows={3}
              maxLength={1000}
              autoFocus
              aria-label="What NOVA should remember"
              className="w-full text-xs p-2.5 rounded-xl border bg-transparent outline-none font-sans select-text"
              style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary }}
            />
            <div className="text-[10px]" style={{ color: theme.palette.textMuted }}>
              This goes to NOVA as a message; she stores it with her memory tool.
            </div>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setRemembering(false)} className="px-3 py-1.5 rounded-xl border text-xs cursor-pointer" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}>
                Cancel
              </button>
              <button type="submit" className="px-4 py-1.5 rounded-xl text-xs font-medium border cursor-pointer" style={{ backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#fff', borderColor: theme.palette.accentBorder }}>
                Remember
              </button>
            </div>
          </form>
        </div>
      )}

      {/* ================= GRAPH ================= */}
      <div className="relative w-full h-full flex items-center justify-center overflow-hidden">
        {!data ? (
          <div className="text-xs" style={{ color: error ? '#ef4444' : theme.palette.textMuted }}>
            {error ? `Could not load the memory map: ${error}` : 'Loading the memory map…'}
          </div>
        ) : (
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" className="w-full h-full max-w-[1400px] max-h-[820px] transition-transform duration-500 ease-out" style={{ transform: `scale(${zoom})` }} role="img" aria-label="Memory map">
            <g opacity="0.10" stroke={theme.palette.accent}>
              {[175, 245, 315].map((r) => (
                <ellipse key={r} cx={CX} cy={CY} rx={r * 1.55} ry={r * 0.98} fill="none" strokeDasharray="3 7" />
              ))}
            </g>

            <g>
              {(data.edges || []).slice(0, 600).map((e, i) => {
                const a = pos.get(e.source);
                const b = pos.get(e.target);
                if (!a || !b) return null;
                const lit = selected && (e.source === selected.id || e.target === selected.id);
                return (
                  <line
                    key={i}
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke={theme.palette.accent}
                    strokeWidth={lit ? 1.6 : 0.9}
                    opacity={lit ? 0.6 : e.type === 'semantic' ? 0.28 : 0.12}
                    strokeDasharray={e.type === 'semantic' ? undefined : '3 4'}
                  />
                );
              })}
              {/* Every region hangs off NOVA herself. */}
              {nodes
                .filter((n) => n.region !== 'core')
                .reduce<MapNode[]>((acc, n) => (acc.some((m) => m.region === n.region) ? acc : [...acc, n]), [])
                .map((n) => {
                  const b = pos.get(n.id);
                  return b ? <line key={`core-${n.region}`} x1={CX} y1={CY} x2={b.x} y2={b.y} stroke={theme.palette.accent} strokeWidth={1} opacity={0.18} strokeDasharray="4 3" /> : null;
                })}
            </g>

            <g fill={theme.palette.textMuted} fontFamily="'JetBrains Mono', monospace" fontSize="10" opacity="0.55">
              {Object.entries(REGIONS).map(([id, r]) =>
                data.stats.regions[id] ? (
                  <text key={id} x={CX + Math.cos(r.angle) * 120 * 1.55} y={CY + Math.sin(r.angle) * 120 * 0.98 + 4} textAnchor="middle">
                    {r.label.toUpperCase()} · {data.stats.regions[id]}
                  </text>
                ) : null,
              )}
            </g>

            <g>
              {nodes.map((n) => {
                const p = pos.get(n.id);
                if (!p) return null;
                const sel = selected?.id === n.id;
                const core = n.region === 'core';
                const hit = matches(n);
                return (
                  <g
                    key={n.id}
                    onClick={() => setSelectedId(n.id)}
                    onMouseEnter={() => setHoverId(n.id)}
                    onMouseLeave={() => setHoverId((h) => (h === n.id ? null : h))}
                    className="cursor-pointer"
                    opacity={hit ? 1 : 0.18}
                  >
                    <title>{n.detail}</title>
                    {sel && <circle cx={p.x} cy={p.y} r={p.r + 5} fill="none" stroke={theme.palette.accent} strokeWidth="1.5" opacity="0.6" strokeDasharray="2 2" />}
                    <circle cx={p.x} cy={p.y} r={p.r} fill={theme.palette.bgElevated} stroke={sel ? theme.palette.accent : n.accent} strokeWidth={sel ? 2 : 1} strokeOpacity={sel ? 1 : 0.6} />
                    <circle cx={p.x} cy={p.y} r={p.r * 0.4} fill={sel ? theme.palette.accent : n.accent} opacity={core ? 0.95 : 0.7} />
                    {(core || sel || hoverId === n.id || (q && hit) || (data.stats.regions[n.region] ?? 0) < LABEL_ALL_BELOW) && (
                      <text x={p.x} y={p.y + p.r + 13} textAnchor="middle" fill={sel ? theme.palette.textPrimary : theme.palette.textSecondary} fontFamily="'Space Grotesk', sans-serif" fontSize={core ? 13 : 10} fontWeight={sel || core ? '600' : '400'}>
                        {n.label.length > 34 ? `${n.label.slice(0, 32)}…` : n.label}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          </svg>
        )}

        <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-20 pointer-events-auto flex items-center gap-2.5">
          <button onClick={() => setZoom((z) => Math.max(0.7, +(z - 0.1).toFixed(2)))} className="w-6 h-6 rounded-full border flex items-center justify-center text-[10px] transition-all hover:scale-105 cursor-pointer" style={{ ...panel, color: theme.palette.textSecondary }} title="Zoom out" aria-label="Zoom out">
            <i className="fa-solid fa-minus" />
          </button>
          <div className="flex items-center space-x-2 px-3.5 py-1.5 rounded-full border backdrop-blur-2xl text-[10px] font-sans shadow-md" style={{ ...panel, color: theme.palette.textPrimary }}>
            <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: theme.palette.accent }} />
            <span className="tracking-wide">MEMORY MAP · {data?.stats.total_nodes ?? 0} NODES</span>
            <span className="opacity-30">|</span>
            <span className="opacity-60 font-mono">{data?.stats.total_edges ?? 0} LINKS</span>
          </div>
          <button onClick={() => setZoom((z) => Math.min(1.4, +(z + 0.1).toFixed(2)))} className="w-6 h-6 rounded-full border flex items-center justify-center text-[10px] transition-all hover:scale-105 cursor-pointer" style={{ ...panel, color: theme.palette.textSecondary }} title="Zoom in" aria-label="Zoom in">
            <i className="fa-solid fa-plus" />
          </button>
        </div>
      </div>

      {/* ================= BOTTOM-LEFT: MAP FACTS ================= */}
      <div className="absolute left-20 bottom-8 z-20 pointer-events-auto w-64">
        <div className="p-4 rounded-2xl border shadow-xl backdrop-blur-2xl transition-all duration-300 space-y-3 font-sans" style={panel}>
          <div className="flex items-center justify-between pb-2 border-b text-xs" style={{ borderColor: theme.palette.glassBorder }}>
            <span className="font-semibold uppercase tracking-wider text-[10px]" style={{ color: theme.palette.accent }}>
              What NOVA knows
            </span>
            <button onClick={load} className="text-[10px] font-mono opacity-60 hover:opacity-100 cursor-pointer" style={{ color: theme.palette.textSecondary }} title="Refresh">
              <i className="fa-solid fa-rotate" />
            </button>
          </div>
          <div className="space-y-2 text-xs">
            {Object.entries(REGIONS).map(([id, r]) => (
              <div key={id} className="flex items-center justify-between" style={{ color: theme.palette.textSecondary }}>
                <span>{r.label}</span>
                <span className="font-semibold font-mono" style={{ color: theme.palette.textPrimary }}>
                  {data?.stats.regions[id] ?? 0}
                </span>
              </div>
            ))}
          </div>
          <div className="pt-2 border-t text-[10px] font-mono flex items-center justify-between opacity-60" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textMuted }}>
            <span>{semantic ? 'links by meaning' : 'links by region'}</span>
            <span>{rt.status?.memory.enabled === false ? 'memory off' : 'stored locally'}</span>
          </div>
        </div>
      </div>

      {/* ================= BOTTOM-RIGHT: INSPECTOR ================= */}
      {selected && (
        <div className="absolute right-6 bottom-8 z-20 pointer-events-auto w-80 max-w-[340px]">
          <div className="p-4 rounded-2xl border shadow-xl backdrop-blur-2xl transition-all duration-300 flex flex-col space-y-2.5" style={panel}>
            <div className="flex items-center justify-between pb-2 border-b text-xs" style={{ borderColor: theme.palette.glassBorder }}>
              <div className="flex items-center space-x-1.5 text-[10px] uppercase tracking-wider font-semibold" style={{ color: theme.palette.textPrimary }}>
                <i className="fa-solid fa-database text-[11px] opacity-70" />
                <span>Inspector</span>
              </div>
              <span className="text-[9px] font-mono px-2 py-0.5 rounded-full border" style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder, color: theme.palette.accent }}>
                {selected.region} · {selected.source_type || selected.type}
              </span>
            </div>
            <div className="p-3 rounded-xl border text-xs leading-relaxed font-sans max-h-40 overflow-y-auto select-text" style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary }}>
              {selected.detail}
            </div>
            <div className="flex items-center justify-between text-[10px] font-mono" style={{ color: theme.palette.textMuted }}>
              <span>{connections.length} connections</span>
              <span>
                {selected.updated ? new Date(selected.updated * 1000).toLocaleDateString() : ''}
                {selected.confirmed === false ? ' · unconfirmed' : ''}
              </span>
            </div>
            {connections.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {connections.slice(0, 6).map((c) => (
                  <button key={c.id} onClick={() => setSelectedId(c.id)} className="text-[10px] px-2 py-0.5 rounded-full border truncate max-w-[140px] cursor-pointer hover:opacity-100 opacity-80" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }} title={c.detail}>
                    {c.label}
                  </button>
                ))}
              </div>
            )}
            {forgettable && (
              <div className="pt-1.5 border-t flex space-x-2 text-xs" style={{ borderColor: theme.palette.glassBorder }}>
                {confirmForget ? (
                  <>
                    <button onClick={() => setConfirmForget(false)} className="flex-1 py-1.5 px-2 rounded-xl border cursor-pointer" style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}>
                      Keep it
                    </button>
                    <button onClick={forget} className="flex-1 py-1.5 px-2 rounded-xl border font-medium cursor-pointer" style={{ backgroundColor: 'rgba(239,68,68,0.12)', borderColor: 'rgba(239,68,68,0.35)', color: '#ef4444' }}>
                      Yes, forget
                    </button>
                  </>
                ) : (
                  <button onClick={() => setConfirmForget(true)} className="flex-1 py-1.5 px-2 rounded-xl border flex items-center justify-center gap-1.5 transition-all cursor-pointer hover:opacity-100" style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}>
                    <i className="fa-solid fa-trash-can text-[10px]" /> Forget this
                  </button>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
