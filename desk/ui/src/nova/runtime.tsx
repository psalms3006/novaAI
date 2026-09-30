// NOVA's real runtime state, for every screen.
//
//   desk/bridge.py ──HTTP + two sockets──► NovaRuntimeProvider ──► components
//
// This is the one place the interface learns what NOVA is doing. Components
// never fetch or open sockets themselves: they read the state below and call
// its actions. Nothing here invents data -- when the backend has not said
// something yet, the value is null/empty and the component says so.

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, getJSON, postJSON, streamChat } from './api';
import { NovaEventHub, type NovaEvent } from './events';
import { STATE_LABEL, VisualEngine, type VisualFrame, type VisualState } from './visual';
import type { AuthStatus } from './onboarding';

// ── backend shapes (desk/bridge.py) ──────────────────────────────────────────

export interface Vitals {
  cpu_pct?: number;
  mem_pct?: number;
  mem_used_gb?: number;
  mem_total_gb?: number;
  up_bps?: number;
  down_bps?: number;
  thermal_c?: number;
}

export interface SystemAgent {
  id: string;
  label: string;
  role: string;
  /** 'running' exactly while real work this agent owns is open (agent_activity.py). */
  state: 'standby' | 'running';
  action?: string;
  task_id?: string;
  since?: number | null;
  /** How many pieces of work it has open. */
  busy?: number;
}

export interface TaskMessage {
  id: string;
  ts: number;
  from: string;
  to: string;
  kind: 'assign' | 'result' | 'failure' | 'handoff' | 'review_feedback' | string;
  text: string;
  step: number;
}

export interface SystemTask {
  id: string;
  title: string;
  status: string;
  /** What is happening now, in words. Empty once the task has stopped. */
  phase?: string;
  steps: number;
  steps_done?: number;
  /** Steps finished out of steps planned, 0-100. Counted, never estimated. */
  progress?: number;
  /** NOVA's own spoken estimate counted down -- shown as hers, never as progress. */
  seconds_remaining?: number | null;
  estimated_duration_s?: number;
  elapsed_s?: number;
  current?: string;
  next?: string;
  agents?: string[];
  reason?: string;
  artifacts?: string[];
  messages?: TaskMessage[];
  review?: { round: number; passed: boolean; issues: string[] } | null;
  updated?: number;
}

export interface SystemSnapshot {
  ts: number;
  uptime_s: number;
  vitals: Vitals;
  agents: SystemAgent[];
  tasks: SystemTask[];
  intelligence: { provider?: string; model?: string; connectivity?: string; brain_ready?: boolean; last_turn_ms?: number };
  voice: { state?: string };
}

export interface StatusSnapshot {
  online: boolean;
  connectivity: string;
  degraded: boolean;
  model: string;
  preferred_model: string;
  serving: 'cloud' | 'local';
  has_key: boolean;
  tools: Record<string, boolean>;
  memory: { enabled: boolean; facts: number };
  voice: { available: boolean; enabled: boolean; responses: boolean };
  local_intelligence: { available: boolean; ollama_running: boolean; model: string; installed: string[]; runtime_state: string };
  brain_ready: boolean;
  user: string;
  auth?: AuthStatus;
  uptime: number;
  version: string;
}

export interface PendingConfirm {
  id: string;
  tool: string;
  prompt: string;
  created: number;
}

export interface Turn {
  id: number;
  role: 'user' | 'nova';
  text: string;
  live: boolean;
  at: number;
}

export type ActivityKind = 'voice' | 'tool' | 'result' | 'task' | 'agent' | 'vision' | 'account' | 'error' | 'system';

export interface ActivityItem {
  id: number;
  at: number;
  kind: ActivityKind;
  text: string;
}

/**
 * What NOVA is doing, in the words the interface shows. Derived from the
 * voice session, the visual engine and the backend's reachability -- never
 * set directly by a component.
 */
