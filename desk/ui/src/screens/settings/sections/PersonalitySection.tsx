import React from 'react';
import { useNova } from '../../../context/NovaStateContext';
import { useNovaSettings } from '../../../context/NovaSettingsContext';
import { Card, Loading, NotConnected, Row, SectionHeader, TextField } from '../primitives';

const LIMIT = 4000;

export const PersonalitySection: React.FC = () => {
  const { theme } = useNova();
  const { settings, update, saveState } = useNovaSettings();
  if (!settings) return <Loading what="your instructions" />;

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Custom Instructions" subtitle="Your own words to NOVA. They are added to her instructions for every typed turn." />

      <Card
        title="Instructions"
        right={
          <span className="text-[10px] font-mono opacity-60" style={{ color: theme.palette.textMuted }}>
            {settings.user_system_prompt.length}/{LIMIT} · {saveState === 'saving' ? 'saving' : saveState === 'error' ? 'not saved' : 'saved'}
          </span>
        }
      >
        <Row first label="How should NOVA behave?" hint="For example: “Keep answers short. Use metric units. Call out risks before acting.”" />
        <TextField
          multiline
          label="Custom instructions"
          value={settings.user_system_prompt}
          maxLength={LIMIT}
          placeholder="Nothing yet"
          onChange={(v) => update('user_system_prompt', v)}
        />
      </Card>

      <NotConnected
        items={[
          { label: 'Personality sliders and presets', why: 'NOVA has no numeric personality model; write the behaviour you want above instead.' },
        ]}
      />
    </div>
  );
};
