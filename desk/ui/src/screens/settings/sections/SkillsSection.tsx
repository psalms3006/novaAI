import React from 'react';
import { getJSON, postJSON } from '../../../nova/api';
import { useNova } from '../../../context/NovaStateContext';
import { Button, Card, Loading, SectionHeader, Stat, useResource } from '../primitives';

interface Provider {
  id: string;
  kind: string;
  name: string;
  cost: string;
  data_leaves_device: boolean;
  health: string;
}

interface Skill {
  id: string;
  name: string;
  description: string;
  health: string;
  learned: boolean;
  version: number;
  versions: { number: number; passed_test: boolean }[];
  source: string;
  providers: Provider[];
  last_verified: number;
}

interface TimelineEvent {
  at: number;
  event: string;
  capability_id: string;
  detail: string;
}

interface Overview {
  capabilities: Skill[];
  timeline: TimelineEvent[];
  pending: Skill[];
}

const HEALTH_COLOR: Record<string, string> = {
  available: '#10b981',
  degraded: '#f59e0b',
  auth_required: '#f59e0b',
  updating: '#60a5fa',
  unverified: '#94a3b8',
  unavailable: '#ef4444',
  broken: '#ef4444',
  deprecated: '#94a3b8',
};

const HEALTH_LABEL: Record<string, string> = {
  available: 'working',
  degraded: 'degraded',
  auth_required: 'reconnect needed',
  updating: 'updating',
  unverified: 'not tested yet',
  unavailable: 'unavailable',
  broken: 'broken',
  deprecated: 'retired',
};

const EVENT_LABEL: Record<string, string> = {
  discovered: 'found a route for',
  composed: 'worked out',
  learned: 'learned',
  verified: 're-verified',
  test_failed: 'test failed for',
  version_proposed: 'is trying a new version of',
  version_tested: 'tested a new version of',
  updated: 'updated',
  rolled_back: 'rolled back',
  removed: 'removed',
};

