import React, { useEffect, useRef, useState } from 'react';
import { OnboardingLayout, OnboardingButton } from './OnboardingLayout';
import { 
  cancelOfflineDownload, 
  completeSetup, 
  formatBytes, 
  getOfflineModel, 
  startOfflineDownload, 
  type OfflineModelInfo, 
  type OfflineState, 
  type SetupStep 
} from '../nova/lifecycle';
import { useNova } from '../context/NovaStateContext';
import { SuccessStep } from './SuccessStep';

type SubStep = 'offline' | 'config';

const PERMISSIONS = [
  { id: 'microphone', label: 'Microphone', icon: 'fa-microphone', desc: 'Natural voice conversations' },
  { id: 'screen_read', label: 'Screen', icon: 'fa-eye', desc: 'Understand what is on your screen' },
  { id: 'file_read', label: 'Read files', icon: 'fa-folder-open', desc: 'Search and read local files' },
  { id: 'browser_read', label: 'Web Access', icon: 'fa-globe', desc: 'Research and browse the web' },
];

const STARTUP = [
  { id: 'open', label: 'Start with Windows', icon: 'fa-window-restore' },
  { id: 'manual', label: 'Manual Launch', icon: 'fa-hand-pointer' },
];

export const DeviceSetup: React.FC<{ onDone: () => void }> = ({ onDone }) => {
  const { theme } = useNova();
  const [subStep, setSubStep] = useState<SubStep>('offline');
  const [info, setInfo] = useState<OfflineModelInfo | null>(null);
  const [st, setSt] = useState<OfflineState | null>(null);
  const [perms, setPerms] = useState<Record<string, boolean>>({ microphone: true, screen_read: false, file_read: false, browser_read: true });
  const [startup, setStartup] = useState('manual');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SetupStep[] | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    getOfflineModel().then(setInfo).catch(() => undefined);
    return () => window.clearInterval(timer.current);
  }, []);

  const polling = st?.state === 'downloading' || st?.state === 'verifying';
  useEffect(() => {
    window.clearInterval(timer.current);
    if (!polling) return;
    timer.current = window.setInterval(async () => {
      try {
        const j = await getOfflineModel();
        setSt(j.state);
        if (j.state.state === 'ready' || j.state.state === 'failed') {
          window.clearInterval(timer.current);
          setSubStep('config');
        }
      } catch { /* ignore */ }
    }, 1000);
    return () => window.clearInterval(timer.current);
  }, [polling]);

  const startDownload = async () => {
    const rec = info?.models.find(m => m.id === info.recommended) || info?.models[0];
    if (!rec) return setSubStep('config');
    try {
      const j = await startOfflineDownload(rec.id);
      setSt(j.state);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const finish = async () => {
    setBusy(true);
    setError(null);
    try {
      const permissions = Object.fromEntries(Object.entries(perms).map(([id, on]) => [id, on ? 'allow' : 'ask']));
      const j = await completeSetup({ permissions, startup_mode: startup, offline_model: st?.state === 'ready' ? 'ready' : 'skipped' });
      setResult(j.steps);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (result) return <SuccessStep steps={result} onDone={onDone} />;

  if (subStep === 'offline') {
    const pct = st?.percent ?? 0;
    const downloading = st?.state === 'downloading' || st?.state === 'verifying';

    return (
      <OnboardingLayout 
        title="Offline Intelligence" 
        subtitle="Download a local AI model so NOVA can keep helping even without internet."
      >
        <div className="space-y-6 pt-2">
          {downloading ? (
            <div className="space-y-4">
              <div className="space-y-2">
                <div className="flex justify-between text-xs font-medium" style={{ color: theme.palette.textPrimary }}>
                  <span>{st?.state === 'verifying' ? 'Verifying...' : 'Downloading model...'}</span>
                  <span>{pct.toFixed(0)}%</span>
                </div>
                <div className="h-2.5 rounded-full overflow-hidden" style={{ backgroundColor: theme.palette.glassSurface }}>
                  <div className="h-full transition-all duration-500 ease-out" style={{ width: `${pct}%`, backgroundColor: theme.palette.accent }} />
                </div>
              </div>
              <p className="text-[11px] text-center opacity-60" style={{ color: theme.palette.textSecondary }}>
                {st?.total_bytes ? `${formatBytes(st.completed_bytes)} / ${formatBytes(st.total_bytes)}` : 'Preparing download...'}
              </p>
              <button 
                className="w-full py-2 text-xs opacity-50 hover:opacity-100 transition-opacity" 
                onClick={async () => { await cancelOfflineDownload(); setSubStep('config'); }}
              >
                Cancel download
              </button>
            </div>
          ) : (
            <>
              <div className="p-5 rounded-[24px] border space-y-3" style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}>
                <div className="flex items-center gap-3">
                  <i className="fa-solid fa-microchip text-lg" style={{ color: theme.palette.accent }} />
                  <div className="text-xs font-semibold" style={{ color: theme.palette.textPrimary }}>Recommended for this PC</div>
                </div>
                <p className="text-[11px] opacity-70 leading-relaxed" style={{ color: theme.palette.textSecondary }}>
                  Llama 3 (8B) — Balanced for speed and intelligence. Requires ~5GB disk space.
                </p>
              </div>
              <div className="flex gap-3">
                <OnboardingButton variant="secondary" onClick={() => setSubStep('config')} className="flex-1">
                  Skip
                </OnboardingButton>
                <OnboardingButton onClick={startDownload} className="flex-[2]">
                  Download & Continue
                </OnboardingButton>
              </div>
            </>
          )}
        </div>
      </OnboardingLayout>
    );
  }

  return (
    <OnboardingLayout 
      title="Hardware & Access" 
      subtitle="Final touches to ensure NOVA works seamlessly on your computer."
    >
      <div className="space-y-6">
        <div className="space-y-3">
          <label className="text-xs font-semibold opacity-70 ml-1" style={{ color: theme.palette.textPrimary }}>Permissions</label>
          <div className="space-y-2">
            {PERMISSIONS.map(p => {
              const active = perms[p.id];
              return (
                <button
                  key={p.id}
                  onClick={() => setPerms({ ...perms, [p.id]: !active })}
                  className="w-full flex items-center gap-4 p-3.5 rounded-2xl border text-left transition-all duration-200 cursor-pointer"
                  style={{ borderColor: active ? theme.palette.accent : theme.palette.glassBorder, backgroundColor: active ? `${theme.palette.accent}05` : 'transparent' }}
                >
                  <div className="w-8 h-8 rounded-full flex items-center justify-center" style={{ backgroundColor: active ? `${theme.palette.accent}15` : theme.palette.glassSurface }}>
                    <i className={`fa-solid ${p.icon} text-[11px]`} style={{ color: active ? theme.palette.accent : theme.palette.textMuted }} />
                  </div>
                  <div className="flex-1">
                    <div className="text-[11px] font-semibold" style={{ color: theme.palette.textPrimary }}>{p.label}</div>
                    <div className="text-[10px] opacity-50" style={{ color: theme.palette.textSecondary }}>{p.desc}</div>
                  </div>
                  <div className={`text-[10px] font-bold ${active ? 'opacity-100' : 'opacity-30'}`} style={{ color: active ? theme.palette.accent : theme.palette.textPrimary }}>
                    {active ? 'ALLOW' : 'ASK'}
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        <div className="space-y-3">
          <label className="text-xs font-semibold opacity-70 ml-1" style={{ color: theme.palette.textPrimary }}>Startup Behavior</label>
          <div className="grid grid-cols-2 gap-3">
            {STARTUP.map(s => {
              const active = startup === s.id;
              return (
                <button
                  key={s.id}
                  onClick={() => setStartup(s.id)}
                  className="flex flex-col items-center gap-2 p-4 rounded-2xl border transition-all duration-200 cursor-pointer"
                  style={{ 
                    borderColor: active ? theme.palette.accent : theme.palette.glassBorder, 
                    backgroundColor: active ? `${theme.palette.accent}05` : 'transparent',
                    boxShadow: active ? `0 0 0 1px ${theme.palette.accent}` : 'none'
                  }}
                >
                  <i className={`fa-solid ${s.icon} text-sm`} style={{ color: active ? theme.palette.accent : theme.palette.textMuted }} />
                  <span className="text-[11px] font-medium" style={{ color: active ? theme.palette.textPrimary : theme.palette.textSecondary }}>{s.label}</span>
                </button>
              );
            })}
          </div>
        </div>

        {error && <p className="text-[11px] text-red-500">{error}</p>}

        <OnboardingButton onClick={finish} disabled={busy}>
          {busy ? 'Configuring...' : 'Finish Setup'}
        </OnboardingButton>
      </div>
    </OnboardingLayout>
  );
};
