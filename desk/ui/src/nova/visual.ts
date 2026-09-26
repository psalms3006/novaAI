// NOVA's visual state engine: real events in, smoothly blended visual
// parameters out.
//
//   NOVA events ──► interpret() ──► semantic state + impulses
//                                         │
//                         tick(dt) ──► eased parameters ──► presence renderer
//
// interpret() is the semantic layer. It never says "play animation X"; it
// says what is happening ("she is looking something up on the web") and the
// renderer decides how that looks. A new kind of event needs one entry here,
// not a new animation.
//
// Every state names a *target* for each parameter, and tick() eases towards
// it at a per-parameter rate. State changes therefore blend -- the
// listening energy decays while the thinking pathways come up -- instead of
// cutting between scenes.

import type { NovaEvent } from './events';

export type VisualState =
  | 'AWAKENING'
  | 'IDLE'
  | 'LISTENING'
  | 'UNDERSTANDING'
  | 'THINKING'
  | 'MEMORY_RETRIEVAL'
  | 'TOOL_SELECTION'
  | 'TOOL_EXECUTION'
  | 'VISION'
  | 'SEARCHING'
  | 'GENERATING'
  | 'SPEAKING'
  | 'SUCCESS'
  | 'ERROR'
  | 'SLEEPING';

export type ToolKind = 'search' | 'vision' | 'file' | 'memory' | 'system' | 'task' | 'other';

/** Everything the renderer draws from. All 0..1 unless noted. */
export interface VisualParams {
  assemble: number; // particles converged into the silhouette
  energy: number; // overall liveliness of the form
  focus: number; // attention: particles lean inward, rings tighten
  core: number; // brightness of the amber core
  coreRhythm: number; // how much the core pulses with speech
  think: number; // energy pathways travelling through the body
  memory: number; // periphery drawn inward toward the core
  tool: number; // a directed pathway out of the core is active
  vision: number; // scanning field across the head
  speak: number; // upward rhythmic energy through the centre
  listenRings: number; // concentric rings drawn in toward her
  speakRings: number; // concentric rings pushed out from her
  terrain: number; // environmental response of the ground field
  error: number; // disturbance
  sleep: number; // dimmed, resting
  offline: number; // running on local intelligence: localised energy
}

export interface Impulse {
  kind: 'ripple' | 'path_out' | 'path_return' | 'success' | 'error' | 'noticed';
  at: number; // seconds, engine clock
  tool?: ToolKind;
  strength?: number;
}

export interface VisualFrame {
  state: VisualState;
  params: VisualParams;
  novaAudio: number; // NOVA's voice, smoothed
  userAudio: number; // the user's voice, smoothed
  bands: number[]; // NOVA's spectrum (8), smoothed
  toolKind: ToolKind;
  toolLabel: string;
  impulses: Impulse[];
  time: number;
}

const BASE: VisualParams = {
  assemble: 1,
  energy: 0.25,
  focus: 0,
  core: 0.35,
  coreRhythm: 0,
  think: 0,
  memory: 0,
  tool: 0,
  vision: 0,
  speak: 0,
  listenRings: 0,
  speakRings: 0,
  terrain: 0.2,
  error: 0,
  sleep: 0,
  offline: 0,
};

const TARGETS: Record<VisualState, Partial<VisualParams>> = {
  AWAKENING: { assemble: 1, energy: 0.55, core: 0.7, terrain: 0.45 },
  IDLE: {},
  LISTENING: { energy: 0.4, focus: 0.85, core: 0.55, listenRings: 1, terrain: 0.3 },
  UNDERSTANDING: { energy: 0.35, focus: 0.6, core: 0.6, think: 0.45, listenRings: 0.25 },
  THINKING: { energy: 0.4, focus: 0.45, core: 0.65, think: 1, terrain: 0.3 },
  MEMORY_RETRIEVAL: { energy: 0.35, focus: 0.5, core: 0.7, think: 0.5, memory: 1 },
  TOOL_SELECTION: { energy: 0.45, focus: 0.5, core: 0.75, think: 0.8, tool: 0.4 },
  TOOL_EXECUTION: { energy: 0.5, focus: 0.35, core: 0.8, think: 0.55, tool: 1, terrain: 0.45 },
  VISION: { energy: 0.45, focus: 0.55, core: 0.7, vision: 1, tool: 0.6 },
  SEARCHING: { energy: 0.5, focus: 0.3, core: 0.8, think: 0.5, tool: 1, terrain: 0.55 },
  GENERATING: { energy: 0.45, core: 0.75, think: 0.6, speak: 0.35 },
  SPEAKING: { energy: 0.55, core: 0.85, coreRhythm: 1, speak: 1, speakRings: 1, terrain: 0.4 },
  SUCCESS: { energy: 0.5, core: 0.9, terrain: 0.4 },
  ERROR: { energy: 0.3, core: 0.3, error: 1 },
  SLEEPING: { energy: 0.08, core: 0.12, sleep: 1, terrain: 0.08 },
};

