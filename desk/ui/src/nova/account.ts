// NOVA Cloud account (desk/account_api.py). Local-first: when no NOVA Cloud
// backend is configured, `configured` is false and nothing here ever shows.

import { api } from './api';

export interface AccountState {
  configured: boolean;
  signed_in: boolean;
  online?: boolean;
  user?: { email?: string; id?: string } | null;
  display_name?: string;
  device_id?: string;
  credential_backend?: string;
  hardware_backed?: boolean;
  email_verified?: boolean;
  verification_required?: boolean;
  email_delivery?: { sent?: boolean };
  message?: string;
}

export interface AccountDevice {
  id: string;
  name: string;
  platform: string;
  app_version?: string;
  current?: boolean;
}

/** Account routes answer errors as {ok:false, error:<code>, message}; surface the message. */
export async function accountCall<T = AccountState>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const r = await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body), timeoutMs: 30000 });
  let j: Record<string, unknown> = {};
  try {
    j = await r.json();
  } catch {
    /* empty */
  }
  if (!r.ok || j.ok === false) throw new Error(String(j.message || j.error || `Request failed (${r.status})`));
  return j as T;
}

export const getAccount = () => accountCall<AccountState>('/api/account');
