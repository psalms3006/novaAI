import React, { useCallback, useEffect, useRef, useState } from 'react';
import { accountCall, type AccountState } from '../nova/account';
import {
  cancelOfflineDownload,
  completeSetup,
  formatBytes,
  getLifecycle,
  getOfflineModel,
  saveProfile,
  startOfflineDownload,
  type Lifecycle,
  type OfflineModelInfo,
  type OfflineState,
  type SetupStep,
} from '../nova/lifecycle';
import { useRuntime } from '../nova/runtime';
import { useNova } from '../context/NovaStateContext';

/**
 * NOVA's one-time setup, and the sign-in screen after that.
 *
 * What to show comes from /api/lifecycle, which decides from separate facts
 * (signed in, email verified, this account set up, this PC set up) and never
 * from the app version -- so an update cannot bring any of it back. After
 * setup the only screen that can return is sign-in, and only after signing
 * out or a session the server has revoked.
 */

type DeviceStep = 'offline' | 'permissions' | 'startup' | 'finish';
type OfflineOutcome = 'ready' | 'skipped' | 'failed';

const ROLES: { id: string; label: string }[] = [
  { id: 'student', label: 'Student' },
  { id: 'developer', label: 'Developer / Engineer' },
  { id: 'researcher', label: 'Researcher' },
  { id: 'business_owner', label: 'Business owner' },
  { id: 'professional', label: 'Professional' },
  { id: 'other', label: 'Other' },
];

const PERMISSIONS: { id: string; label: string; what: string; icon: string; allowByDefault: boolean }[] = [
  { id: 'microphone', label: 'Microphone', what: 'Used for natural voice conversations.', icon: 'fa-microphone', allowByDefault: true },
  { id: 'screen_read', label: 'Screen', what: 'Lets NOVA understand what is on your screen.', icon: 'fa-eye', allowByDefault: false },
  { id: 'file_read', label: 'Read files', what: 'Lets NOVA open and search files you point her to.', icon: 'fa-folder-open', allowByDefault: false },
  { id: 'file_write', label: 'Change files', what: 'Creating, editing, moving and deleting files.', icon: 'fa-file-pen', allowByDefault: false },
  { id: 'browser_read', label: 'Browse and research', what: 'Searching the web and reading pages for you.', icon: 'fa-globe', allowByDefault: true },
  { id: 'browser_interact', label: 'Act in the browser', what: 'Closing tabs and downloading on your behalf.', icon: 'fa-arrow-pointer', allowByDefault: false },
  { id: 'computer_control', label: 'Computer control', what: 'Opening apps, settings, mouse and keyboard.', icon: 'fa-laptop-code', allowByDefault: false },
  { id: 'exec', label: 'Run programs', what: 'Running code and trying out tools you point her to.', icon: 'fa-terminal', allowByDefault: false },
  { id: 'network', label: 'Send things out', what: 'Using connected services and skills that send your content to them.', icon: 'fa-paper-plane', allowByDefault: false },
];

const STARTUP: { id: string; label: string }[] = [
  { id: 'open', label: 'Start NOVA automatically with Windows' },
  { id: 'background', label: 'Start NOVA automatically and stay in the background' },
  { id: 'manual', label: "I'll launch NOVA myself" },
];

type Theme = ReturnType<typeof useNova>['theme'];