/** Seconds for a parameter to cover ~63% of the way to its target. */
const TAU: Partial<Record<keyof VisualParams, number>> = {
  assemble: 1.6,
  core: 0.45,
  speak: 0.35,
  coreRhythm: 0.4,
  listenRings: 0.5,
  speakRings: 0.6,
  error: 0.25,
  tool: 0.5,
  memory: 0.9,
  terrain: 1.4,
  sleep: 1.2,
  offline: 2.0,
};

const TOOL_KINDS: Record<string, ToolKind> = {
  web_search: 'search',
  learn_resource: 'search',
  browser_control: 'search',
  fetch_url: 'search',
  vision: 'vision',
  file_controller: 'file',
  file_processor: 'file',
  generate_document: 'file',
  nova_memory: 'memory',
  remember_fact: 'memory',
  open_app: 'system',
  close_app: 'system',
  app_control: 'system',
  computer_control: 'system',
  computer_settings: 'system',
  desktop_control: 'system',
  autostart: 'system',
  nova_task: 'task',
  planner: 'task',
};

export function toolKind(name: string): ToolKind {
  if (TOOL_KINDS[name]) return TOOL_KINDS[name];
  const n = name.toLowerCase();
  if (n.includes('search') || n.includes('web') || n.includes('browser')) return 'search';
  if (n.includes('vision') || n.includes('screen') || n.includes('camera')) return 'vision';
  if (n.includes('file') || n.includes('doc')) return 'file';
  if (n.includes('memory') || n.includes('remember')) return 'memory';
  if (n.includes('task') || n.includes('plan')) return 'task';
  return 'other';
}

const KIND_STATE: Record<ToolKind, VisualState> = {
  search: 'SEARCHING',
  vision: 'VISION',
  memory: 'MEMORY_RETRIEVAL',
  file: 'TOOL_EXECUTION',
  system: 'TOOL_EXECUTION',
  task: 'TOOL_EXECUTION',
  other: 'TOOL_EXECUTION',
};

/** Human wording of a state for the HUD: what she is doing, not an enum. */
export const STATE_LABEL: Record<VisualState, string> = {
  AWAKENING: 'Coming online',
  IDLE: 'Present',
  LISTENING: 'Listening',
  UNDERSTANDING: 'Understanding',
  THINKING: 'Thinking',
  MEMORY_RETRIEVAL: 'Recalling',
  TOOL_SELECTION: 'Choosing a tool',
  TOOL_EXECUTION: 'Working',
  VISION: 'Looking',
  SEARCHING: 'Searching',
  GENERATING: 'Composing',
  SPEAKING: 'Speaking',
  SUCCESS: 'Done',
  ERROR: 'Something went wrong',
  SLEEPING: 'Resting',
};

export class VisualEngine {
  private state: VisualState = 'AWAKENING';
  private stateAt = 0;
  private clock = 0;
  private params: VisualParams = { ...BASE, assemble: 0, core: 0.05, energy: 0.05 };
  private impulses: Impulse[] = [];
  private novaAudio = 0;
  private novaAudioTarget = 0;
  private lastNovaAudioAt = -10;
  private userAudio = 0;
  private userAudioTarget = 0;
  private bands = new Array(8).fill(0);
  private bandTargets = new Array(8).fill(0);
  private awake = false;
  private offline = false;
  private hearing = false;
  private hearingEndedAt = -10;
  private turnComplete = false;
  private toolsInFlight = 0;
  private kind: ToolKind = 'other';
  private toolLabel = '';
  private listeners = new Set<(s: VisualState) => void>();

  get current(): VisualState {
    return this.state;
  }

