import React, { useState } from 'react';
import { getJSON, postJSON } from '../../../nova/api';
import { useRuntime } from '../../../nova/runtime';
import { useNova } from '../../../context/NovaStateContext';
import { useNovaSettings } from '../../../context/NovaSettingsContext';
import type { BackendSettings } from '../../../types/settings';
import { Button, Card, Loading, NotConnected, Row, Select, SectionHeader, Stat, useResource } from '../primitives';

interface LocalStatus {
  runtime_available: boolean;
  current_model: string;
  current_model_installed: boolean;
  installed_count: number;
  installed_models: { name: string }[] | string[];
  error?: string;
}

interface LocalModel {
  id: string;
  name: string;
  description: string;
  size_gb: number;
  recommended_ram_gb: number;
  speed: string;
  installed: boolean;
  recommended?: boolean;
}

interface OfflineKnowledge {
  libzim_available: boolean;
  installed_count: number;
  available_count: number;
  downloading?: string | null;
}

export const IntelligenceSection: React.FC = () => {
  const { theme, addToast } = useNova();
  const { status } = useRuntime();
  const { settings, update } = useNovaSettings();
  const local = useResource(() => getJSON<LocalStatus>('/api/local-intelligence/status', 10000), [], 15000);
  const models = useResource(() => getJSON<{ models: LocalModel[] }>('/api/local-intelligence/models', 10000));
  const knowledge = useResource(() => getJSON<OfflineKnowledge>('/api/offline-knowledge/status', 10000));
  const [busy, setBusy] = useState('');

  const act = async (label: string, path: string, body: Record<string, unknown>, done: string) => {
    setBusy(label);
    try {
      const j = await postJSON<{ ok?: boolean; error?: string }>(path, body);
      if (j.ok === false) throw new Error(j.error || 'refused');
      addToast(done, undefined, 'success');
      await Promise.all([local.reload(), models.reload()]);
    } catch (e) {
      addToast(`${label} failed`, (e as Error).message, 'warning');
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Models & Offline" subtitle="Which intelligence is answering now, and what NOVA falls back to without the internet." />

      <Card title="Answering now">
        {status ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Stat label="Serving" value={status.serving === 'cloud' ? 'Cloud' : 'Local'} />
            <Stat label="Model" value={status.model || '—'} />
            <Stat label="Internet" value={status.online ? 'Online' : 'Offline'} tone={status.online ? 'ok' : 'warn'} />
            <Stat label="Brain" value={status.brain_ready ? 'Ready' : 'Starting'} tone={status.brain_ready ? 'ok' : 'warn'} />
          </div>
        ) : (
          <Loading what="status" />
        )}
      </Card>

      <Card
        title="Local intelligence (offline)"
        right={
          local.data && (
            <span className="text-[11px] font-mono" style={{ color: local.data.runtime_available ? '#10b981' : '#f59e0b' }}>
              {local.data.runtime_available ? 'Ollama running' : 'Ollama not running'}
            </span>
          )
        }
      >
        {!local.data ? (
          <Loading what="local intelligence" error={local.error} />
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={!!busy}
              onClick={() =>
                act(
                  local.data?.runtime_available ? 'Stopping' : 'Starting',
                  local.data?.runtime_available ? '/api/local-intelligence/runtime/stop' : '/api/local-intelligence/runtime/start',
                  {},
                  local.data?.runtime_available ? 'Local runtime stopped' : 'Local runtime started',
                )
              }
            >
              {local.data.runtime_available ? 'Stop local runtime' : 'Start local runtime'}
            </Button>
            <Button disabled={!!busy || !local.data.runtime_available} onClick={() => act('Testing', '/api/local-intelligence/test', {}, 'The local model answered')}>
              Test local model
            </Button>
          </div>
        )}
        {models.error && <Loading what="models" error={models.error} />}
        <div className="space-y-2">
          {(models.data?.models || []).map((m) => {
            const current = settings?.local_model === m.id;
            return (
              <div
                key={m.id}
                className="p-3 rounded-xl border flex items-center justify-between gap-3"
                style={{ borderColor: current ? theme.palette.accentBorder : theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}
              >
                <div className="min-w-0">
                  <div className="text-xs font-medium" style={{ color: theme.palette.textPrimary }}>
                    {m.name} <span className="font-mono opacity-50">{m.id}</span>
                    {current && (
                      <span className="ml-2 text-[10px] font-mono" style={{ color: theme.palette.accent }}>
                        IN USE
                      </span>
                    )}
                  </div>
                  <div className="text-[11px] opacity-60 truncate" style={{ color: theme.palette.textSecondary }}>
                    {m.size_gb} GB · needs ~{m.recommended_ram_gb} GB RAM · {m.speed} · {m.description}
                  </div>
                </div>
                {m.installed ? (
                  <Button disabled={current || !!busy} onClick={() => act('Switching', '/api/local-intelligence/set-model', { model: m.id }, `Local model: ${m.name}`)}>
                    {current ? 'In use' : 'Use'}
                  </Button>
                ) : (
                  <Button
                    disabled={!!busy || !local.data?.runtime_available}
                    title={local.data?.runtime_available ? undefined : 'Start the local runtime first'}
                    onClick={() => act('Downloading', '/api/local-intelligence/download', { model: m.id }, `Downloading ${m.name}`)}
                  >
                    Download
                  </Button>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      <Card title="Documents & knowledge">
        <Row first label="Document search" hint="How NOVA searches your documents. Auto prefers the on-device model, then the cloud, then keywords.">
          <Select<BackendSettings['embedding_backend']>
            label="Document search"
            value={settings?.embedding_backend || 'auto'}
            onChange={(v) => update('embedding_backend', v)}
            options={[
              { value: 'auto', label: 'Auto' },
              { value: 'onnx', label: 'On this device' },
              { value: 'cloud', label: 'Cloud' },
              { value: 'lexical', label: 'Keywords only' },
            ]}
          />
        </Row>
        <Row
          label="Offline knowledge"
          hint={
            knowledge.data
              ? knowledge.data.libzim_available
                ? `${knowledge.data.installed_count} of ${knowledge.data.available_count} packs installed${knowledge.data.downloading ? ` · downloading ${knowledge.data.downloading}` : ''}.`
                : 'The offline reader (libzim) is not installed, so knowledge packs cannot be read.'
              : knowledge.error || 'Loading…'
          }
        />
      </Card>

      <NotConnected
        items={[
          { label: 'Prefer online / offline fallback switches', why: 'stored, but the model router does not read them from settings yet.' },
          { label: 'Reasoning depth and context size', why: 'the models run with the backend’s fixed parameters.' },
        ]}
      />
    </div>
  );
};