export const FirstRun: React.FC = () => {
  const { theme } = useNova();
  const rt = useRuntime();
  const [lc, setLc] = useState<Lifecycle | null>(null);
  const [loadError, setLoadError] = useState('');
  const [restarting, setRestarting] = useState('');

  const reload = useCallback(async () => {
    try {
      setLc(await getLifecycle());
      setLoadError('');
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  if (restarting) {
    return (
      <Shell theme={theme}>
        <Title theme={theme} text={restarting} sub="This takes a few seconds." />
      </Shell>
    );
  }
  if (!lc) {
    return loadError ? (
      <Shell theme={theme}>
        <Title theme={theme} text="NOVA could not check your account" sub={loadError} />
        <Primary theme={theme} onClick={reload}>Try again</Primary>
      </Shell>
    ) : null;
  }
  if (!lc.accounts_enabled || lc.next === 'none') return null;

  const after = async (st: { restarting?: boolean }) => {
    if (st.restarting) return setRestarting('Restarting NOVA…');
    await reload();
    rt.refreshStatus();
  };

  return (
    <Shell theme={theme}>
      {lc.next === 'sign_in' && <AccountStep lc={lc} onDone={after} />}
      {lc.next === 'verify_email' && <VerifyStep onDone={after} onSignOut={() => setRestarting('Signing out…')} />}
      {lc.next === 'profile' && <ProfileStep lc={lc} onDone={reload} />}
      {lc.next === 'device_setup' && <DeviceSetup onDone={reload} />}
    </Shell>
  );
};

// ---------------------------------------------------------------------------

const Shell: React.FC<{ theme: Theme; children: React.ReactNode }> = ({ theme, children }) => (
  <div data-nova-blocking-overlay="first-run" className="fixed inset-0 z-[58] flex items-center justify-center p-4 backdrop-blur-2xl animate-fade-in" style={{ backgroundColor: theme.palette.bgBase }}>
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Set up NOVA"
      className="w-full max-w-lg p-7 rounded-3xl border shadow-2xl space-y-4 max-h-[92vh] overflow-y-auto"
      style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder, boxShadow: `0 30px 70px -20px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}` }}
    >
      <div className="text-sm font-semibold tracking-[0.25em]" style={{ color: theme.palette.textPrimary }}>
        NOVA
      </div>
      {children}
    </div>
  </div>
);

const Title: React.FC<{ theme: Theme; text: string; sub?: string }> = ({ theme, text, sub }) => (
  <div>
    <h2 className="text-lg font-semibold tracking-tight" style={{ color: theme.palette.textPrimary }}>
      {text}
    </h2>
    {sub && (
      <p className="text-xs mt-1 leading-relaxed" style={{ color: theme.palette.textSecondary }}>
        {sub}
      </p>
    )}
  </div>
);

const Primary: React.FC<{ theme: Theme; onClick: () => void; disabled?: boolean; children: React.ReactNode }> = ({ theme, onClick, disabled, children }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    className="w-full py-2.5 rounded-xl border text-xs font-medium cursor-pointer disabled:opacity-50"
    style={{ backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#fff', borderColor: theme.palette.accentBorder }}
  >
    {children}
  </button>
);

const Secondary: React.FC<{ theme: Theme; onClick: () => void; disabled?: boolean; children: React.ReactNode }> = ({ theme, onClick, disabled, children }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    className="w-full py-2.5 rounded-xl border text-xs cursor-pointer disabled:opacity-50"
    style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}
  >
    {children}
  </button>
);

function useField() {
  const { theme } = useNova();
  return {
    style: { borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface },
    cls: 'w-full text-xs p-2.5 rounded-xl border outline-none select-text',
  };
}

const Note: React.FC<{ note: { text: string; error: boolean } | null }> = ({ note }) => {
  const { theme } = useNova();
  if (!note) return null;
  return (
    <p role={note.error ? 'alert' : 'status'} className="text-[11px]" style={{ color: note.error ? '#ef4444' : theme.palette.textSecondary }}>
      {note.text}
    </p>
  );
};

// -- welcome + account -------------------------------------------------------

const AccountStep: React.FC<{ lc: Lifecycle; onDone: (st: { restarting?: boolean }) => void }> = ({ lc, onDone }) => {
  const { theme } = useNova();
  const f = useField();
  const [welcomed, setWelcomed] = useState(!lc.first_run);
  const [mode, setMode] = useState<'signin' | 'signup'>(lc.first_run ? 'signup' : 'signin');
  const [fullName, setFullName] = useState('');
  const [preferred, setPreferred] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);

  if (!welcomed) {
    return (
      <>
        <Title theme={theme} text="Welcome to NOVA." sub="Your personal AI, built to work with you and around the way you work." />
        <p className="text-xs" style={{ color: theme.palette.textSecondary }}>
          Let's get NOVA ready.
        </p>
        <Primary theme={theme} onClick={() => setWelcomed(true)}>Continue</Primary>
      </>
    );
  }

  const submit = async () => {
    setBusy(true);
    setNote(null);
    try {
      const path = mode === 'signup' ? '/api/account/signup' : '/api/account/signin';
      const st = await accountCall<AccountState & { restarting?: boolean }>(path, 'POST', {
        email: email.trim(),
        password,
        display_name: (preferred || fullName).trim(),
      });
      setPassword('');
      onDone(st);
    } catch (e) {
      setNote({ text: (e as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const forgot = async () => {
    if (!email.trim()) return setNote({ text: 'Enter your email address first.', error: true });
    setBusy(true);
    try {
      const j = await accountCall<{ message?: string }>('/api/account/password/forgot', 'POST', { email: email.trim() });
      setNote({ text: j.message || 'If that address has an account, a reset link is on its way.', error: false });
    } catch (e) {
      setNote({ text: (e as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const link = 'text-[11px] cursor-pointer opacity-70 hover:opacity-100 disabled:opacity-30';
  return (
    <>
      <Title
        theme={theme}
        text={mode === 'signup' ? 'Create your NOVA account' : lc.session_expired ? 'Please sign in again' : 'Sign in to NOVA'}
        sub={mode === 'signup' ? 'Your account keeps your NOVA yours. Your memories and files stay on this computer.' : undefined}
      />
      {mode === 'signup' && (
        <>
          <input className={f.cls} style={f.style} autoComplete="name" placeholder="Full name" value={fullName} onChange={(e) => setFullName(e.target.value)} autoFocus />
          <input className={f.cls} style={f.style} placeholder="Preferred name (what NOVA calls you)" value={preferred} onChange={(e) => setPreferred(e.target.value)} />
        </>
      )}
      <input className={f.cls} style={f.style} type="email" autoComplete="username" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus={mode === 'signin'} />
      <input
        className={f.cls}
        style={f.style}
        type="password"
        autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
        placeholder={mode === 'signup' ? 'Password (at least 10 characters)' : 'Password'}
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && submit()}
      />
      <Primary theme={theme} onClick={submit} disabled={busy || !email.trim() || !password}>
        {busy ? 'One moment…' : mode === 'signup' ? 'Continue' : 'Sign in'}
      </Primary>
      <div className="flex justify-between">
        <button className={link} style={{ color: theme.palette.textSecondary }} onClick={() => { setMode(mode === 'signup' ? 'signin' : 'signup'); setNote(null); }}>
          {mode === 'signup' ? 'I already have an account' : 'Create an account instead'}
        </button>
        {mode === 'signin' && (
          <button className={link} style={{ color: theme.palette.textSecondary }} disabled={busy} onClick={forgot}>
            Forgot password
          </button>
        )}
      </div>
      <Note note={note} />
    </>
  );
};

// -- email verification --------------------------------------------------------

const VerifyStep: React.FC<{ onDone: (st: { restarting?: boolean }) => void; onSignOut: () => void }> = ({ onDone, onSignOut }) => {
  const { theme } = useNova();
  const [acct, setAcct] = useState<AccountState | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  useEffect(() => {
    accountCall<AccountState>('/api/account').then(setAcct).catch(() => undefined);
  }, []);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
    } catch (e) {
      setNote({ text: (e as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };
  const link = 'text-[11px] cursor-pointer opacity-70 hover:opacity-100 disabled:opacity-30';
  return (
    <>
      <Title theme={theme} text="Check your email" />
      <p className="text-xs leading-relaxed" style={{ color: theme.palette.textSecondary }}>
        {acct?.email_delivery?.sent
          ? `We've sent a confirmation link to ${acct?.user?.email || 'your email address'}. Open it, then come back here.`
          : // Nothing confirmed as sent: never tell anyone to check an inbox.
            'NOVA could not confirm that a confirmation email was sent. Use “Send it again”; if it keeps failing, email is not set up on the NOVA server yet.'}
      </p>
      <Primary
        theme={theme}
        disabled={busy}
        onClick={() =>
          run(async () => {
            const st = await accountCall<AccountState>('/api/account/refresh', 'POST');
            if (!st.email_verified) return setNote({ text: 'Not confirmed yet. Open the link in the email, then try again.', error: false });
            onDone({});
          })
        }
      >
        I've confirmed my email
      </Primary>
      <div className="flex justify-between">
        <button
          className={link}
          style={{ color: theme.palette.textSecondary }}
          disabled={busy}
          onClick={() =>
            run(async () => {
              const j = await accountCall<{ email_delivery?: { sent?: boolean } }>('/api/account/verify/resend', 'POST');
              setNote(j.email_delivery?.sent ? { text: 'Sent. Check your inbox (and spam).', error: false } : { text: 'Could not send the email. Try again later.', error: true });
            })
          }
        >
          Send it again
        </button>
        <button
          className={link}
          style={{ color: theme.palette.textSecondary }}
          disabled={busy}
          onClick={() =>
            run(async () => {
              const st = await accountCall<{ restarting?: boolean }>('/api/account/signout', 'POST');
              if (st.restarting) onSignOut();
              else onDone(st);
            })
          }
        >
          Use a different account
        </button>
      </div>
      <Note note={note} />
    </>
  );
};

// -- profile -------------------------------------------------------------------

const ProfileStep: React.FC<{ lc: Lifecycle; onDone: () => void }> = ({ lc, onDone }) => {
  const { theme } = useNova();
  const f = useField();
  const p = lc.instance?.profile || {};
  const [name, setName] = useState(p.preferred_name || '');
  const [role, setRole] = useState(p.role || '');
  const [about, setAbout] = useState(p.about || '');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);

  const save = async (skip: boolean) => {
    setBusy(true);
    setNote(null);
    try {
      await saveProfile(skip ? {} : { preferred_name: name.trim(), role, about: about.trim() });
      onDone();
    } catch (e) {
      setNote({ text: (e as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Title theme={theme} text="Tell NOVA about yourself" sub="Everything here is optional. NOVA can also learn about you naturally as you use her." />
      <label className="block">
        <span className="text-[11px] font-medium" style={{ color: theme.palette.textPrimary }}>Preferred name</span>
        <input className={`${f.cls} mt-1`} style={f.style} maxLength={80} value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </label>
      <div>
        <span className="text-[11px] font-medium" style={{ color: theme.palette.textPrimary }}>What best describes you?</span>
        <div className="grid grid-cols-2 gap-2 mt-1.5" role="radiogroup" aria-label="What best describes you">
          {ROLES.map((r) => {
            const on = role === r.id;
            return (
              <button
                key={r.id}
                role="radio"
                aria-checked={on}
                onClick={() => setRole(on ? '' : r.id)}
                className="px-3 py-2 rounded-xl border text-left text-[11px] cursor-pointer transition-all"
                style={{ borderColor: on ? theme.palette.accent : theme.palette.glassBorder, color: on ? theme.palette.textPrimary : theme.palette.textSecondary, boxShadow: on ? `0 0 0 1px ${theme.palette.accent}` : undefined }}
              >
                {r.label}
              </button>
            );
          })}
        </div>
      </div>
      <label className="block">
        <span className="text-[11px] font-medium" style={{ color: theme.palette.textPrimary }}>Tell NOVA a little about yourself</span>
        <textarea className={`${f.cls} mt-1 h-24 resize-none`} style={f.style} maxLength={4000} value={about} onChange={(e) => setAbout(e.target.value)} placeholder="What you work on, what you'd like help with…" />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <Secondary theme={theme} onClick={() => save(true)} disabled={busy}>Skip for now</Secondary>
        <Primary theme={theme} onClick={() => save(false)} disabled={busy}>{busy ? 'Saving…' : 'Continue'}</Primary>
      </div>
      <Note note={note} />
    </>
  );
};

// -- this PC: offline model, permissions, startup, finish ------------------------

const DeviceSetup: React.FC<{ onDone: () => void }> = ({ onDone }) => {
  const { theme } = useNova();
  const rt = useRuntime();
  const [step, setStep] = useState<DeviceStep>('offline');
  const [offline, setOffline] = useState<OfflineOutcome>('skipped');
  const [perms, setPerms] = useState<Record<string, boolean>>(() => Object.fromEntries(PERMISSIONS.map((p) => [p.id, p.allowByDefault])));
  const [startup, setStartup] = useState('manual');
  const [result, setResult] = useState<SetupStep[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);

  const finish = async () => {
    setBusy(true);
    setNote(null);
    try {
      const permissions = Object.fromEntries(PERMISSIONS.map((p) => [p.id, perms[p.id] ? 'allow' : 'ask']));
      const j = await completeSetup({ permissions, startup_mode: startup, offline_model: offline });
      setResult(j.steps);
      setStep('finish');
    } catch (e) {
      setNote({ text: (e as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  };

  if (step === 'offline') return <OfflineStep onNext={(o) => { setOffline(o); setStep('permissions'); }} />;

  if (step === 'permissions') {
    return (
      <>
        <Title theme={theme} text="Give NOVA access to your computer" sub="You control what NOVA can access. Anything you leave on “Ask me”, she asks before doing — for the microphone, voice waits until you press the mic; for the screen, she only looks when you switch it on. You can change this, or turn anything off completely, any time in Settings → Permissions." />
        <div className="space-y-2">
          {PERMISSIONS.map((p) => {
            const on = perms[p.id];
            return (
              <button
                key={p.id}
                role="switch"
                aria-checked={on}
                onClick={() => setPerms({ ...perms, [p.id]: !on })}
                className="w-full flex items-center gap-3 p-2.5 rounded-xl border text-left cursor-pointer transition-all"
                style={{ borderColor: on ? theme.palette.accent : theme.palette.glassBorder }}
              >
                <i className={`fa-solid ${p.icon} text-xs w-4 text-center`} style={{ color: theme.palette.accent }} />
                <span className="flex-1 min-w-0">
                  <span className="block text-xs font-medium" style={{ color: theme.palette.textPrimary }}>{p.label}</span>
                  <span className="block text-[11px] opacity-70" style={{ color: theme.palette.textSecondary }}>{p.what}</span>
                </span>
                <span className="text-[11px] shrink-0" style={{ color: on ? theme.palette.accent : theme.palette.textMuted }}>
                  {on ? '✓ Allow' : 'Ask me'}
                </span>
              </button>
            );
          })}
        </div>
        <Primary theme={theme} onClick={() => setStep('startup')}>Continue</Primary>
      </>
    );
  }

  if (step === 'startup') {
    return (
      <>
        <Title theme={theme} text="How should NOVA start?" sub="You can change this later in Settings." />
        <div className="space-y-2" role="radiogroup" aria-label="How NOVA starts">
          {STARTUP.map((s) => {
            const on = startup === s.id;
            return (
              <button
                key={s.id}
                role="radio"
                aria-checked={on}
                onClick={() => setStartup(s.id)}
                className="w-full p-3 rounded-xl border text-left text-xs cursor-pointer transition-all"
                style={{ borderColor: on ? theme.palette.accent : theme.palette.glassBorder, color: on ? theme.palette.textPrimary : theme.palette.textSecondary, boxShadow: on ? `0 0 0 1px ${theme.palette.accent}` : undefined }}
              >
                {on ? '● ' : '○ '}
                {s.label}
              </button>
            );
          })}
        </div>
        <Primary theme={theme} onClick={finish} disabled={busy}>{busy ? 'Setting up…' : 'Finish setup'}</Primary>
        <Note note={note} />
      </>
    );
  }

  return (
    <>
      <Title theme={theme} text="NOVA is ready" />
      <ul className="space-y-1.5">
        {(result || []).map((s) => (
          <li key={s.id} className="text-xs flex gap-2" style={{ color: theme.palette.textPrimary }}>
            <span style={{ color: s.ok ? '#22c55e' : theme.palette.textMuted }}>{s.ok ? '✓' : '–'}</span>
            <span>
              {s.label}
              {s.detail && <span className="opacity-60" style={{ color: theme.palette.textSecondary }}> — {s.detail}</span>}
            </span>
          </li>
        ))}
      </ul>
      <Primary
        theme={theme}
        onClick={() => {
          onDone();
          rt.refreshStatus();
        }}
      >
        Open NOVA
      </Primary>
    </>
  );
};

const OfflineStep: React.FC<{ onNext: (o: OfflineOutcome) => void }> = ({ onNext }) => {
  const { theme } = useNova();
  const [info, setInfo] = useState<OfflineModelInfo | null>(null);
  const [st, setSt] = useState<OfflineState | null>(null);
  const [error, setError] = useState('');
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    getOfflineModel()
      .then((j) => {
        setInfo(j);
        if (j.state.state === 'downloading' || j.state.state === 'verifying') setSt(j.state);
      })
      .catch((e) => setError((e as Error).message));
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
      } catch {
        /* keep the last state; the next poll may succeed */
      }
    }, 1000);
    return () => window.clearInterval(timer.current);
  }, [polling]);

  const rec = info?.models.find((m) => m.id === info.recommended) || info?.models[0];
  const start = async () => {
    if (!rec) return;
    setError('');
    try {
      const j = await startOfflineDownload(rec.id);
      setSt(j.state);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (st && polling) {
    const pct = st.percent ?? 0;
    return (
      <>
        <Title theme={theme} text={st.state === 'verifying' ? 'Checking the offline model' : 'Downloading offline model'} />
        <div className="h-2 rounded-full overflow-hidden" style={{ backgroundColor: theme.palette.glassSurface }} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className="h-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: theme.palette.accent }} />
        </div>
        <p className="text-xs" style={{ color: theme.palette.textSecondary }}>
          {st.total_bytes ? `${pct.toFixed(0)}% · ${formatBytes(st.completed_bytes)} / ${formatBytes(st.total_bytes)}` : st.status_text || 'Starting…'}
        </p>
        <p className="text-[11px]" style={{ color: theme.palette.textMuted }}>Please keep NOVA open.</p>
        <Secondary theme={theme} onClick={async () => { await cancelOfflineDownload(); onNext('skipped'); }}>Cancel and continue setup</Secondary>
      </>
    );
  }

  if (st?.state === 'ready') {
    return (
      <>
        <Title theme={theme} text="Offline model ready" sub="NOVA can keep helping when your internet is down, with lighter answers." />
        <Primary theme={theme} onClick={() => onNext('ready')}>Continue</Primary>
      </>
    );
  }

  if (st && (st.state === 'failed' || st.state === 'runtime_missing' || st.state === 'cancelled')) {
    return (
      <>
        <Title theme={theme} text={st.state === 'runtime_missing' ? 'Offline engine not found' : 'Download interrupted.'} />
        <p className="text-xs leading-relaxed" style={{ color: theme.palette.textSecondary }}>
          {st.state === 'runtime_missing' ? st.error : 'NOVA can continue setup. You can download the offline model later from Settings.'}
        </p>
        <Primary theme={theme} onClick={() => onNext('failed')}>Continue setup</Primary>
      </>
    );
  }

  return (
    <>
      <Title theme={theme} text="Prepare NOVA for offline use" sub="Internet isn't always reliable. Download an offline AI model now so NOVA can keep giving limited help when you're offline." />
      {rec ? (
        <div className="p-3 rounded-xl border text-xs" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary }}>
          <div className="font-medium">Recommended: {rec.name || rec.id}</div>
          <div className="opacity-70 mt-0.5" style={{ color: theme.palette.textSecondary }}>
            About {formatBytes(rec.size_bytes)} download{rec.note ? ` · ${rec.note}` : ''}
            {rec.fits === false && info?.ram_gb ? ` · needs ${rec.min_ram_gb} GB of memory; this PC has ${info.ram_gb} GB` : ''}
          </div>
        </div>
      ) : (
        !error && <p className="text-xs" style={{ color: theme.palette.textSecondary }}>Checking what this PC can run…</p>
      )}
      {error && <p className="text-[11px]" style={{ color: '#ef4444' }}>{error}</p>}
      <div className="grid grid-cols-2 gap-2">
        <Secondary theme={theme} onClick={() => onNext('skipped')}>Skip for now</Secondary>
        <Primary theme={theme} onClick={start} disabled={!rec}>Download now</Primary>
      </div>
    </>
  );
};
