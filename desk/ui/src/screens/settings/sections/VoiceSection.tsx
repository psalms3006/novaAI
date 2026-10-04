import React from 'react';
import { getJSON, postJSON } from '../../../nova/api';
import { useRuntime } from '../../../nova/runtime';
import { useNova } from '../../../context/NovaStateContext';
import { useNovaSettings } from '../../../context/NovaSettingsContext';
import { Button, Card, Loading, NotConnected, Row, Select, SectionHeader, Stat, Toggle, useResource } from '../primitives';

interface LiveStatus {
  ok: boolean;
  state: string;
  model?: string;
  voice?: string;
  t_first_audio_ms?: number;
  last_turn_ms?: number;
  uptime_s?: number;
  turns?: number;
  mic?: { available: boolean; heard_pct?: number; barge_ins?: number; muted?: boolean };
  screen?: { watching?: boolean };
  error?: string;
}

interface AudioDevices {
  ok: boolean;
  inputs: { index: number; name: string; default: boolean }[];
  error?: string;
}

const LANGUAGES = [
  ['en-US', 'English (US)'],
  ['en-GB', 'English (UK)'],
  ['en-NG', 'English (Nigeria)'],
  ['en-IN', 'English (India)'],
  ['en-AU', 'English (Australia)'],
  ['fr-FR', 'French'],
  ['de-DE', 'German'],
  ['es-ES', 'Spanish'],
  ['pt-BR', 'Portuguese (Brazil)'],
  ['yo-NG', 'Yoruba'],
  ['ja-JP', 'Japanese'],
] as const;

export const VoiceSection: React.FC = () => {
  const { addToast } = useNova();
  const rt = useRuntime();
  const { settings, update } = useNovaSettings();
  const live = useResource(() => getJSON<LiveStatus>('/api/live/status', 8000), [rt.voiceState], 5000);
  const devices = useResource(() => getJSON<AudioDevices>('/api/audio/devices', 10000));

  if (!settings) return <Loading what="voice settings" />;

  const watching = Boolean(live.data?.screen?.watching);
  const toggleScreen = async () => {
    try {
      const j = await postJSON<{ ok: boolean; watching: boolean; reason?: string }>('/api/live/screen', { watching: !watching });
      if (!j.ok) addToast('Screen sharing unavailable', j.reason || 'The voice session is not running.', 'warning');
      live.reload();
    } catch (e) {
      addToast('Screen sharing failed', (e as Error).message, 'warning');
    }
  };

  const known = new Set(LANGUAGES.map(([v]) => v as string));
  const langOptions: { value: string; label: string }[] = LANGUAGES.map(([value, label]) => ({ value: value as string, label }));
  if (settings.voice_language && !known.has(settings.voice_language)) {
    langOptions.push({ value: settings.voice_language, label: settings.voice_language });
  }

  const micOptions = [{ value: '', label: 'System default' }, ...(devices.data?.inputs || []).map((d) => ({ value: d.name, label: d.name + (d.default ? ' (default)' : '') }))];
  if (settings.mic_device && !micOptions.some((o) => o.value === settings.mic_device)) {
    micOptions.push({ value: settings.mic_device, label: `${settings.mic_device} (not found)` });
  }

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Voice Session" subtitle="NOVA's live voice: the microphone, the language she listens for, and interrupting her." />

      <Card
        title="Session"
        right={
          <span className="text-[11px] font-mono" style={{ color: rt.voiceRunning ? '#10b981' : '#f59e0b' }}>
            {rt.phaseLabel}
          </span>
        }
      >
        <div className="flex flex-wrap gap-2">
          <Button tone="accent" onClick={rt.toggleVoice}>
            {rt.voiceRunning ? 'Stop voice' : 'Start voice'}
          </Button>
          <Button onClick={() => rt.setMuted(!rt.muted)} disabled={!rt.voiceRunning}>
            {rt.muted ? 'Unmute microphone' : 'Mute microphone'}
          </Button>
          <Button onClick={rt.interrupt} disabled={rt.phase !== 'speaking'}>
            Interrupt
          </Button>
          <Button onClick={toggleScreen} disabled={!rt.voiceRunning}>
            {watching ? 'Stop watching screen' : 'Let her watch the screen'}
          </Button>
        </div>
        {rt.voiceError && (
          <div className="text-[11px]" style={{ color: '#ef4444' }}>
            {rt.voiceError}
          </div>
        )}
        {live.data && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Stat label="Model" value={live.data.model || '—'} />
            <Stat label="Voice" value={live.data.voice || '—'} />
            <Stat label="First reply" value={live.data.t_first_audio_ms ? `${live.data.t_first_audio_ms} ms` : '—'} />
            <Stat label="Turns" value={live.data.turns ?? 0} />
            <Stat label="Mic heard" value={live.data.mic?.available ? `${live.data.mic.heard_pct ?? 0}%` : 'no mic'} tone={live.data.mic?.available ? undefined : 'bad'} />
            <Stat label="Interruptions" value={live.data.mic?.barge_ins ?? 0} />
          </div>
        )}
      </Card>

      <Card title="Listening">
        <Row first label="Start voice when NOVA opens" hint="Off: she waits until you press the microphone button.">
          <Toggle label="Start voice when NOVA opens" checked={settings.voice_enabled} onChange={(v) => update('voice_enabled', v)} />
        </Row>
        <Row label="Let me interrupt her" hint="Speaking over NOVA stops her mid-sentence. Off: she finishes what she is saying.">
          <Toggle label="Allow interruption" checked={settings.barge_in} onChange={(v) => update('barge_in', v)} />
        </Row>
        <Row label="Language" hint="What she expects you to speak. Applies from the next voice session.">
          <Select<string> label="Voice language" value={settings.voice_language || 'en-US'} options={langOptions} onChange={(v) => update('voice_language', v)} />
        </Row>
        <Row label="Microphone" hint={devices.error ? `Could not list microphones: ${devices.error}` : 'Applies from the next voice session.'}>
          <Select<string> label="Microphone" value={settings.mic_device || ''} options={micOptions} onChange={(v) => update('mic_device', v)} />
        </Row>
      </Card>

      <NotConnected
        items={[
          { label: 'Voice, speed and pitch', why: 'the voice session uses one fixed voice chosen by the backend.' },
          { label: 'Wake word', why: 'NOVA listens continuously while voice is on; there is no wake-word detector.' },
          { label: 'Spoken replies off', why: 'the live session always answers aloud; the stored setting is not read yet.' },
        ]}
      />
    </div>
  );
};
