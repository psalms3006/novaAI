import React, { useEffect, useState } from 'react';
import { postJSON, api } from '../../../nova/api';
import { accountCall, getAccount, type AccountDevice, type AccountState } from '../../../nova/account';
import { useRuntime } from '../../../nova/runtime';
import { useNova } from '../../../context/NovaStateContext';
import { Button, Card, Row, SectionHeader, Stat } from '../primitives';

const MODE_LABEL: Record<string, string> = {
  env: 'API key from the environment (.env)',
  byok: 'Your own Gemini key, sealed with Windows DPAPI',
  cloud: 'NOVA Cloud account',
  offline: 'Offline — local intelligence only',
  '': 'Not set up',
};

/** How NOVA reaches her intelligence, and the NOVA Cloud account. */
export const AccountSection: React.FC = () => {
  const { theme, addToast } = useNova();
  const rt = useRuntime();
  const auth = rt.status?.auth;
  const [key, setKey] = useState('');
  const [cloudUrl, setCloudUrl] = useState('');
  const [busy, setBusy] = useState('');
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [acct, setAcct] = useState<AccountState | null>(null);
  const [devices, setDevices] = useState<AccountDevice[] | null>(null);
  const [devicesError, setDevicesError] = useState('');
  const [revoking, setRevoking] = useState<string | null>(null);

  const loadAccount = async () => {
    try {
      const a = await getAccount();
      setAcct(a);
      if (a.configured && a.signed_in) {
        try {
          setDevices((await accountCall<{ devices: AccountDevice[] }>('/api/account/devices')).devices || []);
          setDevicesError('');
        } catch (e) {
          setDevices(null);
          setDevicesError((e as Error).message);
        }
      }
    } catch {
      setAcct({ configured: false, signed_in: false });
    }
  };

  useEffect(() => {
    loadAccount();
  }, []);

  const act = async (label: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(label);
    try {
      await fn();
      addToast(done, undefined, 'success');
      await rt.refreshStatus();
    } catch (e) {
      addToast(`${label} failed`, (e as Error).message, 'warning');
    } finally {
      setBusy('');
    }
  };

  const saveKey = () =>
    act(
      'Saving the key',
      async () => {
        const j = await postJSON<{ ok: boolean; error?: string }>('/api/onboarding/byok', { api_key: key.trim() });
        if (!j.ok) throw new Error(j.error || 'refused');
        setKey('');
        rt.startVoice();
      },
      'Key saved',
    );

  const removeKey = () => {
    setConfirmRemove(false);
    return act(
      'Removing the key',
      async () => {
        const r = await api('/api/onboarding/byok', { method: 'DELETE' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
      },
      'Key removed',
    );
  };

  const field = { borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface };

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Account & Access" subtitle="How NOVA reaches her intelligence, and your NOVA Cloud account if you use one." />

      <Card title="Intelligence access">
        <Row first label="Using" hint={auth?.credential_warning || auth?.cloud_error || undefined}>
          <span className="text-xs font-mono" style={{ color: theme.palette.textPrimary }}>
            {MODE_LABEL[auth?.mode ?? ''] || auth?.mode || '—'}
          </span>
        </Row>
        {auth?.byok_masked && (
          <Row label="Key on file">
            <span className="text-xs font-mono" style={{ color: theme.palette.textSecondary }}>
              {auth.byok_masked}
            </span>
          </Row>
        )}
        <div className="pt-3 border-t space-y-2" style={{ borderColor: theme.palette.glassBorder }}>
          <div className="text-xs font-medium" style={{ color: theme.palette.textPrimary }}>
            {auth?.byok_present ? 'Replace your Gemini key' : 'Use your own Gemini key'}
          </div>
          <div className="flex gap-2">
            <input
              type="password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && key.trim() && saveKey()}
              autoComplete="off"
              spellCheck={false}
              placeholder="Paste your API key"
              aria-label="Gemini API key"
              className="flex-1 text-xs p-2.5 rounded-xl border outline-none font-mono select-text"
              style={field}
            />
            <Button tone="accent" onClick={saveKey} disabled={!key.trim() || !!busy}>
              Save key
            </Button>
          </div>
          <div className="text-[10px]" style={{ color: theme.palette.textMuted }}>
            Sealed on this computer with Windows DPAPI. The key is never shown again, only its last characters.
          </div>
        </div>
        <div className="flex flex-wrap gap-2 pt-1">
          {auth?.byok_present &&
            (confirmRemove ? (
              <>
                <Button onClick={() => setConfirmRemove(false)}>Keep it</Button>
                <Button tone="danger" onClick={removeKey} disabled={!!busy}>
                  Yes, remove the key
                </Button>
              </>
            ) : (
              <Button tone="danger" onClick={() => setConfirmRemove(true)} disabled={!!busy}>
                Remove key
              </Button>
            ))}
          {auth?.mode !== 'offline' && (
            <Button
              onClick={() =>
                act(
                  'Going offline',
                  async () => {
                    await postJSON('/api/onboarding/offline', {});
                    await rt.stopVoice();
                  },
                  'Offline mode',
                )
              }
              disabled={!!busy}
            >
              Go offline
            </Button>
          )}
        </div>
        <div className="pt-3 border-t space-y-2" style={{ borderColor: theme.palette.glassBorder }}>
          <div className="text-xs font-medium" style={{ color: theme.palette.textPrimary }}>
            Connect to a NOVA Cloud server
          </div>
          <div className="flex gap-2">
            <input value={cloudUrl} onChange={(e) => setCloudUrl(e.target.value)} placeholder="https://nova.example.com" aria-label="NOVA server address" className="flex-1 text-xs p-2.5 rounded-xl border outline-none select-text" style={field} />
            <Button
              onClick={() =>
                act(
                  'Connecting',
                  async () => {
                    const j = await postJSON<{ ok: boolean; error?: string }>('/api/onboarding/cloud', { url: cloudUrl.trim() });
                    if (!j.ok) throw new Error(j.error || 'refused');
                    await loadAccount();
                  },
                  'Connected to NOVA Cloud',
                )
              }
              disabled={!cloudUrl.trim() || !!busy}
            >
              Connect
            </Button>
          </div>
        </div>
      </Card>

      <Card title="NOVA Cloud account">
        {!acct ? (
          <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
            Checking…
          </div>
        ) : !acct.configured ? (
          <div className="text-[11px]" style={{ color: theme.palette.textSecondary }}>
            This NOVA runs entirely on this computer. No account is in use.
          </div>
        ) : !acct.signed_in ? (
          <div className="text-[11px]" style={{ color: theme.palette.textSecondary }}>
            Not signed in. Restart NOVA to see the sign-in screen.
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2">
              <Stat label="Signed in as" value={acct.user?.email || '—'} />
              <Stat label="Name" value={acct.display_name || '—'} />
              <Stat label="Email" value={acct.email_verified ? 'Confirmed' : 'Not confirmed'} tone={acct.email_verified ? 'ok' : 'warn'} />
              <Stat label="Connection" value={acct.online ? 'Online' : 'Offline (cached session)'} tone={acct.online ? 'ok' : 'warn'} />
            </div>
            <Row label="Credentials stored in">
              <span className="text-xs font-mono" style={{ color: theme.palette.textSecondary }}>
                {acct.credential_backend}
                {acct.hardware_backed ? '' : ' (no OS keystore available)'}
              </span>
            </Row>
            <div className="pt-3 border-t space-y-2" style={{ borderColor: theme.palette.glassBorder }}>
              <div className="text-xs font-medium" style={{ color: theme.palette.textPrimary }}>
                Your devices
              </div>
              {devicesError && (
                <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
                  Device list needs a connection. ({devicesError})
                </div>
              )}
              {(devices || []).map((d) => (
                <div key={d.id} className="flex items-center justify-between text-xs p-2 rounded-xl border" style={{ borderColor: theme.palette.glassBorder }}>
                  <span style={{ color: theme.palette.textPrimary }}>
                    {d.name}
                    {d.current ? ' (this device)' : ''}
                    <span className="font-mono opacity-50 ml-2">
                      {d.platform} · {d.app_version || '?'}
                    </span>
                  </span>
                  {d.current ? (
                    <span className="text-[10px] opacity-60">active now</span>
                  ) : revoking === d.id ? (
                    <span className="flex gap-2">
                      <Button onClick={() => setRevoking(null)}>Cancel</Button>
                      <Button
                        tone="danger"
                        onClick={() => {
                          setRevoking(null);
                          act('Signing the device out', () => accountCall(`/api/account/devices/${encodeURIComponent(d.id)}/revoke`, 'POST').then(loadAccount), `${d.name} signed out`);
                        }}
                      >
                        Sign it out
                      </Button>
                    </span>
                  ) : (
                    <Button onClick={() => setRevoking(d.id)}>Sign out this device</Button>
                  )}
                </div>
              ))}
              {devices && devices.length === 0 && (
                <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
                  No devices listed.
                </div>
              )}
            </div>
            <div className="flex gap-2">
              <Button onClick={() => act('Syncing', () => accountCall('/api/account/sync', 'POST'), 'Preferences synced')}>Sync preferences now</Button>
              <Button onClick={() => act('Signing out', () => accountCall('/api/account/signout', 'POST').then(loadAccount), 'Signed out')}>Sign out</Button>
            </div>
          </>
        )}
      </Card>
    </div>
  );
};
