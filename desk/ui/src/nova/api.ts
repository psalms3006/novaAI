// The one place the UI talks HTTP to NOVA's backend (desk/bridge.py).
//
// Every route is token-guarded: the backend injects the per-run token into
// the page (window.DESK_TOKEN) and checks it on each request, so a random
// local page cannot drive NOVA.

declare global {
  interface Window {
    DESK_TOKEN?: string;
    DESK_VERSION?: string;
  }
}

export const TOKEN: string = window.DESK_TOKEN && !window.DESK_TOKEN.startsWith('__') ? window.DESK_TOKEN : '';
export const VERSION: string =
  window.DESK_VERSION && !window.DESK_VERSION.startsWith('__') ? window.DESK_VERSION : 'dev';

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export async function api(path: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<Response> {
  const { timeoutMs = 20000, headers, ...rest } = init;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(path, {
      ...rest,
      signal: rest.signal ?? ctrl.signal,
      headers: { 'Content-Type': 'application/json', 'X-NOVA-Desk': TOKEN, ...(headers || {}) },
    });
  } finally {
    clearTimeout(timer);
  }
}

export async function getJSON<T = unknown>(path: string, timeoutMs?: number): Promise<T> {
  const r = await api(path, { timeoutMs });
  if (!r.ok) throw new ApiError(`${path} → ${r.status}`, r.status);
  return (await r.json()) as T;
}

export async function postJSON<T = unknown>(path: string, body: unknown = {}, method = 'POST'): Promise<T> {
  const r = await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  let data: unknown = null;
  try {
    data = await r.json();
  } catch {
    /* empty body */
  }
  if (!r.ok) {
    const msg = (data as { error?: string } | null)?.error || `${path} → ${r.status}`;
    throw new ApiError(msg, r.status);
  }
  return data as T;
}

/** Events the text-chat endpoint streams back, one `data:` line each. */
export interface ChatEvent {
  type: string;
  text?: string;
  name?: string;
  label?: string;
  message?: string;
  conversation_id?: string;
}

/** POST /api/chat and read its server-sent events until the turn ends. */
export async function streamChat(body: Record<string, unknown>, onEvent: (ev: ChatEvent) => void): Promise<void> {
  const r = await api('/api/chat', { method: 'POST', body: JSON.stringify(body), timeoutMs: 180000 });
  if (!r.ok || !r.body) {
    let msg = 'Chat request failed';
    try {
      msg = ((await r.json()) as { error?: string }).error || msg;
    } catch {
      /* noop */
    }
    throw new ApiError(msg, r.status);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      try {
        onEvent(JSON.parse(line.slice(6)) as ChatEvent);
      } catch {
        /* a malformed line is skipped, not fatal */
      }
    }
  }
}

export function socketUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}${path}?token=${encodeURIComponent(TOKEN)}`;
}
