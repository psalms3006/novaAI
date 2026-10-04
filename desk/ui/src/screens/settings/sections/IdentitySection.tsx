import React, { useEffect, useState } from 'react';
import { postJSON } from '../../../nova/api';
import { useNova } from '../../../context/NovaStateContext';
import { useNovaSettings } from '../../../context/NovaSettingsContext';
import { Button, Card, Loading, Row, SectionHeader, TextField } from '../primitives';

export const IdentitySection: React.FC = () => {
  const { addToast } = useNova();
  const { settings, update, reload } = useNovaSettings();
  const [name, setName] = useState('');
  const [say, setSay] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!settings) return;
    setName(settings.user_name || '');
    setSay(settings.user_name_pronunciation || '');
  }, [settings?.user_name, settings?.user_name_pronunciation]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!settings) return <Loading what="your profile" />;

  const dirty = name !== settings.user_name || say !== settings.user_name_pronunciation;

  // The profile route, not a plain setting: it also tells the running brain,
  // so NOVA uses the new name from her next sentence.
  const saveProfile = async () => {
    setSaving(true);
    try {
      await postJSON('/api/settings/profile', { user_name: name.trim(), user_name_pronunciation: say.trim() });
      await reload();
      addToast('Profile saved', `NOVA will call you ${name.trim() || 'by your name'}.`, 'success');
    } catch (e) {
      addToast('Could not save your profile', (e as Error).message, 'warning');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Identity" subtitle="Who you are to NOVA. She is told this directly, not left to guess it from memory." />

      <Card title="Your name">
        <Row first label="Name" hint="What NOVA calls you." />
        <TextField label="Name" value={name} maxLength={80} onChange={setName} placeholder="Your name" />
        <Row label="How to say it" hint="Only if the spelling does not tell her, e.g. “SAHMZ”." />
        <TextField label="Pronunciation" value={say} maxLength={80} onChange={setSay} placeholder="Optional" />
        <div className="flex justify-end">
          <Button tone="accent" onClick={saveProfile} disabled={!dirty || saving || !name.trim()}>
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </Card>

      <Card title="Who you are to her">
        <Row first label="Your role" hint="In your own words — “the person who built me”, “my colleague”. Reaches the voice model verbatim." />
        <TextField label="Your role" value={settings.user_role} maxLength={200} onChange={(v) => update('user_role', v)} placeholder="Optional" />
      </Card>
    </div>
  );
};
