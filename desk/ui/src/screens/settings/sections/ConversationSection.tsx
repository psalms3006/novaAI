import React from 'react';
import { useNovaSettings } from '../../../context/NovaSettingsContext';
import type { BackendSettings } from '../../../types/settings';
import { Card, Loading, NotConnected, Row, Select, SectionHeader, Toggle } from '../primitives';

export const ConversationSection: React.FC = () => {
  const { settings, update } = useNovaSettings();
  if (!settings) return <Loading what="conversation settings" />;

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Conversation" subtitle="How NOVA answers what you type, and how much of the conversation she is given." />

      <Card title="Answers">
        <Row first label="Response style" hint="Enters her instructions for every typed turn.">
          <Select<BackendSettings['response_style']>
            label="Response style"
            value={settings.response_style}
            onChange={(v) => update('response_style', v)}
            options={[
              { value: 'concise', label: 'Concise' },
              { value: 'balanced', label: 'Balanced' },
              { value: 'detailed', label: 'Detailed' },
            ]}
          />
        </Row>
        <Row label="Stream replies" hint="Show the answer word by word as it is written, instead of all at once.">
          <Toggle label="Stream replies" checked={settings.streaming} onChange={(v) => update('streaming', v)} />
        </Row>
        <Row label="Context sent" hint="How many earlier turns of a typed conversation go with each new message.">
          <Select<string>
            label="History turns"
            value={String(settings.history_turns)}
            onChange={(v) => update('history_turns', Number(v))}
            options={['4', '6', '10', '16', '24'].map((n) => ({ value: n, label: `${n} turns` }))}
          />
        </Row>
      </Card>

      <NotConnected
        items={[
          { label: 'Continuous conversation', why: 'stored and reported, but the voice session already listens continuously and does not read it.' },
          { label: 'Proactivity level', why: 'NOVA has no proactivity control yet.' },
        ]}
      />
    </div>
  );
};
