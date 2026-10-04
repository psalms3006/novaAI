import React, { useState } from 'react';
import { api, getJSON, postJSON } from '../../../nova/api';
import { useNova } from '../../../context/NovaStateContext';
import { Button, Card, Loading, Row, SectionHeader, Stat, TextField, Toggle, useResource } from '../primitives';

export interface MemoryRecord {
  text: string;
  type: string;
  importance: number;
  project?: string;
  confirmed: boolean;
  updated: number;
  superseded: boolean;
  decayed: boolean;
}

interface MemoryResponse {
  facts: string[];
  records: MemoryRecord[];
  enabled: boolean;
}

/** Forget one memory record. Shared with the memory map. */
export async function forgetRecord(r: Pick<MemoryRecord, 'text' | 'updated'>): Promise<void> {
  const res = await api('/api/memory/records', { method: 'DELETE', body: JSON.stringify({ text: r.text, updated: r.updated }) });
  const j = (await res.json().catch(() => ({}))) as { ok?: boolean; error?: string };
  if (!res.ok || j.ok === false) throw new Error(j.error || `could not forget (${res.status})`);
}

export const MemorySection: React.FC = () => {
  const { theme, addToast } = useNova();
  const [query, setQuery] = useState('');
  const [confirmClear, setConfirmClear] = useState(false);
  const mem = useResource(() => getJSON<MemoryResponse>(`/api/memory${query.trim() ? `?q=${encodeURIComponent(query.trim())}` : ''}`, 15000), [query]);

  const toggle = async (enabled: boolean) => {
    try {
      await postJSON('/api/memory/toggle', { enabled });
      addToast(enabled ? 'Memory on' : 'Memory off', enabled ? 'NOVA will remember and use what she learns.' : 'NOVA will neither use nor add memories.', 'info');
      mem.reload();
    } catch (e) {
      addToast('Could not change memory', (e as Error).message, 'warning');
    }
  };

  const forget = async (r: MemoryRecord) => {
    try {
      await forgetRecord(r);
      addToast('Forgotten', r.text.slice(0, 80), 'success');
      mem.reload();
    } catch (e) {
      addToast('Could not forget that', (e as Error).message, 'warning');
    }
  };

  const clearAll = async () => {
    setConfirmClear(false);
    try {
      const j = await postJSON<{ cleared: number }>('/api/memory/clear', { confirm: true });
      addToast('Memory cleared', `${j.cleared} memories removed.`, 'success');
      mem.reload();
    } catch (e) {
      addToast('Could not clear memory', (e as Error).message, 'warning');
    }
  };

  const records = (mem.data?.records || []).filter((r) => !r.superseded);

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Memory" subtitle="What NOVA has learned about you and your work. Everything here is stored on this computer." />

      <Card title="Memory">
        <Row first label="Remember things" hint="Off: NOVA neither adds new memories nor uses the ones she has.">
          <Toggle label="Memory enabled" checked={Boolean(mem.data?.enabled)} onChange={toggle} disabled={!mem.data} />
        </Row>
        {mem.data && (
          <div className="grid grid-cols-3 gap-2">
            <Stat label="Memories" value={records.length} />
            <Stat label="Confirmed by you" value={records.filter((r) => r.confirmed).length} />
            <Stat label="Facts" value={mem.data.facts.length} />
          </div>
        )}
      </Card>

      <Card title="What she remembers">
        <TextField label="Search memories" value={query} onChange={setQuery} placeholder="Search…" />
        {!mem.data ? (
          <Loading what="memories" error={mem.error} />
        ) : records.length === 0 ? (
          <div className="text-[11px] py-2" style={{ color: theme.palette.textMuted }}>
            {query ? 'Nothing matches.' : 'NOVA has not stored any memories yet.'}
          </div>
        ) : (
          <div className="space-y-1.5 max-h-[420px] overflow-y-auto pr-1">
            {records.map((r) => (
              <div
                key={`${r.updated}-${r.text.slice(0, 40)}`}
                className="p-2.5 rounded-xl border flex items-start justify-between gap-3"
                style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}
              >
                <div className="min-w-0">
                  <div className="text-xs leading-relaxed select-text" style={{ color: theme.palette.textPrimary }}>
                    {r.text}
                  </div>
                  <div className="text-[10px] font-mono opacity-50 mt-0.5" style={{ color: theme.palette.textMuted }}>
                    {r.type} · importance {r.importance.toFixed(2)}
                    {r.confirmed ? ' · confirmed' : ''}
                    {r.decayed ? ' · fading' : ''}
                    {r.updated ? ` · ${new Date(r.updated * 1000).toLocaleDateString()}` : ''}
                  </div>
                </div>
                <button
                  onClick={() => forget(r)}
                  className="text-[11px] opacity-50 hover:opacity-100 shrink-0 cursor-pointer"
                  style={{ color: '#ef4444' }}
                  title="Forget this"
                  aria-label={`Forget: ${r.text.slice(0, 40)}`}
                >
                  <i className="fa-solid fa-trash-can" />
                </button>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Forget everything">
        <Row first label="Clear all memories" hint="Removes everything NOVA has learned. This cannot be undone.">
          {confirmClear ? (
            <div className="flex gap-2">
              <Button onClick={() => setConfirmClear(false)}>Keep them</Button>
              <Button tone="danger" onClick={clearAll}>
                Yes, forget everything
              </Button>
            </div>
          ) : (
            <Button tone="danger" onClick={() => setConfirmClear(true)} disabled={!records.length}>
              Clear…
            </Button>
          )}
        </Row>
      </Card>
    </div>
  );
};
