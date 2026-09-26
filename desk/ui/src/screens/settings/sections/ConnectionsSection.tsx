import React, { useState } from 'react';
import { getJSON, postJSON } from '../../../nova/api';
import { useRuntime } from '../../../nova/runtime';
import { useNova } from '../../../context/NovaStateContext';
import { Button, Card, Loading, Row, SectionHeader, Stat, useResource } from '../primitives';

interface Account {
  provider: string;
  account?: string;
  status: string;
  grants: string[];
  description: string;
  supports: string[];
  last_verified?: number;
}

const AUTH_MODE: Record<string, string> = {
  cloud: 'NOVA Cloud account',
  byok: 'Your own API key',
  env: 'Key from the environment',
  offline: 'Offline only',
  '': 'Not set up',
};

export const ConnectionsSection: React.FC = () => {
  const { theme, addToast } = useNova();
  const { status, busAttached, liveAttached, backendReachable, activity } = useRuntime();
  // Reload whenever an account_changed event lands (the OAuth outcome
  // arrives on the bus, not in the connect call's response).
  const accounts = useResource(() => getJSON<{ accounts: Account[] }>('/api/accounts', 10000), [activity.find((a) => a.kind === 'account')?.id]);
  const [busy, setBusy] = useState('');

  const connect = async (provider: string) => {
    setBusy(provider);
    try {
      await postJSON(`/api/accounts/${provider}/connect`, {});
      addToast(`Connecting ${provider}`, 'Finish signing in in the browser window that opened.', 'info');
    } catch (e) {
      addToast(`Could not connect ${provider}`, (e as Error).message, 'warning');
    } finally {
      setBusy('');
    }
  };

  const disconnect = async (provider: string) => {
    setBusy(provider);
    try {
      await postJSON(`/api/accounts/${provider}/disconnect`, {});
      addToast(`${provider} disconnected`, undefined, 'info');
      accounts.reload();
    } catch (e) {
      addToast(`Could not disconnect ${provider}`, (e as Error).message, 'warning');
    } finally {
      setBusy('');
    }
  };

  const mode = status?.auth?.mode ?? '';

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Connections" subtitle="How NOVA reaches her intelligence, and the accounts she can act on for you." />

      <Card title="Link">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <Stat label="Backend" value={backendReachable ? 'Connected' : 'Unreachable'} tone={backendReachable ? 'ok' : 'bad'} />
          <Stat label="Internet" value={status ? (status.online ? 'Online' : 'Offline') : '—'} tone={status?.online ? 'ok' : 'warn'} />
          <Stat label="Voice link" value={liveAttached ? 'Open' : 'Closed'} tone={liveAttached ? 'ok' : 'warn'} />
          <Stat label="Event bus" value={busAttached ? 'Open' : 'Closed'} tone={busAttached ? 'ok' : 'warn'} />
        </div>
        <Row label="Intelligence access" hint={status?.auth?.has_credential === false ? 'No credential: NOVA can only use local intelligence.' : undefined}>
          <span className="text-xs font-mono" style={{ color: theme.palette.textPrimary }}>
            {AUTH_MODE[mode] || mode}
          </span>
        </Row>
      </Card>

      <Card title="Accounts">
        {!accounts.data ? (
          <Loading what="accounts" error={accounts.error} />
        ) : accounts.data.accounts.length === 0 ? (
          <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
            No account integrations are available in this build.
          </div>
        ) : (
          <div className="space-y-2">
            {accounts.data.accounts.map((a) => {
              const connected = a.status === 'connected';
              return (
                <div key={a.provider} className="p-3 rounded-xl border flex items-center justify-between gap-3" style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}>
                  <div className="min-w-0">
                    <div className="text-xs font-medium capitalize" style={{ color: theme.palette.textPrimary }}>
                      {a.provider}
                      {a.account && <span className="ml-2 normal-case font-mono opacity-60">{a.account}</span>}
                    </div>
                    <div className="text-[11px] opacity-60" style={{ color: theme.palette.textSecondary }}>
                      {a.description}
                      {a.grants.length > 0 && ` · can ${a.grants.join(', ')}`}
                    </div>
                  </div>
                  {connected ? (
                    <Button disabled={busy === a.provider} onClick={() => disconnect(a.provider)}>
                      Disconnect
                    </Button>
                  ) : a.provider === 'gmail' ? (
                    <Button tone="accent" disabled={busy === a.provider} onClick={() => connect(a.provider)}>
                      Connect
                    </Button>
                  ) : (
                    <span className="text-[10px] font-mono opacity-50" style={{ color: theme.palette.textMuted }}>
                      {a.status}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
};
