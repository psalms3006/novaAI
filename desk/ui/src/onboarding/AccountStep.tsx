import React, { useEffect, useRef, useState } from 'react';
import { OnboardingLayout, OnboardingButton, OnboardingInput } from './OnboardingLayout';
import { accountCall, type AccountState } from '../nova/account';
import { startGoogleAuth, type Lifecycle } from '../nova/lifecycle';
import { useNova } from '../context/NovaStateContext';
import { Welcome } from './Welcome';

export const AccountStep: React.FC<{ lc: Lifecycle; onDone: (st: { restarting?: boolean }) => void }> = ({ lc, onDone }) => {
  const { theme } = useNova();
  const [welcomed, setWelcomed] = useState(!lc.first_run);
  const [mode, setMode] = useState<'signin' | 'signup'>(lc.first_run ? 'signup' : 'signin');
  const [fullName, setFullName] = useState('');
  const [preferred, setPreferred] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!welcomed) {
    return <Welcome onContinue={() => setWelcomed(true)} />;
  }

  const submit = async () => {
    setBusy(true);
    setError(null);
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
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Google sign-in finishes in this window's own loopback route; the popup
  // only carries the browser. Polling tells us how it ended -- success is a
  // signed-in account, failure is the reason the route recorded.
  const pollToken = useRef({ stop: false });
  useEffect(() => () => { pollToken.current.stop = true; }, []);

  const handleGoogleSignIn = async () => {
    setBusy(true);
    setError(null);
    let authWindow: Window | null = null;
    try {
      const { auth_url } = await startGoogleAuth();
      authWindow = window.open(auth_url, 'google_auth', 'width=500,height=600');
      if (!authWindow) throw new Error('Popup blocked. Please allow popups.');
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
      return;
    }

    const token = { stop: false };
    pollToken.current = token;
    const deadline = Date.now() + 5 * 60 * 1000;
    while (!token.stop && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1000));
      if (token.stop) break;
      try {
        const st = await accountCall<{ ok: boolean; pending: boolean; error: { message: string } | null }>('/api/account/google/status');
        if (st.error) {
          authWindow?.close();
          setError(st.error.message);
          setBusy(false);
          return;
        }
      } catch { /* older backend without the status route: signed-in is enough */ }
      try {
        const acct = await accountCall<AccountState>('/api/account');
        if (acct.signed_in) {
          authWindow?.close();
          setBusy(false);
          onDone({ restarting: false });
          return;
        }
      } catch { /* keep waiting */ }
    }
    if (token.stop) return;
    authWindow?.close();
    setError('Google sign-in did not finish. Please try again.');
    setBusy(false);
  };

  return (
    <OnboardingLayout 
      title={mode === 'signup' ? 'Create your account' : 'Sign in to NOVA'}
      subtitle={mode === 'signup' ? 'Your account keeps your NOVA yours. Your memories and files stay on this computer.' : 'Welcome back to your personal AI.'}
    >
      <div className="space-y-4">
        {mode === 'signup' && (
          <div className="grid grid-cols-2 gap-3">
            <OnboardingInput placeholder="Full name" value={fullName} onChange={(e) => setFullName(e.target.value)} autoFocus />
            <OnboardingInput placeholder="Preferred name" value={preferred} onChange={(e) => setPreferred(e.target.value)} />
          </div>
        )}
        
        <OnboardingInput 
          type="email" 
          placeholder="Email address" 
          value={email} 
          onChange={(e) => setEmail(e.target.value)} 
          autoFocus={mode === 'signin'} 
        />
        
        <OnboardingInput 
          type="password" 
          placeholder="Password" 
          value={password} 
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
        />

        {error && <p className="text-[11px] text-red-500 animate-shake">{error}</p>}

        <div className="pt-2 space-y-3">
          <OnboardingButton onClick={submit} disabled={busy || !email.trim() || !password}>
            {busy ? 'Processing...' : mode === 'signup' ? 'Create Account' : 'Sign In'}
          </OnboardingButton>
          
          <div className="relative py-2">
            <div className="absolute inset-0 flex items-center"><div className="w-full border-t" style={{ borderColor: theme.palette.glassBorder }} /></div>
            <div className="relative flex justify-center text-[10px] uppercase tracking-widest opacity-40"><span className="px-3" style={{ backgroundColor: theme.palette.bgElevated }}>Or</span></div>
          </div>

          <OnboardingButton variant="secondary" onClick={handleGoogleSignIn} disabled={busy} className="flex items-center justify-center gap-2">
            <svg width="16" height="16" viewBox="0 0 24 24">
              <path fill="currentColor" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
              <path fill="currentColor" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
              <path fill="currentColor" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
              <path fill="currentColor" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
            </svg>
            Continue with Google
          </OnboardingButton>
        </div>

        <div className="text-center pt-2">
          <button 
            className="text-xs opacity-60 hover:opacity-100 transition-opacity cursor-pointer" 
            onClick={() => setMode(mode === 'signup' ? 'signin' : 'signup')}
          >
            {mode === 'signup' ? 'Already have an account? Sign in' : 'New here? Create an account'}
          </button>
        </div>
      </div>
    </OnboardingLayout>
  );
};
