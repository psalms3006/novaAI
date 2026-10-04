import React from 'react';
import { QUALITY, useNova, type GlassQuality, type UiPrefs } from '../../../context/NovaStateContext';
import { NOVA_THEMES } from '../../../theme/themes';
import type { ThemeId } from '../../../types/nova';
import { Card, Row, Select, SectionHeader, Toggle } from '../primitives';

const Slider: React.FC<{ label: string; min: number; max: number; value: number; unit: string; onChange: (v: number) => void }> = ({
  label,
  min,
  max,
  value,
  unit,
  onChange,
}) => {
  const { theme } = useNova();
  return (
    <div className="pt-3 border-t" style={{ borderColor: theme.palette.glassBorder }}>
      <div className="flex justify-between text-xs mb-2">
        <span className="font-medium" style={{ color: theme.palette.textPrimary }}>
          {label}
        </span>
        <span className="font-mono opacity-70" style={{ color: theme.palette.accent }}>
          {value}
          {unit}
        </span>
      </div>
      <input
        aria-label={label}
        type="range"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full cursor-pointer"
        style={{ accentColor: theme.palette.accent }}
      />
    </div>
  );
};

const QUALITY_TEXT: Record<GlassQuality, string> = {
  quality: 'Full blur, 12,000-particle orb at up to 2× resolution.',
  balanced: 'Lighter blur and 7,000 particles. The default on 4-core machines.',
  performance: 'Minimal blur, 3,500 particles at 30 fps. For weak GPUs and battery.',
};

export const AppearanceSection: React.FC = () => {
  const { theme, prefs, setPref } = useNova();
  const set = <K extends keyof UiPrefs>(k: K) => (v: UiPrefs[K]) => setPref(k, v);

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Appearance" subtitle="Every change here applies at once and is saved with NOVA's settings." />

      <Card title="Theme">
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          {(Object.keys(NOVA_THEMES) as ThemeId[]).map((id) => {
            const t = NOVA_THEMES[id];
            const active = prefs.theme_id === id;
            return (
              <button
                key={id}
                onClick={() => setPref('theme_id', id)}
                className="p-3 rounded-2xl border text-left transition-all cursor-pointer hover:scale-[1.01]"
                style={{
                  backgroundColor: t.palette.bgElevated,
                  borderColor: active ? theme.palette.accent : t.palette.glassBorder,
                  boxShadow: active ? `0 0 0 1px ${theme.palette.accent}` : undefined,
                }}
                aria-pressed={active}
              >
                <div className="flex gap-1.5 mb-2">
                  {[t.palette.bgBase, t.palette.accent, t.palette.textPrimary].map((c, i) => (
                    <span key={i} className="w-3 h-3 rounded-full border" style={{ backgroundColor: c, borderColor: t.palette.glassBorder }} />
                  ))}
                </div>
                <div className="text-xs font-medium" style={{ color: t.palette.textPrimary }}>
                  {t.name}
                </div>
                <div className="text-[10px] opacity-60 mt-0.5 truncate" style={{ color: t.palette.textSecondary }}>
                  {t.tagline}
                </div>
              </button>
            );
          })}
        </div>
      </Card>

      <Card title="Liquid glass">
        <Slider label="Glass opacity" min={30} max={95} unit="%" value={prefs.glass_opacity} onChange={set('glass_opacity')} />
        <Slider label="Backdrop blur" min={8} max={40} unit="px" value={prefs.glass_blur} onChange={set('glass_blur')} />
        <Slider label="Specular rim" min={0} max={100} unit="%" value={prefs.specular} onChange={set('specular')} />
        <Row label="Corners">
          <Select<UiPrefs['corner_radius']>
            label="Corner radius"
            value={prefs.corner_radius}
            onChange={set('corner_radius')}
            options={[
              { value: 'sharp', label: 'Sharp' },
              { value: 'soft', label: 'Soft' },
              { value: 'round', label: 'Round' },
            ]}
          />
        </Row>
        <Row label="Interface scale">
          <Select<UiPrefs['ui_scale']>
            label="Interface scale"
            value={prefs.ui_scale}
            onChange={set('ui_scale')}
            options={[
              { value: '90', label: '90%' },
              { value: '100', label: '100%' },
              { value: '110', label: '110%' },
            ]}
          />
        </Row>
      </Card>

      <Card title="Motion & rendering">
        <Row first label="Ambient motion" hint="Floating panels, glows and the orb's idle drift. Off also slows the orb to a still frame when nothing is happening.">
          <Toggle label="Ambient motion" checked={prefs.ambient_motion} onChange={set('ambient_motion')} />
        </Row>
        <Row label="Rendering quality" hint={QUALITY_TEXT[prefs.quality]}>
          <Select<GlassQuality>
            label="Rendering quality"
            value={prefs.quality}
            onChange={set('quality')}
            options={(Object.keys(QUALITY) as GlassQuality[]).map((q) => ({ value: q, label: q[0].toUpperCase() + q.slice(1) }))}
          />
        </Row>
        <Row label="Show conversation" hint="The transcript of what you and NOVA said, beside the orb.">
          <Toggle label="Show conversation" checked={prefs.show_transcript} onChange={set('show_transcript')} />
        </Row>
      </Card>
    </div>
  );
};
