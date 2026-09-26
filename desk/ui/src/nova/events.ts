// Real-time events from NOVA, from the two sockets the backend exposes:
//
//   /api/live/ws  the voice session itself -- state, audio levels, both
//                 transcripts, tool calls/results, vision, interruptions.
//                 Being subscribed here is also what tells the session an
//                 interface is attached (the greeting waits for one).
//   /ws/events    the backend bus -- agent and task lifecycle from the chat
//                 path and the task manager.
//
// The bus also re-broadcasts voice events it relays from the live session
// (voice_state, orb_state, transcript, tool_call, ...). Taking those from both
// sockets would double every event, so each type has exactly one source.

import { socketUrl } from './api';

export interface NovaEvent {
  source: 'live' | 'bus';
  type: string;
  ts: number;
  [key: string]: unknown;
}

type Listener = (ev: NovaEvent) => void;

/** Types taken from the bus. Everything voice-related comes from /api/live/ws. */
const BUS_TYPES = new Set([
  'agent_start',
  'agent_progress',
  'agent_done',
  'task_start',
  'task_done',
  'account_changed',
  // Bus-only: the background task manager's busy flag and the ambient
  // window switching. Neither is ever relayed through the live socket.
  'task_activity',
  'ambient',
]);

class Channel {
  private ws: WebSocket | null = null;
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  connected = false;

  constructor(
    private readonly path: string,
    private readonly source: 'live' | 'bus',
    private readonly accept: (type: string) => boolean,
    private readonly emit: (ev: NovaEvent) => void,
    private readonly onStatus: () => void,
  ) {}

  open(): void {
    if (this.closed) return;
    let ws: WebSocket;
    try {
      ws = new WebSocket(socketUrl(this.path));
    } catch {
      this.schedule();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.connected = true;
      this.onStatus();
    };
    ws.onmessage = (msg) => {
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(String(msg.data));
      } catch {
        return;
      }
      const type = String(data.type || '');
      if (!type || type === 'ping' || !this.accept(type)) return;
      this.emit({ ...data, source: this.source, type, ts: Number(data.ts) || Date.now() / 1000 });
    };
    const dropped = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.connected) {
        this.connected = false;
        this.onStatus();
      }
      this.schedule();
    };
    ws.onclose = dropped;
    ws.onerror = dropped;
  }

  private schedule(): void {
    if (this.closed || this.timer) return;
    // 0.5 s, 1 s, 2 s ... capped at 8 s: back quickly after a blip without
    // hammering a backend that is restarting.
    const delay = Math.min(8000, 500 * 2 ** this.retry++);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.open();
    }, delay);
  }

  close(): void {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.connected = false;
  }
}

export class NovaEventHub {
  private listeners = new Set<Listener>();
  private statusListeners = new Set<() => void>();
  private live: Channel;
  private bus: Channel;

  /**
   * `liveSocket: false` is for the ambient window. The backend copies voice
   * events onto the bus once per open live socket, so a second window with
   * its own live socket would double every event for everyone. That window
   * listens to the bus alone and takes everything from it.
   */
  constructor(private readonly opts: { liveSocket: boolean } = { liveSocket: true }) {
    const emit = (ev: NovaEvent) => {
      for (const l of this.listeners) {
        try {
          l(ev);
        } catch (e) {
          // A broken listener (a visual effect, say) must never stop the
          // events reaching the rest of the interface.
          console.error('[nova] event listener failed', e);
        }
      }
    };
    const status = () => this.statusListeners.forEach((l) => l());
    this.live = new Channel('/api/live/ws', 'live', () => true, emit, status);
    const busAccepts = opts.liveSocket ? (t: string) => BUS_TYPES.has(t) : () => true;
    this.bus = new Channel('/ws/events', 'bus', busAccepts, emit, status);
  }

  start(): void {
    if (this.opts.liveSocket) this.live.open();
    this.bus.open();
  }

  stop(): void {
    this.live.close();
    this.bus.close();
  }

  get liveConnected(): boolean {
    return this.live.connected;
  }

  get busConnected(): boolean {
    return this.bus.connected;
  }

  on(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onStatus(listener: () => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }
}
