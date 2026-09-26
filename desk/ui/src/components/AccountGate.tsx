import React, { useEffect, useState } from 'react';
import { accountCall, getAccount, type AccountState } from '../nova/account';
import { useNova } from '../context/NovaStateContext';

/**
 * NOVA Cloud sign-in, shown once before the main interface when a NOVA Cloud
 * backend is configured and nobody is signed in. After that the session is
 * restored from the OS keystore, so the user is never asked again just for
 * reopening the app. With no backend configured this renders nothing, and an
 * unreachable account service never stops NOVA loading.
 */
export const AccountGate: React.FC = () => {
  const { theme } = useNova();
  const [acct, setAcct] = useState<AccountState | null>(null);
  const [mode, setMode] = useState<'signin' | 'signup'>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);

  useEffect(() => {
    getAccount()
      .then(setAcct)
      .catch(() => setAcct({ configured: false, signed_in: false }));
  }, []);

  if (!acct || !acct.configured) return null;
  const needsVerify = acct.signed_in && acct.verification_required && !acct.email_verified;
  if (acct.signed_in && !needsVerify) return null;

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

  const submit = () =>
    run(async () => {
      const path = mode === 'signup' ? '/api/account/signup' : '/api/account/signin';
      const st = await accountCall(path, 'POST', { email: email.trim(), password, display_name: displayName.trim() });
      setPassword('');
      setAcct(st);
    });

  const field = { borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface };
  const input = 'w-full text-xs p-2.5 rounded-xl border outline-none select-text';
  const link = 'text-[11px] cursor-pointer opacity-70 hover:opacity-100 disabled:opacity-30';

  return (
    <div className="fixed inset-0 z-[57] flex items-center justify-center p-4 backdrop-blur-2xl" style={{ backgroundColor: theme.palette.bgBase }}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Sign in to NOVA"
        className="w-full max-w-sm p-6 rounded-3xl border shadow-2xl space-y-3"
        style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder, boxShadow: `0 30px 70px -20px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}` }}
      >
        <div className="text-sm font-semibold tracking-[0.2em]" style={{ color: theme.palette.textPrimary }}>
          NOVA
        </div>

        {needsVerify ? (
          <>
            <h3 className="text-xs font-medium" style={{ color: theme.palette.textPrimary }}>
              Confirm your email
            </h3>
            <p className="text-[11px] leading-relaxed" style={{ color: theme.palette.textSecondary }}>
              {acct.email_delivery?.sent
                ? `We sent a link to ${acct.user?.email || 'your address'}. Open it to finish setting up your account.`
                : // Nothing was sent: never tell the user to check an inbox.
                  'This NOVA server has no email delivery configured, so no confirmation link could be sent. Ask your administrator to finish setting up email.'}
            </p>
            <button
              disabled={busy}
              onClick={() =>
                run(async () => {
                  const st = await accountCall('/api/account/refresh', 'POST');
                  setAcct(st);
                  if (!st.email_verified) setNote({ text: 'Not confirmed yet. Open the link, then try again.', error: false });
                })
              }
              className="w-full py-2 rounded-xl border text-xs font-medium cursor-pointer disabled:opacity-50"
              style={{ backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#fff', borderColor: theme.palette.accentBorder }}
            >
              I have confirmed
            </button>
            <div className="flex justify-between">
              <button
                className={link}
                style={{ color: theme.palette.textSecondary }}
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const j = await accountCall('/api/account/verify/resend', 'POST');
                    setNote({ text: j.email_delivery?.sent ? 'Sent. Check your inbox.' : 'Could not send — email is not configured on this server.', error: !j.email_delivery?.sent });
                  })
                }
              >
                Send it again
              </button>
              <button className={link} style={{ color: theme.palette.textSecondary }} disabled={busy} onClick={() => run(async () => setAcct(await accountCall('/api/account/signout', 'POST')))}>
                Sign out
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="text-[11px]" style={{ color: theme.palette.textSecondary }}>
              {mode === 'signup' ? 'Your account carries your preferences between devices. Conversations and local models stay on this machine.' : 'Sign in to carry your NOVA between devices.'}
            </p>
            <input className={input} style={field} type="email" autoComplete="username" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
            <input
              className={input}
              style={field}
              type="password"
              autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && submit()}
            />
            {mode === 'signup' && <input className={input} style={field} type="text" autoComplete="name" placeholder="What should NOVA call you?" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />}
            <button
              onClick={submit}
              disabled={busy || !email.trim() || !password}
              className="w-full py-2 rounded-xl border text-xs font-medium cursor-pointer disabled:opacity-50"
              style={{ backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#fff', borderColor: theme.palette.accentBorder }}
            >
              {busy ? 'One moment…' : mode === 'signup' ? 'Create account' : 'Sign in'}
            </button>
            <div className="flex justify-between">
              <button className={link} style={{ color: theme.palette.textSecondary }} onClick={() => setMode(mode === 'signup' ? 'signin' : 'signup')}>
                {mode === 'signup' ? 'I already have an account' : 'Create an account instead'}
              </button>
              {mode === 'signin' && (
                <button
                  className={link}
                  style={{ color: theme.palette.textSecondary }}
                  disabled={busy}
                  onClick={() => {
                    if (!email.trim()) return setNote({ text: 'Enter your email address first.', error: true });
                    run(async () => {
                      const j = await accountCall('/api/account/password/forgot', 'POST', { email: email.trim() });
                      setNote({ text: j.message || 'Check your email for a reset link.', error: false });
                    });
                  }}
                >
                  Forgot password
                </button>
              )}
            </div>
          </>
        )}

        {note && (
          <p role={note.error ? 'alert' : 'status'} className="text-[11px]" style={{ color: note.error ? '#ef4444' : theme.palette.textSecondary }}>
            {note.text}
          </p>
        )}
      </div>
    </div>
  );
};
