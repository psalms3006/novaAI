import React from 'react';
import { useNova } from '../../../context/NovaStateContext';
import { useNovaSettings } from '../../../context/NovaSettingsContext';
import type { ScreenMode } from '../../../types/nova';
import { Button, Card, NotConnected, Row, Select, SectionHeader, TextField } from '../primitives';

export const GeneralSection: React.FC = () => {
  const { prefs, setPref, resetPrefs, addToast } = useNova();
  const { settings, update } = useNovaSettings();

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="General" subtitle="Where NOVA opens, how the clock reads, and the folder she works in." />

      <Card title="Window">
        <Row first label="Start screen" hint="The screen NOVA opens on.">
          <Select<ScreenMode>
            label="Start screen"
            value={prefs.landing_view}
            onChange={(v) => setPref('landing_view', v)}
            options={[
              { value: 'substrate', label: 'Presence' },
              { value: 'synaptic', label: 'Memory map' },
              { value: 'runtime', label: 'Settings' },
            ]}
          />
        </Row>
        <Row label="Clock" hint="The top bar clock and notification times.">
          <Select<'12h' | '24h'>
            label="Clock format"
            value={prefs.time_format}
            onChange={(v) => setPref('time_format', v)}
            options={[
              { value: '12h', label: '12-hour' },
              { value: '24h', label: '24-hour' },
            ]}
          />
        </Row>
      </Card>

      <Card title="Workspace">
        <Row first label="Workspace folder" hint="Where NOVA creates and saves files. Leave empty for the default folder." />
        <TextField
          label="Workspace folder"
          value={settings?.workspace_dir ?? ''}
          placeholder="Default workspace"
          onChange={(v) => update('workspace_dir', v)}
        />
      </Card>

      <NotConnected
        items={[
          { label: 'Launch at startup', why: 'the setting is stored, but nothing registers NOVA with Windows startup yet.' },
          { label: 'Start minimized', why: 'the desktop launcher does not read this setting yet.' },
        ]}
      />

      <Card title="Reset">
        <Row first label="Reset interface preferences" hint="Theme, glass, motion and start screen go back to their defaults. Memory, settings and files are untouched.">
          <Button
            onClick={() => {
              resetPrefs();
              addToast('Interface preferences reset', 'Theme, glass and motion are back to their defaults.', 'success');
            }}
          >
            Reset
          </Button>
        </Row>
      </Card>
    </div>
  );
};