export type Phase =
  | 'offline' // the backend itself cannot be reached
  | 'connecting'
  | 'recovering' // voice lost its link and is reconnecting
  | 'no-network' // backend up, internet down: local intelligence only
  | 'voice-off' // backend up, no voice session running
  | 'muted'
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'interrupted'
  | 'executing'
  | 'looking' // reading the screen or the camera
  | 'researching'
  | 'awaiting-permission'
  | 'error';

export const PHASE_LABEL: Record<Phase, string> = {
  offline: 'NOVA is not reachable',
  connecting: 'Connecting',
  recovering: 'Reconnecting',
  'no-network': 'Offline · local intelligence',
  'voice-off': 'Voice is off',
  muted: 'Microphone muted',
  idle: 'Ready',
  listening: 'Hearing you',
  thinking: 'Thinking',
  speaking: 'Speaking',
  interrupted: 'Interrupted',
  executing: 'Working',
  looking: 'Looking at the screen',
  researching: 'Researching',
  'awaiting-permission': 'Waiting for your permission',
  error: 'Something went wrong',
};

/** Live-session states in which a session exists (desk/live_session.py LiveState + UI states). */
const VOICE_RUNNING = new Set(['connecting', 'connected', 'ready', 'streaming', 'listening', 'speaking', 'muted']);

/** States that prove the session actually came up (not merely that a start was requested). */
const VOICE_UP = new Set(['connected', 'ready', 'streaming', 'listening', 'speaking', 'muted']);

/** How long a start may go without the session reporting itself up. */
const VOICE_READY_TIMEOUT_MS = 20000;

const TOOL_WORDS: Record<string, string> = {
  web_search: 'Searching the web',
  search_web: 'Searching the web',
  deep_research: 'Researching',
  open_app: 'Opening an app',
  open_url: 'Opening a page',
  browser_control: 'Using the browser',
  file_controller: 'Working with files',
  computer_settings: 'Changing a setting',
  take_screenshot: 'Looking at the screen',
  look_at_screen: 'Looking at the screen',
  remember_fact: 'Remembering that',
  recall_memory: 'Recalling',
  create_task: 'Planning a task',
  run_command: 'Running a command',
};

