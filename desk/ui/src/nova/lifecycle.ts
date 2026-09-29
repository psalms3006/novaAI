// First-run lifecycle (desk/account_api.py /api/lifecycle). The backend
// decides what comes next from separate facts -- signed in, email verified,
// account set up (server), this PC set up (local) -- never from the version.

import { accountCall } from './account';
import { getJSON } from './api';

export type LifecycleNext = 'sign_in' | 'verify_email' | 'profile' | 'device_setup' | 'none';

export interface Lifecycle {
  ok: boolean;
  accounts_enabled: boolean;
  first_run: boolean;
  next: LifecycleNext;
  signed_in?: boolean;
  session_expired?: boolean;
  online?: boolean;
  email_verified?: boolean;
  instance?: { id?: string; profile?: { preferred_name?: string; role?: string; about?: string } };
  brain?: { started?: boolean; ready?: boolean; error?: string };
}

export interface SetupStep {
  id: string;
  label: string;
  ok: boolean;
  detail?: string;
  error?: string;
}

export interface OfflineState {
  state: 'idle' | 'downloading' | 'verifying' | 'ready' | 'failed' | 'cancelled' | 'runtime_missing';
  model: string;
  completed_bytes: number;
  total_bytes: number;
  percent: number | null;
  status_text: string;
  error: string;
}

export interface OfflineModelInfo {
  ok: boolean;
  recommended?: string;
  models: { id: string; name?: string; size_bytes: number; min_ram_gb?: number; note?: string; fits: boolean; installed: boolean }[];
  ram_gb: number | null;
  runtime_available: boolean;
  state: OfflineState;
}

export const getLifecycle = () => getJSON<Lifecycle>('/api/lifecycle', 20000);

export const saveProfile = (body: { preferred_name?: string; role?: string; about?: string }) =>
  accountCall<Lifecycle>('/api/lifecycle/profile', 'POST', body);

export const completeSetup = (body: { permissions: Record<string, string>; startup_mode: string; offline_model: string }) =>
  accountCall<Lifecycle & { steps: SetupStep[] }>('/api/lifecycle/complete', 'POST', body);

export const getOfflineModel = () => getJSON<OfflineModelInfo>('/api/offline-model', 20000);

export const startOfflineDownload = (model: string) =>
  accountCall<{ ok: boolean; state: OfflineState }>('/api/offline-model/download', 'POST', { model });

export const cancelOfflineDownload = () => accountCall<{ ok: boolean; state: OfflineState }>('/api/offline-model/cancel', 'POST');

export function formatBytes(n: number): string {
  if (!n) return '0 MB';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  return `${Math.round(n / 1e6)} MB`;
}
