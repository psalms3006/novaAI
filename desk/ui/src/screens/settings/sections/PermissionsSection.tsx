import React from 'react';
import { getJSON } from '../../../nova/api';
import { useRuntime } from '../../../nova/runtime';
import { useNova } from '../../../context/NovaStateContext';
import { useNovaSettings } from '../../../context/NovaSettingsContext';
import type { PermissionValue } from '../../../types/settings';
import { Button, Card, Loading, NotConnected, Row, SectionHeader, useResource } from '../primitives';

const CATEGORY: Record<string, { label: string; what: string; icon: string }> = {
  web: { label: 'Search the web', what: 'Web searches and reading pages.', icon: 'fa-globe' },
  network: { label: 'Use the network', what: 'Other outbound connections, such as downloads.', icon: 'fa-network-wired' },
  screen: { label: 'See your screen', what: 'Screenshots and looking at what is on screen.', icon: 'fa-eye' },
  mic: { label: 'Use the microphone', what: 'Listening outside the voice session.', icon: 'fa-microphone' },
  files: { label: 'Work with files', what: 'Reading, writing, moving and deleting files.', icon: 'fa-folder-tree' },
  computer: { label: 'Control the computer', what: 'Opening and closing apps, system settings, automation.', icon: 'fa-laptop-code' },
  exec: { label: 'Run commands', what: 'Running programs, scripts and code.', icon: 'fa-terminal' },
};

const CHOICES: { value: PermissionValue; label: string }[] = [
  { value: 'allow', label: 'Allow' },
  { value: 'ask', label: 'Ask me' },
  { value: 'deny', label: 'Never' },
];

export const PermissionsSection: React.FC = () => {
  const { theme, addToast } = useNova();
  const rt = useRuntime();
  const { settings, setPermission } = useNovaSettings();
  const perms = useResource(() => getJSON<{ categories: string[]; tool_map: Record<string, string> }>('/api/permissions', 8000));

  if (!settings || !perms.data) return <Loading what="permissions" error={perms.error} />;

  const toolsFor = (cat: string) =>
    Object.entries(perms.data?.tool_map || {})
      .filter(([, c]) => c === cat)
      .map(([t]) => t);

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Permissions" subtitle="What NOVA may do on her own. “Ask me” pauses her until you answer the prompt in this window." />

      <Card title="Stop">
        <Row
          first
          label="Stop NOVA now"
          hint="Stops her speaking and any reply being written, and mutes the microphone. Background tasks already running are not cancelled — the backend cannot yet."
        >
          <Button
            tone="danger"
            onClick={async () => {
              await rt.halt();
              addToast('NOVA stopped', 'Speech and replies stopped; microphone muted.', 'warning');
            }}
          >
            <i className="fa-solid fa-hand mr-1.5" />
            Stop
          </Button>
        </Row>
        {rt.pendingConfirm && (
          <Row label="Waiting for you" hint={rt.pendingConfirm.prompt}>
            <div className="flex gap-2">
              <Button onClick={() => rt.decide(rt.pendingConfirm!.id, false)}>No</Button>
              <Button tone="accent" onClick={() => rt.decide(rt.pendingConfirm!.id, true)}>
                Yes
              </Button>
            </div>
          </Row>
        )}
      </Card>

      <Card title="What she may do">
        <div className="space-y-3">
          {perms.data.categories.map((cat, i) => {
            const meta = CATEGORY[cat] || { label: cat, what: '', icon: 'fa-circle' };
            const value = (settings.permissions?.[cat] || 'ask') as PermissionValue;
            const tools = toolsFor(cat);
            return (
              <div key={cat} className={`flex items-center justify-between gap-4 ${i ? 'pt-3 border-t' : ''}`} style={{ borderColor: theme.palette.glassBorder }}>
                <div className="min-w-0 flex items-start gap-2.5">
                  <i className={`fa-solid ${meta.icon} text-xs mt-0.5 w-4 text-center opacity-70`} style={{ color: theme.palette.accent }} />
                  <div className="min-w-0">
                    <div className="text-xs font-medium" style={{ color: theme.palette.textPrimary }}>
                      {meta.label}
                    </div>
                    <div className="text-[11px] opacity-60" style={{ color: theme.palette.textSecondary }}>
                      {meta.what}
                      {tools.length > 0 && <span className="font-mono opacity-70"> · {tools.slice(0, 5).join(', ')}{tools.length > 5 ? '…' : ''}</span>}
                    </div>
                  </div>
                </div>
                <div className="flex rounded-xl border overflow-hidden shrink-0" style={{ borderColor: theme.palette.glassBorder }} role="radiogroup" aria-label={meta.label}>
                  {CHOICES.map((c) => {
                    const on = value === c.value;
                    return (
                      <button
                        key={c.value}
                        role="radio"
                        aria-checked={on}
                        onClick={() => setPermission(cat, c.value)}
                        className="px-2.5 py-1.5 text-[11px] cursor-pointer transition-all"
                        style={{
                          backgroundColor: on ? (c.value === 'deny' ? 'rgba(239,68,68,0.15)' : theme.palette.accent) : 'transparent',
                          color: on ? (c.value === 'deny' ? '#ef4444' : theme.isDark ? theme.palette.bgBase : '#ffffff') : theme.palette.textSecondary,
                        }}
                      >
                        {c.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      <NotConnected
        items={[
          { label: 'Camera, messages and purchases', why: 'NOVA has no tools in these categories, so there is nothing to permit.' },
          { label: '“Auto-approve safe actions” policy', why: 'stored, but the safety gate uses only the per-category choices above.' },
        ]}
      />
    </div>
  );
};