  onState(fn: (s: VisualState) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  setOffline(offline: boolean): void {
    this.offline = offline;
  }

  private go(next: VisualState): void {
    if (next === this.state) return;
    this.state = next;
    this.stateAt = this.clock;
    this.listeners.forEach((l) => l(next));
  }

  private impulse(kind: Impulse['kind'], extra: Partial<Impulse> = {}): void {
    this.impulses.push({ kind, at: this.clock, ...extra });
    if (this.impulses.length > 24) this.impulses.shift();
  }

  private settle(): void {
    // Where she rests depends on what is still true, not on what just ended.
    if (this.toolsInFlight > 0) this.go(KIND_STATE[this.kind]);
    else if (this.hearing) this.go('LISTENING');
    else this.go(this.awake ? 'IDLE' : 'AWAKENING');
  }

  /** Real NOVA events -> what is happening. */
  interpret(ev: NovaEvent): void {
    switch (ev.type) {
      case 'state': {
        const s = String(ev.state || '');
        if (s === 'connecting' && !this.awake) this.go('AWAKENING');
        else if (s === 'ready' || s === 'connected' || s === 'listening' || s === 'streaming') {
          if (!this.awake) {
            this.awake = true;
            this.impulse('ripple', { strength: 1 });
          }
          if (this.state === 'SLEEPING' || this.state === 'AWAKENING' || this.state === 'ERROR') this.settle();
        } else if (s === 'speaking') this.go('SPEAKING');
        else if (s === 'muted' || s === 'closed' || s === 'disconnecting') this.go('SLEEPING');
        else if (s === 'offline') this.offline = true;
        else if (s === 'error') {
          this.impulse('error');
          this.go('ERROR');
        }
        break;
      }
      case 'mic_level': {
        const hearing = Boolean(ev.hearing);
        this.userAudioTarget = hearing ? Math.max(0, Math.min(1, Number(ev.level) || 0)) : 0;
        if (hearing && !this.hearing) {
          this.hearing = true;
          this.turnComplete = false;
          if (this.state !== 'SPEAKING' && this.state !== 'SLEEPING') this.go('LISTENING');
        } else if (!hearing && this.hearing) {
          this.hearing = false;
          this.hearingEndedAt = this.clock;
        }
        break;
      }
      case 'user_transcript':
        this.turnComplete = false;
        if (this.state === 'IDLE') this.go('LISTENING');
        break;
      case 'tool_call': {
        const tools = (ev.tools as string[] | undefined) || [];
        const name = tools[0] || 'tool';
        this.kind = toolKind(name);
        this.toolLabel = name;
        this.toolsInFlight += Math.max(1, tools.length);
        this.impulse('path_out', { tool: this.kind });
        this.go('TOOL_SELECTION');
        break;
      }
      case 'agent_start': {
        const name = String(ev.tool || ev.name || 'tool');
        this.kind = toolKind(name);
        this.toolLabel = name;
        this.toolsInFlight += 1;
        this.impulse('path_out', { tool: this.kind });
        this.go(KIND_STATE[this.kind]);
        break;
      }
      case 'tool_result':
      case 'agent_done': {
        this.toolsInFlight = Math.max(0, this.toolsInFlight - 1);
        const failed = ev.ok === false;
        this.impulse('path_return', { tool: this.kind });
        this.impulse(failed ? 'error' : 'success');
        if (this.toolsInFlight === 0 && this.state !== 'SPEAKING') this.go(failed ? 'ERROR' : 'THINKING');
        break;
      }
      case 'task_start':
        this.impulse('path_out', { tool: 'task' });
        break;
      case 'task_done':
        this.impulse(ev.ok === false ? 'error' : 'success');
        break;
      case 'vision_capture':
      case 'vision_captured':
      case 'vision_sent':
        this.kind = 'vision';
        this.go('VISION');
        break;
      case 'screen_frame':
        // Ambient awareness: a faint shimmer, not a state -- she is not
        // "looking" in the attentive sense every second.
        this.impulse('noticed', { strength: 0.25, tool: 'vision' });
        break;
      case 'audio_level': {
        const lvl = Math.max(0, Math.min(1, Number(ev.level) || 0));
        this.novaAudioTarget = lvl;
        if (Array.isArray(ev.bands)) {
          (ev.bands as number[]).slice(0, 8).forEach((b, i) => (this.bandTargets[i] = Math.max(0, Math.min(1, b || 0))));
        }
        if (lvl > 0.02) {
          this.lastNovaAudioAt = this.clock;
          if (this.state !== 'SPEAKING' && this.state !== 'SLEEPING') this.go('SPEAKING');
        }
        break;
      }
      case 'interrupted':
        this.impulse('ripple', { strength: 0.6 });
        this.novaAudioTarget = 0;
        this.go('LISTENING');
        break;
      case 'turn_complete':
        this.turnComplete = true;
        break;
      case 'playback_complete':
        if (this.state === 'SPEAKING') this.settle();
        break;
      case 'loud_sound':
        // Something loud happened in the room. She notices; nothing changes.
        this.impulse('noticed', { strength: 0.6 });
        break;
      case 'error':
        this.impulse('error');
        break;
    }
  }

  /** The typed-chat path, which the UI drives itself. */
  chat(phase: 'sent' | 'token' | 'tool_start' | 'tool_done' | 'done' | 'error', tool?: string): void {
    switch (phase) {
      case 'sent':
        this.turnComplete = false;
        this.go('UNDERSTANDING');
        break;
      case 'token':
        if (this.state !== 'GENERATING') this.go('GENERATING');
        break;
      case 'tool_start':
        this.kind = toolKind(tool || 'tool');
        this.toolLabel = tool || '';
        this.toolsInFlight += 1;
        this.impulse('path_out', { tool: this.kind });
        this.go(KIND_STATE[this.kind]);
        break;
      case 'tool_done':
        this.toolsInFlight = Math.max(0, this.toolsInFlight - 1);
        this.impulse('path_return', { tool: this.kind });
        this.go('THINKING');
        break;
      case 'done':
        this.toolsInFlight = 0;
        this.impulse('success');
        this.go('SUCCESS');
        break;
      case 'error':
        this.toolsInFlight = 0;
        this.impulse('error');
        this.go('ERROR');
        break;
    }
  }

  /** Advance the clock; returns the frame the renderer draws. */
  tick(dt: number): VisualFrame {
    this.clock += dt;
    const inState = this.clock - this.stateAt;

    // Time-based settling: every transient state ends by itself.
    if (this.state === 'LISTENING' && !this.hearing && this.clock - this.hearingEndedAt > 0.9 && inState > 0.4) {
      this.go('UNDERSTANDING');
    } else if (this.state === 'UNDERSTANDING' && inState > 1.6) {
      this.go(this.toolsInFlight > 0 ? KIND_STATE[this.kind] : 'THINKING');
    } else if (this.state === 'TOOL_SELECTION' && inState > 0.7) {
      this.go(KIND_STATE[this.kind]);
    } else if (this.state === 'SPEAKING' && this.clock - this.lastNovaAudioAt > 1.4) {
      this.settle();
    } else if (this.state === 'SUCCESS' && inState > 1.2) {
      this.settle();
    } else if (this.state === 'ERROR' && inState > 3.0) {
      this.settle();
    } else if (this.state === 'THINKING' && inState > 25) {
      this.settle(); // nothing came back; do not sit "thinking" forever
    } else if (this.state === 'AWAKENING' && this.awake && inState > 1.8) {
      this.settle();
    } else if (
      (this.state === 'SEARCHING' || this.state === 'TOOL_EXECUTION' || this.state === 'VISION' ||
        this.state === 'MEMORY_RETRIEVAL') && this.toolsInFlight === 0 && inState > 1.5
    ) {
      this.go(this.turnComplete ? 'IDLE' : 'THINKING');
    }

    // Ease every parameter toward this state's target.
    const target: VisualParams = { ...BASE, ...TARGETS[this.state], offline: this.offline ? 1 : 0 };
    if (this.state === 'AWAKENING' && !this.awake) target.assemble = 0.35 + 0.35 * Math.min(1, inState / 3);
    for (const key of Object.keys(target) as (keyof VisualParams)[]) {
      const tau = TAU[key] ?? 0.7;
      const k = 1 - Math.exp(-dt / tau);
      this.params[key] += (target[key] - this.params[key]) * k;
    }

    // Audio: fast attack, slow release -- speech cadence without jitter.
    const ease = (cur: number, tgt: number, attack: number, release: number) =>
      cur + (tgt - cur) * (1 - Math.exp(-dt / (tgt > cur ? attack : release)));
    this.novaAudio = ease(this.novaAudio, this.novaAudioTarget, 0.05, 0.28);
    this.novaAudioTarget *= Math.exp(-dt / 0.35); // levels arrive in bursts
    this.userAudio = ease(this.userAudio, this.userAudioTarget, 0.06, 0.3);
    for (let i = 0; i < 8; i++) {
      this.bands[i] = ease(this.bands[i], this.bandTargets[i], 0.06, 0.3);
      this.bandTargets[i] *= Math.exp(-dt / 0.35);
    }

    // Impulses live for a few seconds; the renderer reads their age.
    this.impulses = this.impulses.filter((i) => this.clock - i.at < 4);

    return {
      state: this.state,
      params: this.params,
      novaAudio: this.novaAudio,
      userAudio: this.userAudio,
      bands: this.bands,
      toolKind: this.kind,
      toolLabel: this.toolLabel,
      impulses: this.impulses,
      time: this.clock,
    };
  }
}
