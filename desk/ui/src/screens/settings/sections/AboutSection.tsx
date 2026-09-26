import React from 'react';
import { VERSION } from '../../../nova/api';
import { useRuntime } from '../../../nova/runtime';
import { useNova } from '../../../context/NovaStateContext';
import { Card, Row, SectionHeader } from '../primitives';

export const AboutSection: React.FC = () => {
  const { theme } = useNova();
  const { status } = useRuntime();

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="About NOVA" subtitle="A voice-first assistant for your computer, by Omniel." />

      <Card>
        <div className="flex items-center gap-4">
          <div
            className="w-12 h-12 rounded-2xl border flex items-center justify-center"
            style={{ borderColor: theme.palette.accentBorder, backgroundColor: theme.palette.glassSurface }}
          >
            <span className="w-3 h-3 rounded-full" style={{ backgroundColor: theme.palette.accent, boxShadow: `0 0 18px ${theme.palette.accent}` }} />
          </div>
          <div>
            <div className="text-sm font-semibold tracking-[0.18em]" style={{ color: theme.palette.textPrimary }}>
              NOVA
            </div>
            <div className="text-[11px] font-mono opacity-60" style={{ color: theme.palette.textSecondary }}>
              version {status?.version || VERSION}
            </div>
          </div>
        </div>
        <Row label="Interface" hint="The desktop interface in desk/ui, served by NOVA's own backend.">
          <span className="text-xs font-mono" style={{ color: theme.palette.textPrimary }}>
            2.0
          </span>
        </Row>
        <Row label="Voice engine">
          <span className="text-xs font-mono" style={{ color: theme.palette.textPrimary }}>
            Gemini Live
          </span>
        </Row>
        <Row label="Offline intelligence">
          <span className="text-xs font-mono" style={{ color: theme.palette.textPrimary }}>
            {status?.local_intelligence?.model || 'Ollama'}
          </span>
        </Row>
      </Card>

      <div className="text-[10px] font-mono opacity-50" style={{ color: theme.palette.textMuted }}>
        © Omniel. Updates are delivered with new installers; NOVA does not check for updates on its own.
      </div>
    </div>
  );
};
