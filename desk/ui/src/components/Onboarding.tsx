import React, { useEffect, useState } from 'react';
import { postJSON } from '../nova/api';
import { shouldShowOnboarding } from '../nova/onboarding';
import { useRuntime } from '../nova/runtime';
import { useNova } from '../context/NovaStateContext';

type Choice = 'byok' | 'cloud' | 'offline';

/**
 * First run: what to call the user, and how NOVA reaches her intelligence --
 * the user's own Gemini key (sealed with Windows DPAPI), a NOVA Cloud server,
 * or offline only. Shown once. The latch means nothing -- a slow status, a
 * refresh -- can put it back up after the user has chosen.
 */
export const Onboarding: React.FC = () => {
  const { theme, addToast } = useNova();
  const rt = useRuntime();
  const [done, setDone] = useState(false);
  const [name, setName] = useState('');
  const [say, setSay] = useState('');
  const [choice, setChoice] = useState<Choice>('byok');
  const [key, setKey] = useState('');
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const auth = rt.status?.auth;
  // Latch closed as soon as the backend says this person is set up.
  useEffect(() => {
    if (auth && (auth.onboarded === true || auth.has_credential === true)) setDone(true);
  }, [auth]);

  if (done || !shouldShowOnboarding(rt.status)) return null;

  const finish = async () => {
    setError('');
    if (choice === 'byok' && !key.trim()) return setError('Paste your Gemini API key first.');
    if (choice === 'cloud' && !url.trim()) return setError('Enter your NOVA server address.');
    setBusy(true);
    try {
      if (name.trim()) {
        await postJSON('/api/settings/profile', { user_name: name.trim(), user_name_pronunciation: say.trim() });
      }
      const path = choice === 'byok' ? '/api/onboarding/byok' : choice === 'cloud' ? '/api/onboarding/cloud' : '/api/onboarding/offline';
      const body = choice === 'byok' ? { api_key: key.trim() } : choice === 'cloud' ? { url: url.trim() } : {};
      const j = await postJSON<{ ok: boolean; error?: string }>(path, body);
      if (!j.ok) throw new Error(j.error || 'The backend refused it');
      setKey('');
      setDone(true);
      addToast(
        choice === 'offline' ? 'Offline mode' : choice === 'cloud' ? 'Connected to NOVA Cloud' : 'Key saved',
        choice === 'byok' ? 'Sealed on this computer; it never leaves it except to reach Gemini.' : undefined,
        'success',
      );
      await rt.refreshStatus();
      if (choice !== 'offline') rt.startVoice();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const card = (id: Choice, icon: string, title: string, desc: string) => {
    const on = choice === id;
    return (
      <button
        type="button"
        onClick={() => {
          setChoice(id);
          setError('');
        }}
        className="p-3.5 rounded-2xl border text-left transition-all cursor-pointer"
        style={{
          backgroundColor: on ? theme.palette.glassSurface : 'transparent',
          borderColor: on ? theme.palette.accent : theme.palette.glassBorder,
          boxShadow: on ? `0 0 0 1px ${theme.palette.accent}` : undefined,
        }}
        aria-pressed={on}
      >
        <i className={`fa-solid ${icon} text-sm`} style={{ color: theme.palette.accent }} />
        <div className="text-xs font-medium mt-2" style={{ color: theme.palette.textPrimary }}>
          {title}
        </div>
        <div className="text-[11px] mt-0.5 leading-relaxed opacity-70" style={{ color: theme.palette.textSecondary }}>
          {desc}
        </div>
      </button>
    );
  };

  const field = { borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface };

  return (
    <div className="fixed inset-0 z-[55] flex items-center justify-center p-4 backdrop-blur-xl animate-fade-in" style={{ backgroundColor: 'rgba(0,0,0,0.5)' }}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboard-title"
        className="w-full max-w-2xl p-6 rounded-3xl border shadow-2xl space-y-5"
        style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder, boxShadow: `0 30px 70px -20px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}` }}
      >
        <div>
          <h2 id="onboard-title" className="text-lg font-semibold tracking-tight" style={{ color: theme.palette.textPrimary }}>
            Welcome to NOVA
          </h2>
          <p className="text-xs mt-1" style={{ color: theme.palette.textSecondary }}>
            Two quick things and she's ready: what to call you, and how she should think.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="text-[11px] font-medium" style={{ color: theme.palette.textPrimary }}>
              Your name
            </span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} placeholder="Leave blank and she'll ask" className="mt-1 w-full text-xs p-2.5 rounded-xl border outline-none select-text" style={field} autoFocus />
          </label>
          <label className="block">
            <span className="text-[11px] font-medium" style={{ color: theme.palette.textPrimary }}>
              How it's said <span className="opacity-50">(optional)</span>
            </span>
            <input value={say} onChange={(e) => setSay(e.target.value)} maxLength={80} placeholder="e.g. AY-duh" className="mt-1 w-full text-xs p-2.5 rounded-xl border outline-none select-text" style={field} />
          </label>
        </div>

        <div className="grid grid-cols-3 gap-3">
          {card('byok', 'fa-key', 'My own Gemini key', 'Voice, vision and the best answers. Sealed on this computer.')}
          {card('cloud', 'fa-cloud', 'NOVA Cloud', 'Sign in through a NOVA server; no key for you to manage.')}
          {card('offline', 'fa-plane', 'Offline only', 'Local intelligence only. No voice session, nothing leaves this computer.')}
        </div>

        {choice === 'byok' && (
          <label className="block">
            <span className="text-[11px] font-medium" style={{ color: theme.palette.textPrimary }}>
              Gemini API key
            </span>
            <input
              type="password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder="Paste your key"
              onKeyDown={(e) => e.key === 'Enter' && finish()}
              className="mt-1 w-full text-xs p-2.5 rounded-xl border outline-none font-mono select-text"
              style={field}
            />
            <span className="text-[10px] mt-1 block" style={{ color: theme.palette.textMuted }}>
              Get one free at aistudio.google.com. You can change or remove it later in Settings → Account &amp; Access.
            </span>
          </label>
        )}
        {choice === 'cloud' && (
          <label className="block">
            <span className="text-[11px] font-medium" style={{ color: theme.palette.textPrimary }}>
              NOVA server address
            </span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://nova.example.com" onKeyDown={(e) => e.key === 'Enter' && finish()} className="mt-1 w-full text-xs p-2.5 rounded-xl border outline-none select-text" style={field} />
          </label>
        )}

        {error && (
          <div role="alert" className="text-[11px]" style={{ color: '#ef4444' }}>
            {error}
          </div>
        )}

        <div className="flex justify-end">
          <button
            onClick={finish}
            disabled={busy}
            className="px-5 py-2 rounded-xl border text-xs font-medium cursor-pointer disabled:opacity-50"
            style={{ backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#ffffff', borderColor: theme.palette.accentBorder }}
          >
            {busy ? 'Setting up…' : choice === 'offline' ? 'Continue offline' : 'Continue'}
          </button>
        </div>
      </div>
    </div>
  );
};
