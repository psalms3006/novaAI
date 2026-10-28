import React, { useEffect, useState } from 'react';
import { OnboardingLayout, OnboardingButton, OnboardingInput } from './OnboardingLayout';
import { accountCall, type AccountState } from '../nova/account';
import { useNova } from '../context/NovaStateContext';

export const VerifyStep: React.FC<{ onDone: (st: { restarting?: boolean }) => void }> = ({ onDone }) => {
  const { theme } = useNova();
  const [acct, setAcct] = useState<AccountState | null>(null);
  const [busy, setBusy] = useState(false);
  // `ok` distinguishes "sent" from "could not send": both are shown in the
  // same place, but only one may be green.
  const [note, setNote] = useState<{ text: string; ok: boolean } | null>(null);
  const [code, setCode] = useState('');

  const say = (text: string) => setNote({ text, ok: false });

  useEffect(() => {
    accountCall<AccountState>('/api/account').then(setAcct).catch(() => undefined);
  }, []);

  const checkStatus = async () => {
    setBusy(true);
    setNote(null);
    try {
      const st = await accountCall<AccountState>('/api/account/refresh', 'POST');
      if (!st.email_verified) throw new Error("Email not verified yet. Please check your inbox.");
      onDone({});
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const verifyWithCode = async () => {
    if (!code.trim() || code.length !== 6 || !/^\d{6}$/.test(code)) {
      say("Please enter a valid 6-digit code.");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const st = await accountCall<AccountState & { message?: string }>('/api/account/email/verify_code', 'POST', { code: code.trim() });
      if (st.email_verified) {
        onDone({});
      } else {
        say(st.message || "Verification failed.");
      }
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const resend = async () => {
    setBusy(true);
    setNote(null);
    try {
      const j = await accountCall<{ email_delivery?: { sent?: boolean }; message?: string }>('/api/account/verify/resend', 'POST');
      if (j.email_delivery?.sent) {
        setNote({ text: 'Sent. Check your inbox (and spam) for the link and the 6-digit code.', ok: true });
      } else {
        // The provider refused, or there is no provider configured. Saying
        // "sent" here is the exact lie this flow exists to prevent.
        say(j.message || 'NOVA could not send the email. Try again later, or enter a code if one arrived.');
      }
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Only claim an email went out when the server could confirm it did.
  const delivered = acct?.email_delivery?.sent;
  const address = acct?.user?.email || 'your email';
  const subtitle = delivered === true
    ? `We've sent a confirmation link and a 6-digit code to ${address}. Use either to continue.`
    : delivered === false
      ? `We could not confirm that an email reached ${address}. Try "Resend" below, or enter a 6-digit code if one arrived.`
      : `Verify ${address} to continue.`;

  return (
    <OnboardingLayout
      title="Verify your email"
      subtitle={subtitle}
    >
      <div className="flex flex-col items-center space-y-6 pt-2">
        <div 
          className="w-20 h-20 rounded-full flex items-center justify-center"
          style={{ backgroundColor: `${theme.palette.accent}10`, border: `1px solid ${theme.palette.accent}25` }}
        >
          <i className="fa-solid fa-envelope-open-text text-2xl" style={{ color: theme.palette.accent }} />
        </div>

        {note && (
          <p className={`text-xs text-center px-4 ${note.ok ? 'text-green-500' : 'text-red-500'}`}>
            {note.text}
          </p>
        )}

        <div className="w-full space-y-3">
          <OnboardingButton onClick={checkStatus} disabled={busy}>
            {busy ? 'Checking...' : "I've verified my email"}
          </OnboardingButton>

          <div className="pt-3 border-t w-full" style={{ borderColor: theme.palette.glassBorder }}>
            <p className="text-xs font-medium mb-2" style={{ color: theme.palette.textPrimary }}>Or enter the 6-digit code</p>
            <OnboardingInput 
              placeholder="Enter 6-digit code" 
              value={code} 
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))} 
              autoFocus 
              maxLength={6}
            />
            <OnboardingButton onClick={verifyWithCode} disabled={busy || code.length !== 6}>
              {busy ? 'Verifying...' : 'Verify with code'}
            </OnboardingButton>
          </div>

          <div className="text-center pt-2">
            <button 
              className="text-xs opacity-60 hover:opacity-100 transition-opacity cursor-pointer disabled:opacity-30" 
              onClick={resend}
              disabled={busy}
            >
              Didn't receive it? Resend link and code
            </button>
          </div>
        </div>
      </div>
    </OnboardingLayout>
  );
};
