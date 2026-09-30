import React from 'react';
import { useNova } from '../../../context/NovaStateContext';
import { useNovaSettings } from '../../../context/NovaSettingsContext';
import type { ScreenMode } from '../../../types/nova';
import { getJSON, postJSON } from '../../../nova/api';
import { Button, Card, Loading, Row, Select, SectionHeader, TextField, useResource } from '../primitives';

type StartupMode = 'manual' | 'open' | 'background';

/** Start with Windows: written to the per-user Run key by the backend and
 * read back from Windows, so what this shows is what Windows will do. */
const StartupCard: React.FC = () => {
  const { addToast } = useNova();
  const st = useResource(() => getJSON<{ mode: StartupMode; registered: string }>('/api/startup', 10000));
  const [busy, setBusy] = React.useState(false);
  const change = async (mode: StartupMode) => {
    setBusy(true);
    try {
      await postJSON('/api/startup', { mode });
      addToast(mode === 'manual' ? 'NOVA will not start with Windows' : 'NOVA will start with Windows', undefined, 'success');
    } catch (e) {
      addToast('Could not change startup', (e as Error).message, 'warning');
    } finally {
      setBusy(false);
      st.reload();
    }
  };
  return (
    <Card title="Startup">
      {!st.data ? (
        <Loading what="startup" error={st.error} />
      ) : (
        <Row first label="When Windows starts" hint={st.data.registered ? 'Registered with Windows for your account.' : 'Not registered: you open NOVA yourself.'}>
          <Select<StartupMode>
            label="When Windows starts"
            value={st.data.mode}
            disabled={busy}
            onChange={change}
            options={[
              { value: 'manual', label: 'Don’t start NOVA' },
              { value: 'open', label: 'Start NOVA and open her window' },
              { value: 'background', label: 'Start NOVA quietly (orb only)' },
            ]}
          />
        </Row>
      )}
    </Card>
  );
};

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
              { value: 'library', label: 'Library' },
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

      <StartupCard />

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
