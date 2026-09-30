import React from 'react';
import { getJSON, postJSON } from '../../../nova/api';
import { useNova } from '../../../context/NovaStateContext';
import { Button, Card, Loading, SectionHeader, Stat, useResource } from '../primitives';

interface Verification {
  verdict?: string | null;
  passed?: number | null;
  total?: number | null;
  tests?: { id: string; kind: string; pass: boolean; reason?: string; question?: string }[];
}

interface Domain {
  id: string;
  name: string;
  scope: string;
  status: string;
  sources: string[];
  item_count: number;
  contradiction_count?: number;
  updated: number;
  verification: Verification;
}

interface Session {
  id: string;
  domain: string;
  source: string;
  status: string;
  phase: string;
  counts: Record<string, unknown>;
  failed: Record<string, string>;
  skipped: Record<string, string>;
  message: string;
  verification: Verification | null;
}

interface Item {
  id: string;
  kind: string;
  statement: string;
  confidence: number;
  status: string;
  contested?: boolean;
  support: { rel: string; quote?: string; authority?: string }[];
}

interface Contradiction {
  id: string;
  a_statement: string;
  b_statement: string;
  a_sources: string[];
  b_sources: string[];
  resolved_by: string;
  explanation?: string;
}

interface Overview {
  domains: Domain[];
  sessions: Session[];
  timeline: { at: number; event: string; domain: string; detail: string }[];
}

const STATUS_WORD: Record<string, { label: string; color: string }> = {
  learned: { label: 'learned — verified', color: '#10b981' },
  unverified: { label: 'analyzed — not verified', color: '#f59e0b' },
  learning: { label: 'learning…', color: '#60a5fa' },
  failed: { label: 'could not learn', color: '#ef4444' },
};

const SESSION_WORD: Record<string, string> = {
  running: 'working', paused: 'paused', interrupted: 'interrupted — continue to resume',
  waiting_for_model: 'waiting for the model', failed: 'failed', done: 'finished',
};

function when(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
}