function when(ts: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** Connect an account for one provider. The key goes straight to the OS
 *  credential store through the backend; it is never shown again. */
const ConnectForm: React.FC<{ skill: Skill; onDone: () => void }> = ({ skill, onDone }) => {
  const { theme } = useNova();
  const [providerId, setProviderId] = React.useState(skill.providers[0]?.id || '');
  const [secret, setSecret] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState('');
  const connect = async () => {
    setBusy(true);
    setMsg('');
    try {
      const r = await postJSON<{ ok: boolean; learned: boolean; detail: string }>(
        `/api/skills/${encodeURIComponent(skill.id)}/credential`,
        { provider_id: providerId, secret },
      );
      setSecret('');
      setMsg(r.learned ? 'Connected and tested — NOVA can use this now.' : `Connected, but the test failed: ${r.detail}`);
      onDone();
    } catch (e) {
      setMsg(`Not connected: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2 pt-2">
      {skill.providers.length > 1 && (
        <select
          aria-label="Service"
          value={providerId}
          onChange={(e) => setProviderId(e.target.value)}
          className="w-full text-xs p-2 rounded-xl border outline-none"
          style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface }}
        >
          {skill.providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      )}
      <div className="flex gap-2">
        <input
          aria-label="API key or token"
          type="password"
          autoComplete="off"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          placeholder={`API key for ${skill.providers.find((p) => p.id === providerId)?.name || 'this service'}`}
          className="flex-1 text-xs p-2.5 rounded-xl border outline-none font-mono placeholder:opacity-40 select-text"
          style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface }}
        />
        <Button tone="accent" onClick={connect} disabled={busy || !secret.trim() || !providerId}>
          {busy ? 'Testing…' : 'Connect'}
        </Button>
      </div>
      <div className="text-[10px]" style={{ color: theme.palette.textMuted }}>
        Stored in Windows' credential store for your account only. NOVA's model never sees it.
      </div>
      {msg && (
        <div className="text-[11px]" style={{ color: theme.palette.textSecondary }} role="status">
          {msg}
        </div>
      )}
    </div>
  );
};

export const SkillsSection: React.FC = () => {
  const { theme } = useNova();
  const ov = useResource(() => getJSON<Overview>('/api/skills', 10000), [], 15000);
  const [busy, setBusy] = React.useState('');
  const [note, setNote] = React.useState('');

  const act = async (label: string, path: string, method = 'POST') => {
    setBusy(label);
    setNote('');
    try {
      const r = await postJSON<{ ok?: boolean; detail?: string; error?: string; version?: number; changed?: unknown[] }>(path, {}, method);
      if (r.detail) setNote(r.detail);
      else if (r.version) setNote(`Back on version ${r.version}.`);
      else if (Array.isArray(r.changed)) setNote(r.changed.length ? `${r.changed.length} skill(s) changed health.` : 'All skills unchanged.');
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy('');
      ov.reload();
    }
  };

  const skills = Array.isArray(ov.data?.capabilities) ? ov.data!.capabilities : [];
  const learned = skills.filter((s) => s.learned);
  const names = Object.fromEntries(skills.map((s) => [s.id, s.name]));

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader
        title="Skills"
        subtitle="What NOVA has learned to do beyond her built-in tools. A skill counts as learned only after it has passed its own test on this computer."
        right={
          <Button onClick={() => act('check', '/api/skills/check')} disabled={!!busy}>
            {busy === 'check' ? 'Checking…' : 'Check health'}
          </Button>
        }
      />

      {!ov.data ? (
        <Loading what="skills" error={ov.error} />
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            <Stat label="Learned" value={learned.length} tone={learned.length ? 'ok' : 'muted'} />
            <Stat label="Working now" value={learned.filter((s) => s.health === 'available').length} />
            <Stat label="Need you" value={skills.filter((s) => !s.learned || s.health === 'auth_required').length} tone={skills.some((s) => s.health === 'auth_required') ? 'warn' : 'muted'} />
          </div>

          {note && (
            <div className="text-[11px]" style={{ color: theme.palette.textSecondary }} role="status">
              {note}
            </div>
          )}

          <Card title="Her skills">
            {skills.length === 0 ? (
              <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
                None yet. When you ask NOVA for something she can&apos;t do, she investigates — her own tools first, then services she knows about — and anything she sets up appears here.
              </div>
            ) : (
              <div className="space-y-2">
                {skills.map((s) => {
                  const needsKey = s.providers.some((p) => p.kind === 'http_api') && (s.health === 'auth_required' || !s.learned);
                  const canRollBack = s.versions.some((v) => v.number < s.version && v.passed_test);
                  return (
                    <div key={s.id} className="p-3 rounded-xl border" style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}>
                      <div className="flex justify-between gap-3">
                        <div className="min-w-0">
                          <div className="text-xs font-medium truncate" style={{ color: theme.palette.textPrimary }}>
                            {s.name}
                          </div>
                          <div className="text-[11px] opacity-70" style={{ color: theme.palette.textSecondary }}>
                            {s.description}
                          </div>
                          <div className="text-[10px] font-mono opacity-50 mt-0.5" style={{ color: theme.palette.textMuted }}>
                            v{s.version} · {s.providers.map((p) => `${p.name}${p.cost && p.cost !== 'free' ? ` (${p.cost})` : ''}`).join(', ') || 'her own tools'}
                            {s.providers.some((p) => p.data_leaves_device) ? ' · sends content out' : ''}
                            {s.last_verified ? ` · verified ${when(s.last_verified)}` : ''}
                          </div>
                        </div>
                        <span className="text-[10px] font-mono shrink-0" style={{ color: HEALTH_COLOR[s.health] || theme.palette.textMuted }}>
                          {HEALTH_LABEL[s.health] || s.health}
                        </span>
                      </div>
                      <div className="flex gap-2 mt-2">
                        <Button onClick={() => act(`test:${s.id}`, `/api/skills/${encodeURIComponent(s.id)}/test`)} disabled={!!busy}>
                          {busy === `test:${s.id}` ? 'Testing…' : 'Test'}
                        </Button>
                        {canRollBack && (
                          <Button onClick={() => act(`rb:${s.id}`, `/api/skills/${encodeURIComponent(s.id)}/rollback`)} disabled={!!busy}>
                            Roll back
                          </Button>
                        )}
                        <Button tone="danger" onClick={() => act(`rm:${s.id}`, `/api/skills/${encodeURIComponent(s.id)}`, 'DELETE')} disabled={!!busy}>
                          Remove
                        </Button>
                      </div>
                      {needsKey && <ConnectForm skill={s} onDone={ov.reload} />}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title="How she grew">
            {ov.data.timeline.length === 0 ? (
              <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
                Nothing yet.
              </div>
            ) : (
              <div className="space-y-1.5">
                {ov.data.timeline.map((e, i) => (
                  <div key={`${e.at}-${i}`} className="flex gap-3 text-[11px]">
                    <span className="font-mono text-[10px] opacity-50 shrink-0 w-24" style={{ color: theme.palette.textMuted }}>
                      {when(e.at)}
                    </span>
                    <span style={{ color: theme.palette.textSecondary }}>
                      NOVA {EVENT_LABEL[e.event] || e.event.replace(/_/g, ' ')}{' '}
                      <span style={{ color: theme.palette.textPrimary }}>{names[e.capability_id] || e.capability_id}</span>
                      {e.detail && e.event !== 'composed' && e.event !== 'discovered' ? <span className="opacity-60"> — {e.detail}</span> : null}
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