export function toolWords(name: string): string {
  return TOOL_WORDS[name] || name.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

// ── context ──────────────────────────────────────────────────────────────────

interface Runtime {
  ambient: boolean;
  /** The engine and its latest frame, for renderers that draw every frame. */
  engine: VisualEngine;
  frame: React.MutableRefObject<VisualFrame | null>;

  backendReachable: boolean;
  liveAttached: boolean;
  busAttached: boolean;
  voiceState: string;
  voiceError: string;
  voiceRunning: boolean;
  muted: boolean;
  phase: Phase;
  phaseLabel: string;
  visualState: VisualState;
  visualLabel: string;
  taskBusy: boolean;

  status: StatusSnapshot | null;
  system: SystemSnapshot | null;
  turns: Turn[];
  activity: ActivityItem[];
  pendingConfirm: PendingConfirm | null;
  chatBusy: boolean;

  startVoice: (source?: 'user' | 'auto') => Promise<void>;
  stopVoice: () => Promise<void>;
  toggleVoice: () => Promise<void>;
  setMuted: (muted: boolean) => Promise<void>;
  interrupt: () => Promise<void>;
  sendText: (text: string) => Promise<void>;
  stopReply: () => Promise<void>;
  openConversation: (cid: string) => Promise<void>;
  newConversation: () => void;
  /** The conversation typed turns go to, or '' before the first one. */
  conversationId: string;
  halt: () => Promise<void>;
  decide: (id: string, yes: boolean) => Promise<void>;
  /** Stop a background task at its next step. */
  cancelTask: (id: string) => Promise<void>;
  /** Run a stopped task again, as a new task. */
  retryTask: (id: string) => Promise<void>;
  refreshStatus: () => Promise<void>;
  clearActivity: () => void;
}

const RuntimeContext = createContext<Runtime | null>(null);

let seq = 0;

export const NovaRuntimeProvider: React.FC<{ ambient?: boolean; children: React.ReactNode }> = ({
  ambient = false,
  children,
}) => {
  const hub = useMemo(() => new NovaEventHub({ liveSocket: !ambient }), [ambient]);
  const engine = useMemo(() => new VisualEngine(), []);
  const frame = useRef<VisualFrame | null>(null);

  const [visualState, setVisualState] = useState<VisualState>(engine.current);
  const [voiceState, setVoiceState] = useState('connecting');
  const [voiceError, setVoiceError] = useState('');
  const [muted, setMutedState] = useState(false);
  const [interruptedAt, setInterruptedAt] = useState(0);
  const [liveAttached, setLiveAttached] = useState(false);
  const [busAttached, setBusAttached] = useState(false);
  const [backendReachable, setBackendReachable] = useState(true);
  const [taskBusy, setTaskBusy] = useState(false);
  const [status, setStatus] = useState<StatusSnapshot | null>(null);
  const [system, setSystem] = useState<SystemSnapshot | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  const conversationId = useRef('');
  const failures = useRef(0);
  // Task and agent events ask for a fresh snapshot instead of waiting for the
  // next poll, so a step starting shows now, not up to 2.5 s later.
  const pokeSystem = useRef<() => void>(() => {});
  // Voice watchdog: /api/live/start answers when a thread spawns, which
  // proves nothing. If the session never reports itself up, say so rather
  // than showing "Connecting" forever.
  const voiceStateRef = useRef('connecting');
  const readyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const log = useCallback((kind: ActivityKind, text: string) => {
    if (!text) return;
    setActivity((prev) => [{ id: ++seq, at: Date.now(), kind, text }, ...prev].slice(0, 80));
  }, []);

  /** Append to the turn in progress for `role`, or start a new one. */
  const speak = useCallback((role: Turn['role'], text: string, finish = false) => {
    if (!text.trim()) return;
    setTurns((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === role && last.live) {
        const joined = (last.text + ' ' + text).replace(/\s+/g, ' ').trim();
        return [...prev.slice(0, -1), { ...last, text: joined, live: !finish }];
      }
      const settled = prev.map((t) => (t.live ? { ...t, live: false } : t));
      return [...settled, { id: ++seq, role, text: text.trim(), live: !finish, at: Date.now() }].slice(-60);
    });
  }, []);

  const settleTurns = useCallback(() => {
    setTurns((prev) => (prev.some((t) => t.live) ? prev.map((t) => ({ ...t, live: false })) : prev));
  }, []);

  // ── the frame clock: one engine tick per animation frame ─────────────────
  // Time-based transitions (listening → understanding → thinking) need the
  // clock running even when no renderer is on screen. rAF pauses by itself
  // while the window is hidden.
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const loop = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      frame.current = engine.tick(dt);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [engine]);

  /**
   * Ask the backend what the voice session is doing right now. Events only
   * say what *changed*, so a window that (re)connects to a session already
   * running -- a reload, a dropped socket -- would otherwise show
   * "Connecting" until the next change, which may never come.
   */
  const syncVoice = useCallback(async () => {
    try {
      const s = await getJSON<{ state?: string; mic?: { muted?: boolean } }>('/api/live/status', 8000);
      const st = String(s.state || '');
      if (!st) return;
      const muted = Boolean(s.mic?.muted);
      setMutedState(muted);
      const next = muted && st !== 'closed' && st !== 'idle' ? 'muted' : st === 'idle' ? 'closed' : st;
      setVoiceState(next);
      voiceStateRef.current = next;
      if (st === 'error' || st === 'closed' || st === 'idle') engine.interpret({ source: 'live', type: 'state', state: st, ts: Date.now() / 1000 });
    } catch {
      /* the next event will tell us */
    }
  }, [engine]);

  // ── events ───────────────────────────────────────────────────────────────
  useEffect(() => {
    const offState = engine.onState(setVisualState);
    let wasAttached = false;
    const offStatus = hub.onStatus(() => {
      setLiveAttached(hub.liveConnected);
      setBusAttached(hub.busConnected);
      const attached = ambient ? hub.busConnected : hub.liveConnected;
      if (attached && !wasAttached) syncVoice();
      wasAttached = attached;
    });
    const off = hub.on((raw: NovaEvent) => {
      // The ambient window hears voice only through the bus, where the live
      // session's events arrive under relay names. Translate them back so
      // one interpreter serves both windows.
      let ev = raw;
      if (raw.source === 'bus' && ambient) {
        if (raw.type === 'voice_state') ev = { ...raw, type: 'state' };
        else if (raw.type === 'transcript') ev = { ...raw, type: raw.role === 'user' ? 'user_transcript' : 'nova_transcript' };
      }
      engine.interpret(ev);
      switch (ev.type) {
        case 'state': {
          const s = String(ev.state || '');
          setVoiceState(s);
          voiceStateRef.current = s;
          if (VOICE_UP.has(s) && readyTimer.current) {
            clearTimeout(readyTimer.current);
            readyTimer.current = null;
          }
          if (s === 'muted') setMutedState(true);
          else if (s === 'listening' || s === 'ready' || s === 'connected') setMutedState(false);
          if (s === 'error') {
            const msg = String(ev.message || ev.error || 'Voice error');
            setVoiceError(msg);
            log('error', `Voice: ${msg}`);
          } else if (s === 'ready' || s === 'listening' || s === 'connected') {
            setVoiceError('');
          }
          if (s === 'offline') log('voice', 'No network — voice paused until it returns');
          if (s === 'connecting' && ev.retry_in_s) log('voice', `Voice link lost — retrying in ${Number(ev.retry_in_s).toFixed(0)}s`);
          break;
        }
        case 'user_transcript':
          speak('user', String(ev.text || ''));
          break;
        case 'nova_transcript':
          speak('nova', String(ev.text || ''));
          break;
        case 'turn_complete':
          settleTurns();
          break;
        case 'interrupted':
          settleTurns();
          setInterruptedAt(Date.now());
          log('voice', ev.reason === 'barge_in' ? 'You interrupted her' : 'Reply interrupted');
          break;
        case 'tool_call':
          for (const t of (ev.tools as string[]) || []) log('tool', toolWords(t));
          break;
        case 'tool_result':
          log('result', `${toolWords(String(ev.tool || ''))} — ${String(ev.summary || 'done').slice(0, 140)}`);
          break;
        case 'task_start':
          log('task', `Started: ${String(ev.label || ev.task_id || 'a task')}`);
          break;
        case 'task_done':
          log('task', `${ev.ok === false ? 'Failed' : 'Finished'}${ev.summary ? `: ${String(ev.summary).slice(0, 140)}` : ''}`);
          break;
        case 'agent_start':
          log('agent', String(ev.action || ev.name || 'Agent working'));
          break;
        case 'agent_done':
          if (ev.ok === false) log('agent', `Agent stopped: ${String(ev.summary || '')}`);
          break;
        case 'task_activity':
          setTaskBusy(Boolean(ev.busy));
          break;
        case 'task.created':
          log('task', `Queued: ${String((ev.task as { title?: string })?.title || '')}`);
          pokeSystem.current();
          break;
        case 'task.planned':
          log('task', `Planned — ${String(ev.detail || '')}`);
          pokeSystem.current();
          break;
        case 'task.waiting':
          log('task', String(ev.detail || 'Waiting for a shared resource'));
          pokeSystem.current();
          break;
        case 'task.completed':
        case 'task.failed':
        case 'task.finished':
        case 'task.cancelled': {
          const title = String((ev.task as { title?: string })?.title || 'Task');
          const word = ev.type === 'task.completed' ? 'Completed' : ev.type === 'task.cancelled' ? 'Cancelled' : 'Stopped';
          log(ev.type === 'task.failed' ? 'error' : 'task', `${word}: ${title}${ev.type !== 'task.completed' && ev.detail ? ` — ${String(ev.detail)}` : ''}`);
          pokeSystem.current();
          break;
        }
        case 'agent.message': {
          // Only the hand-offs worth reading; assignments and results are
          // already visible as the task's own steps.
          const m = ev.message as { kind?: string; from?: string; to?: string; text?: string } | undefined;
          if (m && (m.kind === 'handoff' || m.kind === 'review_feedback')) {
            log('agent', `${String(m.from).toUpperCase()} → ${String(m.to).toUpperCase()}: ${String(m.text || '')}`);
          }
          pokeSystem.current();
          break;
        }
        case 'review.completed':
          log('task', `Review: ${String(ev.detail || '')}`);
          pokeSystem.current();
          break;
        case 'task.step':
        case 'task.progress':
        case 'task.status':
        case 'task.started':
        case 'artifact.created':
        case 'agent.state':
          pokeSystem.current();
          break;
        case 'vision_capture':
          log('vision', 'Looking at the screen');
          break;
        case 'vision_failed':
          log('error', `Could not see the screen: ${String(ev.error || '')}`);
          break;
        case 'screen_share':
          log('vision', ev.watching ? 'Watching your screen' : 'Stopped watching your screen');
          break;
        case 'account_changed':
          log('account', `${String(ev.provider || 'Account')} ${ev.ok ? 'connected' : 'not connected'}${ev.error ? `: ${String(ev.error)}` : ''}`);
          break;
        case 'error':
          log('error', String(ev.message || ev.error || 'Error'));
          break;
      }
    });
    hub.start();
    return () => {
      if (readyTimer.current) clearTimeout(readyTimer.current);
      off();
      offState();
      offStatus();
      hub.stop();
    };
  }, [hub, engine, ambient, log, speak, settleTurns, syncVoice]);

  // ── polled state ─────────────────────────────────────────────────────────
  const refreshStatus = useCallback(async () => {
    try {
      const s = await getJSON<StatusSnapshot>('/api/status', 15000);
      setStatus(s);
      engine.setOffline(!s.online);
    } catch {
      /* keep the last known status; reachability is tracked by the poll */
    }
  }, [engine]);

  useEffect(() => {
    let alive = true;
    const pollSystem = async () => {
      if (document.hidden) return;
      try {
        const s = await getJSON<SystemSnapshot>('/api/system', 8000);
        if (!alive) return;
        failures.current = 0;
        setBackendReachable(true);
        setSystem(s);
      } catch {
        if (!alive) return;
        // Two misses in a row, not one: a single slow reply is not an outage.
        if (++failures.current >= 2) setBackendReachable(false);
      }
    };
    const pollConfirm = async () => {
      try {
        const j = await getJSON<{ pending: PendingConfirm[] }>('/api/confirm/pending', 5000);
        if (alive) setPendingConfirm(j.pending?.[0] ?? null);
      } catch {
        /* next tick */
      }
    };
    let poked: ReturnType<typeof setTimeout> | null = null;
    pokeSystem.current = () => {
      if (poked) return;
      poked = setTimeout(() => {
        poked = null;
        pollSystem();
      }, 200);
    };
    pollSystem();
    refreshStatus();
    const a = setInterval(pollSystem, ambient ? 5000 : 2500);
    const b = setInterval(refreshStatus, 15000);
    // Confirmations are never pushed, only polled -- and polled even while
    // hidden: NOVA may be blocked on the user's agreement, and the prompt
    // must be there when they come back. The ambient window leaves this to
    // the main one, so a prompt never appears twice.
    let c: ReturnType<typeof setInterval> | null = null;
    if (!ambient) {
      pollConfirm();
      c = setInterval(pollConfirm, 2500);
    }
    return () => {
      alive = false;
      if (poked) clearTimeout(poked);
      pokeSystem.current = () => {};
      clearInterval(a);
      clearInterval(b);
      if (c) clearInterval(c);
    };
  }, [refreshStatus, ambient]);

  // ── voice actions ─────────────────────────────────────────────────────────
  const startVoice = useCallback(async (source: 'user' | 'auto' = 'user') => {
    try {
      // `source` lets the backend honour "Microphone: Ask me" -- voice starts
      // when the person presses the mic, never on its own.
      const j = await postJSON<{ ok: boolean; reason?: string; message?: string; error?: string }>('/api/live/start', { source });
      if (!j.ok && j.error === 'microphone_ask') {
        setVoiceError('');
        setVoiceState('closed');
        voiceStateRef.current = 'closed';
        log('system', j.message || 'Microphone is set to Ask me: press the mic to start voice.');
        return;
      }
      if (!j.ok) {
        const why = j.reason || 'Voice could not start';
        setVoiceError(why);
        setVoiceState('error');
        log('error', `Voice could not start: ${why}`);
      } else if (j.message === 'already running') {
        // No state event follows for a session that is already up.
        await syncVoice();
      } else {
        if (readyTimer.current) clearTimeout(readyTimer.current);
        readyTimer.current = setTimeout(async () => {
          readyTimer.current = null;
          await syncVoice();
          if (!VOICE_UP.has(voiceStateRef.current)) {
            const why = 'Voice did not come up. Check your key in Settings → Account & Access, and the microphone in Voice Session.';
            setVoiceError(why);
            setVoiceState('error');
            voiceStateRef.current = 'error';
            log('error', why);
          }
        }, VOICE_READY_TIMEOUT_MS);
      }
    } catch (e) {
      // Refused (microphone set to Never, no key, ...): say why and settle in a
      // stopped state. Leaving it at 'connecting' made the mic button act as
      // mute on a session that did not exist and show "Hearing you".
      const why = (e as Error).message || 'Voice could not start';
      setVoiceError(why);
      setVoiceState('error');
      voiceStateRef.current = 'error';
      log('error', `Voice could not start: ${why}`);
    }
  }, [log, syncVoice]);

  const stopVoice = useCallback(async () => {
    try {
      await postJSON('/api/live/stop', {});
    } catch (e) {
      log('error', `Voice could not stop: ${(e as Error).message}`);
    }
  }, [log]);

  const voiceRunning = VOICE_RUNNING.has(voiceState);

  const toggleVoice = useCallback(async () => {
    if (voiceRunning) await stopVoice();
    else await startVoice();
  }, [voiceRunning, startVoice, stopVoice]);

  // Voice comes up with the interface, as it always has, unless the user
  // turned voice off in Settings. The live socket is opened first (above) so
  // the greeting finds an interface attached. The ambient window never
  // starts or stops voice: the main window owns the session.
  useEffect(() => {
    if (ambient) return;
    let cancelled = false;
    const t = setTimeout(async () => {
      try {
        const j = await getJSON<{ settings: { voice_enabled?: boolean } }>('/api/settings', 8000);
        if (!cancelled && j.settings?.voice_enabled !== false) await startVoice('auto');
        else if (!cancelled) setVoiceState('closed');
      } catch {
        if (!cancelled) await startVoice('auto');
      }
    }, 1200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [ambient, startVoice]);

  const setMuted = useCallback(
    async (m: boolean) => {
      try {
        const j = await postJSON<{ muted: boolean }>('/api/live/mute', { muted: m });
        setMutedState(Boolean(j.muted));
      } catch (e) {
        log('error', `Could not ${m ? 'mute' : 'unmute'}: ${(e as Error).message}`);
      }
    },
    [log],
  );

  const interrupt = useCallback(async () => {
    try {
      await postJSON('/api/live/interrupt', { notify_model: true });
    } catch {
      /* the worst case is she finishes her sentence */
    }
  }, []);

  // ── typed conversation ────────────────────────────────────────────────────
  const sendText = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      if (!text) return;
      // With a live session, typing joins the spoken conversation: she
      // answers out loud and the reply arrives on the voice socket.
      if (hub.liveConnected && voiceRunning && voiceState !== 'connecting') {
        try {
          if (engine.current === 'SPEAKING') await postJSON('/api/live/interrupt', { notify_model: false });
          const j = await postJSON<{ ok?: boolean }>('/api/live/text', { message: text });
          if (j.ok) {
            speak('user', text, true);
            engine.chat('sent');
            return;
          }
        } catch {
          /* fall through to the text model */
        }
      }
      // Otherwise the text model (cloud, or local when offline).
      setChatBusy(true);
      speak('user', text, true);
      engine.chat('sent');
      let reply = '';
      const showReply = () =>
        setTurns((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === 'nova' && last.live) return [...prev.slice(0, -1), { ...last, text: reply }];
          return [...prev, { id: ++seq, role: 'nova' as const, text: reply, live: true, at: Date.now() }].slice(-60);
        });
      try {
        // One conversation for the session's typed turns: /api/chat with no
        // id starts a fresh conversation per message and never reports it.
        if (!conversationId.current) {
          const c = await postJSON<{ id: string }>('/api/conversations', {});
          conversationId.current = c.id;
        }
        await streamChat({ conversation_id: conversationId.current, message: text, image_path: '' }, (ev) => {
          if (ev.type === 'token' && ev.text) {
            reply += ev.text;
            engine.chat('token');
            showReply();
          } else if (ev.type === 'tool_start') {
            engine.chat('tool_start', String(ev.name || ''));
            log('tool', ev.label || toolWords(String(ev.name || '')));
          } else if (ev.type === 'tool_done') {
            engine.chat('tool_done');
          } else if (ev.type === 'status' && ev.label) {
            log('system', ev.label);
          } else if (ev.type === 'assistant' && ev.text) {
            reply = String(ev.text);
            showReply();
          } else if (ev.type === 'error') {
            engine.chat('error');
            log('error', String(ev.message || 'Chat error'));
          }
        });
        engine.chat('done');
      } catch (e) {
        engine.chat('error');
        log('error', e instanceof ApiError ? e.message : `Could not reach NOVA: ${(e as Error).message}`);
      } finally {
        settleTurns();
        setChatBusy(false);
      }
    },
    [hub, engine, voiceRunning, voiceState, speak, settleTurns, log],
  );

  /**
   * Pick up a saved conversation: its messages become the transcript, and
   * what the user types next continues it (the text model gets its history).
   */
  const openConversation = useCallback(
    async (cid: string) => {
      const c = await getJSON<{ id: string; messages: { role: string; content: string; ts: number }[] }>(`/api/conversations/${encodeURIComponent(cid)}`, 15000);
      conversationId.current = c.id;
      setTurns(
        (c.messages || [])
          .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.content)
          .slice(-60)
          .map((m) => ({ id: ++seq, role: m.role === 'user' ? ('user' as const) : ('nova' as const), text: m.content, live: false, at: (m.ts || 0) * 1000 })),
      );
    },
    [],
  );

  /** Start afresh: the next typed message opens a new conversation. */
  const newConversation = useCallback(() => {
    conversationId.current = '';
    setTurns([]);
  }, []);

  const stopReply = useCallback(async () => {
    if (!conversationId.current) return;
    try {
      await postJSON('/api/chat/stop', { conversation_id: conversationId.current });
    } catch {
      /* the stream ends on its own */
    }
  }, []);

  /**
   * Stop everything the user can see NOVA doing right now: her speech, the
   * typed reply being written, and the microphone. Background tasks are
   * stopped from their own row (Presence → Tasks → Stop), and the label in
   * the interface says exactly this.
   */
  const halt = useCallback(async () => {
    await Promise.allSettled([interrupt(), stopReply(), voiceRunning ? setMuted(true) : Promise.resolve()]);
    log('system', 'Stopped speaking and replying; microphone muted');
  }, [interrupt, stopReply, setMuted, voiceRunning, log]);

  const taskAction = useCallback(
    async (id: string, action: 'cancel' | 'retry') => {
      try {
        const j = await postJSON<{ ok: boolean; error?: string; task_id?: string }>(`/api/tasks/${encodeURIComponent(id)}/${action}`, {});
        if (!j.ok) log('error', j.error || `Could not ${action} the task`);
        else log('task', action === 'cancel' ? 'Stopping the task after its current step' : `Retrying as ${j.task_id}`);
      } catch (e) {
        log('error', `Could not ${action} the task: ${(e as Error).message}`);
      }
      pokeSystem.current();
    },
    [log],
  );
  const cancelTask = useCallback((id: string) => taskAction(id, 'cancel'), [taskAction]);
  const retryTask = useCallback((id: string) => taskAction(id, 'retry'), [taskAction]);

  const decide = useCallback(
    async (id: string, yes: boolean) => {
      setPendingConfirm(null);
      try {
        await postJSON('/api/confirm', { id, decision: yes ? 'yes' : 'no' });
        log('system', yes ? 'You allowed the action' : 'You declined the action');
      } catch (e) {
        log('error', `Could not send your decision: ${(e as Error).message}`);
      }
    },
    [log],
  );

  // ── the phase the interface shows ─────────────────────────────────────────
  const [, forceTick] = useState(0);
  useEffect(() => {
    // "Interrupted" is a moment, not a state: re-derive once it has passed.
    if (!interruptedAt) return;
    const t = setTimeout(() => forceTick((n) => n + 1), 1300);
    return () => clearTimeout(t);
  }, [interruptedAt]);

  const phase: Phase = (() => {
    if (!backendReachable) return 'offline';
    if (pendingConfirm) return 'awaiting-permission';
    if (voiceState === 'error') return 'error';
    if (voiceState === 'offline' || (status && !status.online)) {
      if (!voiceRunning && !chatBusy) return 'no-network';
    }
    if (Date.now() - interruptedAt < 1200) return 'interrupted';
    if (voiceState === 'connecting') return turns.length || liveAttached ? 'recovering' : 'connecting';
    switch (visualState) {
      case 'SPEAKING':
        return 'speaking';
      case 'LISTENING':
        return 'listening';
      case 'SEARCHING':
        return 'researching';
      case 'VISION':
        return 'looking';
      case 'TOOL_SELECTION':
      case 'TOOL_EXECUTION':
        return 'executing';
      case 'UNDERSTANDING':
      case 'THINKING':
      case 'MEMORY_RETRIEVAL':
      case 'GENERATING':
        return 'thinking';
      case 'ERROR':
        return 'error';
    }
    if (chatBusy || taskBusy) return 'thinking';
    if (muted) return 'muted';
    if (!voiceRunning) return 'voice-off';
    return 'idle';
  })();

  const value: Runtime = {
    ambient,
    engine,
    frame,
    backendReachable,
    liveAttached,
    busAttached,
    voiceState,
    voiceError,
    voiceRunning,
    muted,
    phase,
    phaseLabel: phase === 'error' && voiceError ? voiceError : PHASE_LABEL[phase],
    visualState,
    visualLabel: STATE_LABEL[visualState],
    taskBusy,
    status,
    system,
    turns,
    activity,
    pendingConfirm,
    chatBusy,
    startVoice,
    stopVoice,
    toggleVoice,
    setMuted,
    interrupt,
    sendText,
    stopReply,
    openConversation,
    newConversation,
    conversationId: conversationId.current,
    halt,
    decide,
    cancelTask,
    retryTask,
    refreshStatus,
    clearActivity: () => setActivity([]),
  };
  return <RuntimeContext.Provider value={value}>{children}</RuntimeContext.Provider>;
};

export function useRuntime(): Runtime {
  const ctx = useContext(RuntimeContext);
  if (!ctx) throw new Error('useRuntime must be used inside <NovaRuntimeProvider>');
  return ctx;
}

// ── formatting helpers shared by the screens ──────────────────────────────────

export function fmtBps(bps?: number): string {
  if (bps == null) return '—';
  if (bps < 1024) return `${bps.toFixed(0)} B/s`;
  if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} KB/s`;
  return `${(bps / 1024 / 1024).toFixed(1)} MB/s`;
}

export function fmtPct(v?: number): string {
  return v == null ? '—' : `${v.toFixed(0)}%`;
}

export function fmtAgo(ts: number): string {
  const s = Math.max(0, (Date.now() - ts) / 1000);
  if (s < 5) return 'now';
  if (s < 60) return `${s.toFixed(0)}s ago`;
  if (s < 3600) return `${(s / 60).toFixed(0)}m ago`;
  return `${(s / 3600).toFixed(0)}h ago`;
}