const DomainDetail: React.FC<{ domain: Domain }> = ({ domain }) => {
  const { theme } = useNova();
  const d = useResource(
    () => getJSON<{ items: Item[]; contradictions: Contradiction[] }>(`/api/knowledge/domains/${encodeURIComponent(domain.id)}`, 10000),
    [domain.id, domain.updated],
  );
  if (!d.data) return <Loading what="knowledge" error={d.error} />;
  const active = d.data.items.filter((i) => i.status === 'active').sort((a, b) => b.confidence - a.confidence);
  return (
    <div className="space-y-3 pt-2">
      {domain.verification?.passed != null && (
        <div className="text-[11px]" style={{ color: theme.palette.textSecondary }}>
          Verification: {domain.verification.passed} of {domain.verification.total} checks passed
        </div>
      )}
      <div className="space-y-1.5">
        {active.slice(0, 40).map((i) => (
          <div key={i.id} className="text-[11px] leading-relaxed" style={{ color: theme.palette.textPrimary }}>
            <span className="font-mono text-[10px] opacity-50 mr-1.5">{i.kind}</span>
            {i.statement}
            {i.contested && <span style={{ color: '#f59e0b' }}> — the material disagrees</span>}
            <div className="text-[10px] opacity-50 font-mono" style={{ color: theme.palette.textMuted }}>
              {Math.round(i.confidence * 100)}% · from {[...new Set(i.support.map((s) => s.rel))].join(', ')}
            </div>
          </div>
        ))}
        {active.length === 0 && <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>No knowledge was kept.</div>}
      </div>
      {d.data.contradictions.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-[10px] font-mono uppercase tracking-wider opacity-60" style={{ color: theme.palette.textMuted }}>
            Where the material disagrees
          </div>
          {d.data.contradictions.map((c) => (
            <div key={c.id} className="text-[11px]" style={{ color: theme.palette.textSecondary }}>
              “{c.a_statement}” ({c.a_sources.join(', ')}) vs “{c.b_statement}” ({c.b_sources.join(', ')}) —{' '}
              {c.resolved_by ? `followed by ${c.resolved_by}: ${c.explanation}` : c.explanation || 'unresolved'}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export const KnowledgeSection: React.FC = () => {
  const { theme, addToast } = useNova();
  const ov = useResource(() => getJSON<Overview>('/api/knowledge', 10000), [], 4000);
  const [path, setPath] = React.useState('');
  const [name, setName] = React.useState('');
  const [scope, setScope] = React.useState('personal');
  const [busy, setBusy] = React.useState('');
  const [open, setOpen] = React.useState('');

  const call = async (label: string, fn: () => Promise<unknown>, done?: string) => {
    setBusy(label);
    try {
      await fn();
      if (done) addToast(done, undefined, 'success');
    } catch (e) {
      addToast(`${label} failed`, (e as Error).message, 'warning');
    } finally {
      setBusy('');
      ov.reload();
    }
  };

  const domains = Array.isArray(ov.data?.domains) ? ov.data!.domains : [];
  const sessions = Array.isArray(ov.data?.sessions) ? ov.data!.sessions : [];
  const live = sessions.filter((s) => s.status !== 'done' && s.status !== 'failed');

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader
        title="Knowledge"
        subtitle="What you have taught NOVA on purpose. A folder counts as learned only after NOVA passes checks on it; every piece of knowledge keeps the files it came from."
      />

      <Card title="Teach NOVA a folder">
        <div className="space-y-2">
          <input aria-label="Folder to learn" value={path} onChange={(e) => setPath(e.target.value)}
            placeholder="C:\Users\you\DesignKnowledge"
            className="w-full text-xs p-2.5 rounded-xl border outline-none font-mono placeholder:opacity-40 select-text"
            style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface }} />
          <div className="flex gap-2">
            <input aria-label="What it teaches" value={name} onChange={(e) => setName(e.target.value)}
              placeholder="What it teaches, e.g. Design"
              className="flex-1 text-xs p-2.5 rounded-xl border outline-none placeholder:opacity-40 select-text"
              style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface }} />
            <select aria-label="Scope" value={scope} onChange={(e) => setScope(e.target.value)}
              className="text-xs p-2 rounded-xl border outline-none"
              style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface }}>
              <option value="personal">Lasting — how I work</option>
              <option value="reference">Reference only</option>
            </select>
            <Button tone="accent" disabled={!!busy || !path.trim() || !name.trim()}
              title={!path.trim() || !name.trim() ? 'Enter a folder and what it teaches' : undefined}
              onClick={() => call('Learning', () => postJSON('/api/knowledge/learn', { path: path.trim(), domain: name.trim(), scope }), 'Learning started')}>
              Learn
            </Button>
          </div>
          <div className="text-[10px]" style={{ color: theme.palette.textMuted }}>
            The files are read on this computer and their content is sent to NOVA's model to study. You can also just say “learn this folder”.
          </div>
        </div>
      </Card>

      {!ov.data ? (
        <Loading what="knowledge" error={ov.error} />
      ) : (
        <>
          {live.length > 0 && (
            <Card title="Learning now">
              <div className="space-y-2">
                {live.map((s) => (
                  <div key={s.id} className="p-3 rounded-xl border" style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}>
                    <div className="flex justify-between gap-3 text-xs">
                      <span style={{ color: theme.palette.textPrimary }}>{s.domain}</span>
                      <span className="font-mono text-[10px]" style={{ color: theme.palette.accent }}>{SESSION_WORD[s.status] || s.status} · {s.phase}</span>
                    </div>
                    <div className="text-[10px] font-mono opacity-60 mt-1" style={{ color: theme.palette.textMuted }}>
                      {Object.entries(s.counts || {}).filter(([, v]) => typeof v === 'number' || typeof v === 'string')
                        .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`).join(' · ')}
                    </div>
                    {s.message && <div className="text-[11px] mt-1" style={{ color: theme.palette.textSecondary }}>{s.message}</div>}
                    <div className="flex gap-2 mt-2">
                      {s.status === 'running' ? (
                        <Button onClick={() => call('Pausing', () => postJSON(`/api/knowledge/sessions/${s.id}/pause`, {}))} disabled={!!busy}>Pause</Button>
                      ) : (
                        <Button tone="accent" onClick={() => call('Continuing', () => postJSON(`/api/knowledge/sessions/${s.id}/resume`, {}))} disabled={!!busy}>Continue</Button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <div className="grid grid-cols-3 gap-2">
            <Stat label="Domains" value={domains.length} />
            <Stat label="Verified" value={domains.filter((d) => d.status === 'learned').length} tone="ok" />
            <Stat label="Knowledge" value={domains.reduce((n, d) => n + (d.item_count || 0), 0)} />
          </div>

          <Card title="What NOVA has learned">
            {domains.length === 0 ? (
              <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>Nothing yet.</div>
            ) : (
              <div className="space-y-2">
                {domains.map((d) => {
                  const st = STATUS_WORD[d.status] || { label: d.status, color: theme.palette.textMuted };
                  return (
                    <div key={d.id} className="p-3 rounded-xl border" style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}>
                      <div className="flex justify-between gap-3">
                        <div className="min-w-0">
                          <div className="text-xs font-medium" style={{ color: theme.palette.textPrimary }}>{d.name}</div>
                          <div className="text-[10px] font-mono opacity-50 truncate" style={{ color: theme.palette.textMuted }}>
                            {d.scope} · {d.item_count} items{d.contradiction_count ? ` · ${d.contradiction_count} open disagreement(s)` : ''} · {(d.sources || []).join(', ')}
                          </div>
                        </div>
                        <span className="text-[10px] font-mono shrink-0" style={{ color: st.color }}>{st.label}</span>
                      </div>
                      <div className="flex gap-2 mt-2">
                        <Button onClick={() => setOpen(open === d.id ? '' : d.id)}>{open === d.id ? 'Hide' : 'Show knowledge'}</Button>
                        {d.sources?.[0] && (
                          <Button onClick={() => call('Updating', () => postJSON('/api/knowledge/learn', { path: d.sources[0], domain: d.name, scope: d.scope }), 'Checking the folder for changes')} disabled={!!busy}>
                            Update from folder
                          </Button>
                        )}
                        <Button tone="danger" onClick={() => call('Forgetting', () => postJSON(`/api/knowledge/domains/${encodeURIComponent(d.id)}`, undefined, 'DELETE'), `Forgot ${d.name}`)} disabled={!!busy}>
                          Forget
                        </Button>
                      </div>
                      {open === d.id && <DomainDetail domain={d} />}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title="Recent learning">
            {ov.data.timeline.length === 0 ? (
              <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>Nothing yet.</div>
            ) : (
              <div className="space-y-1.5">
                {ov.data.timeline.slice(0, 20).map((e, i) => (
                  <div key={`${e.at}-${i}`} className="flex gap-3 text-[11px]">
                    <span className="font-mono text-[10px] opacity-50 shrink-0 w-24" style={{ color: theme.palette.textMuted }}>{when(e.at)}</span>
                    <span style={{ color: theme.palette.textSecondary }}>
                      {e.event.replace(/_/g, ' ')} <span style={{ color: theme.palette.textPrimary }}>{e.domain}</span>
                      {e.detail ? <span className="opacity-60"> — {e.detail}</span> : null}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
};
