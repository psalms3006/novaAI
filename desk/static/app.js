/* NOVA Desktop — application logic (voice-first, ambient/full layout) */
"use strict";

const TOKEN = window.DESK_TOKEN || "";
const VERSION = window.DESK_VERSION || "dev";

const $ = (id) => document.getElementById(id);

/* ── Global State ──────────────────────────────────── */
const state = {
  cid: null,
  title: null,
  messages: [],
  streaming: false,
  voiceHolding: false,
  tools: {},
  statusData: null,
  imagePath: "",
  projectId: "",
  settings: {},
  speaking: false,
  listening: false,
  voiceMode: false,
  liveWs: null,
  liveEngine: "legacy",
  liveAudioCtx: null,
  liveNextPlayTime: 0,
  liveQueue: [],
  panelOpen: true,
  rightOpen: false,
  backendState: "initializing",
};

function setBackendState(newState) {
  if (state.backendState === newState) return;
  state.backendState = newState;
  const statusDot = $("status-dot");
  const statusText = $("status-text");
  const connText = $("conn-text");
  if (statusDot) {
    statusDot.className = "status-dot " + newState;
  }
  if (statusText) {
    const labels = {
      initializing: "Starting...",
      ready: "Connected",
      error: "Disconnected",
      reconnecting: "Reconnecting...",
    };
    statusText.textContent = labels[newState] || newState;
  }
  if (connText) {
    connText.textContent = newState === "ready" ? "Online" : "Offline";
  }
  console.log("[nova] backend state:", newState);
}

/* ── API helpers ──────────────────────────────────── */
async function api(path, opts = {}) {
  const headers = Object.assign({ "X-NOVA-Desk": TOKEN }, opts.headers || {});
  if (typeof opts.body === "string" && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const fetchOpts = Object.assign({}, opts, { headers });
  if (!fetchOpts.signal) {
    fetchOpts.signal = AbortSignal.timeout(opts.timeout || 8000);
  }
  const res = await fetch(path, fetchOpts);
  if (res.status === 401) throw new Error("Session expired — restart NOVA.");
  return res;
}

async function api_json(path, opts = {}) {
  const res = await api(path, opts);
  if (!res.ok) {
    let msg = "Request failed";
    try { msg = (await res.json()).error || msg; } catch (e) { /* noop */ }
    throw new Error(msg);
  }
  return res.json();
}

/* ── SSE streaming over fetch ─────────────────────── */
async function streamChat(path, body, onEvent) {
  const res = await api(path, { method: "POST", body: JSON.stringify(body), timeout: 120000 });
  if (!res.ok) {
    let msg = "Chat request failed";
    try { msg = (await res.json()).error || msg; } catch (e) { /* noop */ }
    throw new Error(msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          const ev = JSON.parse(line.slice(6));
          onEvent(ev);
        } catch (e) { /* noop */ }
      }
    }
  }
}

/* ── Toast ─────────────────────────────────────────── */
let _toastTimer = null;
function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}

/* ── Conversation Management ───────────────────────── */
async function newChat() {
  try {
    const j = await api_json("/api/conversations", { method: "POST", body: "{}" });
    state.cid = j.id;
    state.title = j.title || "New Chat";
    state.messages = [];
    $("chat-title").textContent = state.title;
    $("transcript-messages").innerHTML = "";
    $("transcript-placeholder").style.display = "";
    $("live-transcript").classList.remove("has-content");
  } catch (e) {
    console.warn("[nova] newChat:", e);
  }
}

async function loadConversation(cid) {
  try {
    const j = await api_json(`/api/conversations/${cid}`);
    state.cid = cid;
    state.title = j.title || "Chat";
    state.messages = j.messages || [];
    $("chat-title").textContent = state.title;
    renderTranscript();
  } catch (e) {
    console.warn("[nova] loadConversation:", e);
  }
}

function renderTranscript() {
  const container = $("transcript-messages");
  const placeholder = $("transcript-placeholder");
  container.innerHTML = "";

  if (state.messages.length === 0) {
    placeholder.style.display = "";
    $("live-transcript").classList.remove("has-content");
    return;
  }

  placeholder.style.display = "none";
  $("live-transcript").classList.add("has-content");

  for (const msg of state.messages.slice(-20)) {
    const div = document.createElement("div");
    div.className = `transcript-msg ${msg.role}`;
    div.textContent = msg.content || "";
    container.appendChild(div);
  }
  container.scrollTop = container.scrollHeight;
}

/* ── Settings ──────────────────────────────────────── */
async function loadSettings() {
  try {
    const j = await api_json("/api/settings");
    state.settings = j.settings || j;
    applySettings();
  } catch (e) {
    console.warn("[nova] loadSettings:", e);
  }
}

function applySettings() {
  const s = state.settings;
  if (s.theme) document.documentElement.dataset.theme = s.theme;
  if (s.accent) document.body.dataset.accent = s.accent;
}

/* ── Status ────────────────────────────────────────── */
async function refreshStatus() {
  try {
    const j = await api_json("/api/status");
    state.statusData = j;
    setBackendState("ready");
    const model = $("status-model");
    if (model) model.textContent = j.model || "";
    if (j.version) {
      const v = $("about-version");
      if (v) v.textContent = j.version;
    }
    if (j.brain_ready && orb) {
      setOrb("idle");
      console.log("[NOVA] Brain ready — orb set to idle");
    } else if (!j.brain_ready) {
      setOrb("offline");
      const lbl = $("orb-label");
      if (lbl) lbl.textContent = "Initializing brain...";
    }
  } catch (e) {
    setBackendState("error");
  }
}

async function pollBackendHealth() {
  try {
    const res = await fetch("/api/health", { signal: AbortSignal.timeout(3000) });
    if (res.ok) {
      setBackendState("ready");
      return true;
    }
  } catch (e) { /* noop */ }
  return false;
}

/* ── Conversations list ────────────────────────────── */
async function refreshConversations(query = "") {
  try {
    let url = "/api/conversations";
    if (query) url += `?search=${encodeURIComponent(query)}`;
    const j = await api_json(url);
    const list = $("convo-list");
    if (!list) return;
    list.innerHTML = "";
    for (const c of (j.conversations || [])) {
      const div = document.createElement("div");
      div.className = `convo-item${c.id === state.cid ? " active" : ""}`;
      div.textContent = c.title || "Untitled";
      div.addEventListener("click", () => loadConversation(c.id));
      list.appendChild(div);
    }
  } catch (e) {
    console.warn("[nova] refreshConversations:", e);
  }
}

/* ── Voice (Live WebSocket) ────────────────────────── */
async function liveStart() {
  try {
    const j = await api_json("/api/live/start", { method: "POST", body: "{}" });
    if (j.ok) {
      liveConnect();
      return true;
    }
    return false;
  } catch (e) {
    return false;
  }
}

function liveConnect() {
  if (state.liveWs) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/api/live/ws?token=${TOKEN}`;
  try {
    state.liveWs = new WebSocket(url);
  } catch (e) {
    return;
  }
  state.liveWs.onmessage = (evt) => {
    try {
      const ev = JSON.parse(evt.data);
      handleLiveEvent(ev);
    } catch (e) { /* noop */ }
  };
  state.liveWs.onclose = () => { state.liveWs = null; };
  state.liveWs.onerror = () => { state.liveWs = null; };
}

function handleLiveEvent(ev) {
  switch (ev.type) {
    case "state":
      if (ev.state === "connected" || ev.state === "streaming") {
        setOrb("listening");
        state.listening = true;
      } else if (ev.state === "error") {
        setOrb("error");
        state.listening = false;
      } else {
        setOrb("idle");
        state.listening = false;
      }
      break;
    case "user_transcript":
      addTranscript(ev.text, "user");
      break;
    case "nova_transcript":
      addTranscript(ev.text, "nova");
      break;
    case "audio":
      playAudioChunk(ev.data_b64);
      break;
  }
}

function addTranscript(text, role) {
  const container = $("transcript-messages");
  const placeholder = $("transcript-placeholder");
  if (!container) return;

  placeholder.style.display = "none";
  $("live-transcript").classList.add("has-content");

  const div = document.createElement("div");
  div.className = `transcript-msg ${role}`;
  div.textContent = text;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

async function liveDisconnect() {
  if (state.liveWs) {
    state.liveWs.close();
    state.liveWs = null;
  }
  state.listening = false;
  try { await api_json("/api/live/stop", { method: "POST", body: "{}" }); } catch (e) { /* noop */ }
}

/* ── Audio Playback ────────────────────────────────── */
function playAudioChunk(b64) {
  try {
    if (!state.liveAudioCtx) {
      state.liveAudioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    }
    const ctx = state.liveAudioCtx;
    const raw = atob(b64);
    const buf = new ArrayBuffer(raw.length);
    const view = new Uint8Array(buf);
    for (let i = 0; i < raw.length; i++) view[i] = raw.charCodeAt(i);
    const samples = new Int16Array(buf);
    const float32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      float32[i] = samples[i] / 32768.0;
    }
    // Drive the orb and the AUDIO S/N panel from the real signal, so the
    // surface tracks NOVA's actual voice instead of animating on a timer.
    let peak = 0;
    for (let i = 0; i < float32.length; i += 16) {
      const a = float32[i] < 0 ? -float32[i] : float32[i];
      if (a > peak) peak = a;
    }
    window.__novaAudioLevel = peak;
    if (orb && orb.setAmplitude) orb.setAmplitude(peak);
    setTimeout(() => {
      if (window.__novaAudioLevel === peak) window.__novaAudioLevel = 0;
    }, 260);

    const audioBuf = ctx.createBuffer(1, float32.length, 24000);
    audioBuf.getChannelData(0).set(float32);
    const src = ctx.createBufferSource();
    src.buffer = audioBuf;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    if (now < state.liveNextPlayTime) {
      src.start(state.liveNextPlayTime);
    } else {
      src.start(0);
    }
    state.liveNextPlayTime = ctx.currentTime + audioBuf.duration + 0.02;
  } catch (e) { /* noop */ }
}

/* ── Chat (text input) ─────────────────────────────── */
async function sendText(text, imagePath) {
  if (!text || !text.trim()) return;
  if (state.streaming) return;

  const msg = text.trim();
  state.streaming = true;
  setOrb("thinking");

  // Add user message to transcript
  addTranscript(msg, "user");

  // Hide placeholder
  const placeholder = $("transcript-placeholder");
  if (placeholder) placeholder.style.display = "none";
  $("live-transcript").classList.add("has-content");

  let fullText = "";
  let toolEvents = [];

  try {
    await streamChat("/api/chat", {
      conversation_id: state.cid || "",
      message: msg,
      image_path: imagePath || "",
    }, (ev) => {
      if (ev.type === "token") {
        fullText += ev.text;
        // Streaming indicator
      } else if (ev.type === "tool_start") {
        setOrb("executing");
        toolEvents.push({ name: ev.name, label: ev.label });
      } else if (ev.type === "tool_done") {
        setOrb("thinking");
      } else if (ev.type === "assistant") {
        fullText = ev.text;
      } else if (ev.type === "done") {
        // Turn complete
      } else if (ev.type === "error") {
        console.error("[nova] chat error event:", ev);
        toast(ev.message || "Error");
      }
    });

    if (fullText) {
      addTranscript(fullText, "nova");
    }

    // Update conversation list
    refreshConversations();
  } catch (e) {
    toast("Chat failed: " + e.message);
  } finally {
    state.streaming = false;
    setOrb("idle");
    state.imagePath = "";
  }
}

/* ── Orb State Management ──────────────────────────── */
let orb = null;

/* The WebGL orb is an ES module and may finish loading after boot() has already
 * stood up the 2D fallback. Rather than race, let it hand itself in and swap
 * cleanly — the 2D orb is stopped and its canvas hidden. */
window.__novaSetOrb = function (next) {
  try {
    if (orb === next) return;
    if (orb && typeof orb.stop === "function") orb.stop();
    const old2d = document.getElementById("orb-canvas");
    if (old2d) old2d.style.display = "none";
    orb = next;
    next.start();
    next.setState(window.__lastOrbState || "idle");
    console.log("[NOVA] 3D orb active");
    if (window.hudLog) window.hudLog("volumetric orb active", "ok");
  } catch (e) {
    console.warn("[NOVA] orb swap failed:", e);
  }
};
let agentField = null;
let eventClient = null;

function setOrb(st) {
  if (orb) orb.setState(st);
  if (window.hudLog && st !== window.__lastOrbState) {
    window.__lastOrbState = st;
    window.hudLog("state -> " + st, st === "error" ? "bad" : "");
  }

  // Update label
  const label = $("orb-label");
  if (label) {
    const labels = {
      idle: "Ready",
      listening: "Listening...",
      thinking: "Thinking...",
      speaking: "Speaking...",
      executing: "Working...",
      offline: "Offline",
      error: "Error",
    };
    label.textContent = labels[st] || st;
  }
}

/* ── Panel Toggle ──────────────────────────────────── */
function togglePanel() {
  state.panelOpen = !state.panelOpen;
  const panel = $("left-panel");
  if (panel) panel.classList.toggle("collapsed", !state.panelOpen);
}

/* ── Right Field Toggle ────────────────────────────── */
function toggleRightField(show) {
  state.rightOpen = show;
  const field = $("right-field");
  if (field) field.classList.toggle("active", show);
}

/* ── Onboarding ────────────────────────────────────── */
function maybeShowOnboarding() {
  const a = (state.statusData || {}).auth || {};
  if (a.onboarded || a.has_credential) {
    $("onboard-modal").style.display = "none";
    return;
  }
  $("onboard-modal").style.display = "flex";
}

function closeOnboarding() {
  $("onboard-modal").style.display = "none";
  refreshStatus().then(maybeShowOnboarding);
}

/* ── Settings Modal ────────────────────────────────── */
const SETTINGS_TABS = {
  general(host) {
    const s = state.settings;
    host.innerHTML = `
      <div class="settings-sec"><h3>Profile</h3>
        <div class="set-row"><div class="lbl">Your name</div>
          <input type="text" id="set-user-name" value="${escapeHtml(s.user_name || "User")}" style="width:180px"></div>
      </div>
      <div class="settings-sec"><h3>Startup</h3>
        <div class="set-row"><div class="lbl">Launch on startup</div>
          <label class="switch"><input type="checkbox" id="set-autostart" ${s.launch_on_startup ? "checked" : ""}><span class="slider"></span></label></div>
      </div>`;
  },
  voice(host) {
    const s = state.settings;
    host.innerHTML = `
      <div class="settings-sec"><h3>Voice</h3>
        <div class="set-row"><div class="lbl">Microphone input</div>
          <label class="switch"><input type="checkbox" id="set-voice" ${s.voice_enabled !== false ? "checked" : ""}><span class="slider"></span></label></div>
        <div class="set-row"><div class="lbl">Voice responses</div>
          <label class="switch"><input type="checkbox" id="set-tts" ${s.voice_responses ? "checked" : ""}><span class="slider"></span></label></div>
      </div>`;
  },
  appearance(host) {
    const s = state.settings;
    host.innerHTML = `
      <div class="settings-sec"><h3>Look & feel</h3>
        <div class="set-row"><div class="lbl">Theme</div>
          <select id="set-theme" style="width:130px">
            <option value="dark" ${s.theme === "dark" ? "selected" : ""}>Dark</option>
            <option value="light" ${s.theme === "light" ? "selected" : ""}>Light</option>
          </select></div>
      </div>`;
  },
  intelligence(host) {
    const s = state.settings;
    host.innerHTML = `
      <div class="settings-sec"><h3>Intelligence</h3>
        <div class="set-row"><div class="lbl">Stream responses</div>
          <label class="switch"><input type="checkbox" id="set-stream" ${s.streaming !== false ? "checked" : ""}><span class="slider"></span></label></div>
        <div class="set-row"><div class="lbl">Persistent memory</div>
          <label class="switch"><input type="checkbox" id="set-memory" ${s.memory_enabled !== false ? "checked" : ""}><span class="slider"></span></label></div>
      </div>`;
  },
  account(host) {
    const a = (state.statusData || {}).auth || {};
    const mode = a.mode || "unknown";
    const hasKey = !!(a.has_credential || a.byok_present);
    const masked = a.byok_masked || "";
    const modeLabel = {
      env: "API key from environment (.env)",
      byok: "Your own Gemini API key (sealed with Windows DPAPI)",
      cloud: "NOVA Cloud account",
      offline: "No AI model configured",
      unknown: "Unknown",
    }[mode] || mode;
    host.innerHTML = `
      <div class="settings-sec"><h3>AI connection</h3>
        <p class="set-note">Mode: <strong>${escapeHtml(mode)}</strong> — ${escapeHtml(modeLabel)}</p>
        ${masked ? `<p class="set-note">Key on file: ${escapeHtml(masked)}</p>` : ""}
      </div>
      <div class="settings-sec"><h3>Gemini API key</h3>
        <div class="set-col">
          <input type="password" id="set-api-key" placeholder="Paste your API key" autocomplete="off" style="width:100%">
          <div class="set-actions">
            <button class="btn-sm primary" id="set-api-key-save">Save key</button>
            ${hasKey ? '<button class="btn-sm" id="set-api-key-remove">Remove key</button>' : ""}
          </div>
          <p class="set-note" id="set-api-key-status"></p>
        </div>
      </div>
      <div class="settings-sec"><h3>Offline</h3>
        <div class="set-row"><div class="lbl">Use the local brain only</div>
          <button class="btn-sm" id="set-offline-mode">Go offline</button></div>
      </div>`;
  },
  permissions(host) {
    host.innerHTML = '<p class="set-note">Tool permissions are managed by NOVA\'s safety system.</p>';
  },
  advanced(host) {
    const s = state.settings;
    host.innerHTML = `
      <div class="settings-sec"><h3>Advanced</h3>
        <div class="set-row"><div class="lbl">Offline fallback</div>
          <label class="switch"><input type="checkbox" id="set-offline" ${s.offline_fallback ? "checked" : ""}><span class="slider"></span></label></div>
      </div>`;
  },
};

function showSettingsTab(tab) {
  document.querySelectorAll("#settings-tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab));
  const host = $("settings-body");
  host.innerHTML = "";
  try { SETTINGS_TABS[tab](host); } catch (e) { /* noop */ }
  wireSettingsListeners(host, tab);
}

function wireSettingsListeners(host, tab) {
  const save = (key, value) => {
    state.settings[key] = value;
    api("/api/settings", { method: "POST", body: JSON.stringify({ [key]: value }) })
      .then(() => toast("Settings saved"))
      .catch(() => toast("Failed to save"));
  };

  const userName = host.querySelector("#set-user-name");
  if (userName) userName.addEventListener("change", () => save("user_name", userName.value));

  const autostart = host.querySelector("#set-autostart");
  if (autostart) autostart.addEventListener("change", () => save("launch_on_startup", autostart.checked));

  const voice = host.querySelector("#set-voice");
  if (voice) voice.addEventListener("change", () => save("voice_enabled", voice.checked));

  const tts = host.querySelector("#set-tts");
  if (tts) tts.addEventListener("change", () => save("voice_responses", tts.checked));

  const theme = host.querySelector("#set-theme");
  if (theme) theme.addEventListener("change", () => {
    save("theme", theme.value);
    document.documentElement.dataset.theme = theme.value;
  });

  const stream = host.querySelector("#set-stream");
  if (stream) stream.addEventListener("change", () => save("streaming", stream.checked));

  const memory = host.querySelector("#set-memory");
  if (memory) memory.addEventListener("change", () => save("memory_enabled", memory.checked));

  const offline = host.querySelector("#set-offline");
  if (offline) offline.addEventListener("change", () => save("offline_fallback", offline.checked));

  // ── Account tab ────────────────────────────────────────────────
  const apiKeySave = host.querySelector("#set-api-key-save");
  const apiKeyInput = host.querySelector("#set-api-key");
  if (apiKeySave) apiKeySave.addEventListener("click", async () => {
    const key = (apiKeyInput ? apiKeyInput.value : "").trim();
    const statusEl = host.querySelector("#set-api-key-status");
    if (!key) {
      if (statusEl) statusEl.textContent = "Please paste a Gemini API key first.";
      toast("Enter an API key first");
      return;
    }
    apiKeySave.disabled = true;
    try {
      await api_json("/api/onboarding/byok", { method: "POST", body: JSON.stringify({ api_key: key }) });
      if (statusEl) statusEl.textContent = "Key saved.";
      toast("API key saved");
      await refreshStatus();
      showSettingsTab("account");
    } catch (e) {
      if (statusEl) statusEl.textContent = "Failed: " + (e.message || e);
      toast("Failed: " + (e.message || e));
    } finally {
      apiKeySave.disabled = false;
    }
  });

  const apiKeyRemove = host.querySelector("#set-api-key-remove");
  if (apiKeyRemove) apiKeyRemove.addEventListener("click", async () => {
    apiKeyRemove.disabled = true;
    try {
      await api_json("/api/onboarding/byok", { method: "DELETE" });
      toast("API key removed");
      await refreshStatus();
      showSettingsTab("account");
    } catch (e) {
      toast("Failed: " + (e.message || e));
    } finally {
      apiKeyRemove.disabled = false;
    }
  });

  const offlineMode = host.querySelector("#set-offline-mode");
  if (offlineMode) offlineMode.addEventListener("click", async () => {
    try {
      await api_json("/api/onboarding/offline", { method: "POST", body: "{}" });
      toast("Switched to offline mode");
      await refreshStatus();
      showSettingsTab("account");
    } catch (e) {
      toast("Failed: " + (e.message || e));
    }
  });
}



function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

function dismissInitScreen() {
  const screen = $("init-screen");
  if (!screen || screen.dataset.dismissed) return;
  screen.dataset.dismissed = "1";
  screen.classList.add("fade-out");
  screen.style.pointerEvents = "none";
  setTimeout(() => {
    screen.style.display = "none";
    try { screen.remove(); } catch(e) { /* noop */ }
  }, 400);
  console.log("[nova] init screen dismissed");
}

/* ── Boot Sequence ─────────────────────────────────── */
async function runInitSequence() {
  const screen = $("init-screen");
  const readyEl = $("init-ready");
  if (!screen || !readyEl) { dismissInitScreen(); return false; }

  const steps = ["core", "voice", "memory", "tools"];
  for (const step of steps) {
    const statusEl = $(`init-${step}`);
    if (statusEl) {
      statusEl.textContent = "loading...";
      await new Promise(r => setTimeout(r, 80 + Math.random() * 70));
      statusEl.textContent = "ready";
      statusEl.classList.add("ready");
    }
  }
  await new Promise(r => setTimeout(r, 200));
  const seq = screen.querySelector(".init-sequence");
  if (seq) seq.style.display = "none";
  readyEl.style.display = "flex";
  await new Promise(r => setTimeout(r, 500));
  dismissInitScreen();
  return true;
}

(async function boot() {
  // Theme
  try {
    const th = localStorage.getItem("nova_theme");
    if (th) document.documentElement.dataset.theme = th;
  } catch (e) { /* noop */ }

  // Safety timer: init screen always clears within 3s
  setTimeout(() => dismissInitScreen(), 3000);

  // Init animation
  const initDone = runInitSequence().catch(e => { console.warn("[boot] init:", e); return false; });

  // Show app
  const appEl = $("app");
  if (appEl) appEl.style.display = "flex";

  // Mark backend as ready immediately — the loading page already verified /api/health
  setBackendState("ready");

  // Initialize Canvas orb — retry if NovaOrb not yet loaded
  const orbCanvas = $("orb-canvas");
  function _initOrb() {
    if (!orbCanvas) return;
    try {
      // Prefer the volumetric WebGL form; the 2D canvas orb is only a fallback
      // for machines without a working WebGL context.
      if (window.__novaOrb) {
        orb = window.__novaOrb;
        orb.start();
        setOrb("idle");
        console.log("[NOVA] 3D orb initialized");
      } else if (window.NovaOrb) {
        orb = new NovaOrb(orbCanvas, 140);
        orb.start();
        setOrb("idle");
        console.log("[NOVA] Canvas orb initialized (WebGL unavailable)");
      } else {
        console.warn("[NOVA] NovaOrb not loaded — using CSS fallback");
        orbCanvas.style.display = "none";
        const fb = document.createElement("div");
        fb.className = "orb-css-fallback";
        orbCanvas.parentNode.insertBefore(fb, orbCanvas);
      }
    } catch (e) {
      console.error("[NOVA] Orb init error:", e);
      orbCanvas.style.display = "none";
      const fb = document.createElement("div");
      fb.className = "orb-css-fallback";
      orbCanvas.parentNode.insertBefore(fb, orbCanvas);
    }
  }
  if (window.__novaOrb || window.NovaOrb) {
    _initOrb();
  } else {
    // Retry up to 2s for deferred scripts
    let retries = 0;
    const iv = setInterval(() => {
      retries++;
      if (window.__novaOrb || window.NovaOrb || retries > 20) { clearInterval(iv); _initOrb(); }
    }, 100);
  }

  // Initialize agent field
  const agentContainer = $("agent-field");
  if (agentContainer && window.AgentField) {
    agentField = new AgentField(agentContainer);
  }

  // Initialize event client
  if (window.NovaEvents) {
    eventClient = new NovaEvents(TOKEN);
    eventClient.on("orb_state", (ev) => setOrb(ev.state));
    eventClient.on("voice_state", (ev) => {
      if (ev.state === "streaming") {
        state.listening = true;
        state.voiceMode = true;
      } else if (ev.state === "error" || ev.state === "closed") {
        state.listening = false;
      }
    });
    eventClient.on("transcript", (ev) => addTranscript(ev.text, ev.role));
    eventClient.on("task_start", (ev) => {
      if (agentField) agentField.handleEvent(ev);
      toggleRightField(true);
    });
    eventClient.on("task_done", (ev) => {
      if (agentField) agentField.handleEvent(ev);
    });
    eventClient.on("agent_start", (ev) => {
      if (agentField) agentField.handleEvent(ev);
    });
    eventClient.on("agent_progress", (ev) => {
      if (agentField) agentField.handleEvent(ev);
    });
    eventClient.on("agent_done", (ev) => {
      if (agentField) agentField.handleEvent(ev);
    });
    eventClient.connect();
  }

  // Parallel backend work
  const settingsP = loadSettings().catch(e => console.warn("[boot] settings:", e));
  const statusP = refreshStatus().catch(e => console.warn("[boot] status:", e));
  const convosP = refreshConversations().catch(e => console.warn("[boot] convos:", e));

  await Promise.allSettled([settingsP, statusP, convosP, initDone]);

  maybeShowOnboarding();
  try { await newChat(); } catch (e) { console.warn("[boot] newChat:", e); }
  setInterval(refreshStatus, 20000);

  // Fast-poll for brain_ready in first 30s (every 2s)
  let brainPolls = 0;
  const brainIv = setInterval(async () => {
    brainPolls++;
    if (brainPolls > 15 || (state.statusData && state.statusData.brain_ready)) {
      clearInterval(brainIv);
      if (state.statusData && state.statusData.brain_ready) {
        setOrb("idle");
        const lbl = $("orb-label");
        if (lbl) lbl.textContent = "Ready";
      }
      return;
    }
    await refreshStatus();
  }, 2000);

  // Auto-start voice
  setOrb("thinking");
  setTimeout(async () => {
    try {
      const liveOk = await liveStart();
      if (liveOk) {
        state.voiceMode = true;
        state.liveEngine = "live";
        setOrb("listening");
      } else {
        setOrb("idle");
      }
    } catch (e) {
      setOrb("idle");
    }
  }, 1500);
})();

/* ── Event Listeners ───────────────────────────────── */

// Panel toggle
const togglePanelBtn = $("btn-toggle-panel");
if (togglePanelBtn) togglePanelBtn.addEventListener("click", togglePanel);

const closePanelBtn = $("btn-close-panel");
if (closePanelBtn) closePanelBtn.addEventListener("click", () => {
  state.panelOpen = false;
  $("left-panel").classList.add("collapsed");
});

// Orb click — toggle listening
const orbCanvas = $("orb-canvas");
if (orbCanvas) {
  orbCanvas.addEventListener("click", () => {
    if (state.listening) {
      liveDisconnect();
      setOrb("idle");
    } else if (!state.streaming) {
      setOrb("thinking");
      liveStart().then(ok => {
        if (ok) { state.voiceMode = true; state.liveEngine = "live"; setOrb("listening"); }
        else setOrb("idle");
      }).catch(() => setOrb("idle"));
    }
  });
}

// Command bar submit
const commandInput = $("input");
if (commandInput) {
  commandInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const text = commandInput.value.trim();
      if (!text) return;
      commandInput.value = "";
      sendText(text, state.imagePath);
    }
  });
}

// Send button
const sendBtn = $("btn-send");
if (sendBtn) {
  sendBtn.addEventListener("click", () => {
    const text = commandInput.value.trim();
    if (!text) return;
    commandInput.value = "";
    sendText(text, state.imagePath);
  });
}

// Primary voice control. NOVA is voice-first, so this is the main affordance
// on the stage; #btn-voice is kept for compatibility but hidden by the HUD.
const micBtn = $("btn-mic");
const micLabel = $("voice-cta-label");

function _setMicUI(live) {
  if (micBtn) micBtn.classList.toggle("live", !!live);
  if (micLabel) {
    micLabel.textContent = live
      ? "Listening — just talk"
      : "Tap to talk, or type below";
  }
}

async function toggleVoice() {
  if (state.listening) {
    await liveDisconnect();
    _setMicUI(false);
    setOrb("idle");
    if (window.hudLog) window.hudLog("voice session stopped", "");
    return;
  }
  setOrb("thinking");
  if (window.hudLog) window.hudLog("opening voice session...", "");
  const ok = await liveStart();
  if (ok) {
    state.voiceMode = true;
    state.liveEngine = "live";
    _setMicUI(true);
    setOrb("listening");
    if (window.hudLog) window.hudLog("voice session live", "ok");
  } else {
    _setMicUI(false);
    setOrb("error");
    toast("Voice unavailable — check the Gemini key and microphone");
    if (window.hudLog) window.hudLog("voice session failed to start", "bad");
    setTimeout(() => setOrb("idle"), 2500);
  }
}

if (micBtn) micBtn.addEventListener("click", toggleVoice);
window.novaToggleVoice = toggleVoice;

// Voice button (legacy control, hidden in the HUD layout)
const voiceBtn = $("btn-voice");
if (voiceBtn) {
  voiceBtn.addEventListener("click", () => {
    if (state.listening) {
      liveDisconnect();
      setOrb("idle");
    } else {
      setOrb("thinking");
      liveStart().then(ok => {
        if (ok) { state.voiceMode = true; state.liveEngine = "live"; setOrb("listening"); }
        else setOrb("idle");
      }).catch(() => setOrb("idle"));
    }
  });
}

// Nav items
document.querySelectorAll(".nav-item").forEach(btn =>
  btn.addEventListener("click", () => {
    const view = btn.dataset.view;
    if (view === "settings") {
      refreshStatus();
      $("settings-modal").classList.add("open");
      showSettingsTab("general");
    } else if (window.novaOpenView) {
      // Real inspector panel (hud.js). These used to fall through to a toast
      // that just echoed the view name — five controls that looked functional
      // and did nothing, while their backends already existed.
      window.novaOpenView(view);
    } else {
      toast(`${view} unavailable`);
    }
  }));

// New chat
const newChatBtn = $("btn-new");
if (newChatBtn) newChatBtn.addEventListener("click", () => newChat());

// Search conversations
const searchInput = $("search-convos");
if (searchInput) {
  searchInput.addEventListener("input", (e) => {
    const q = e.target.value.trim();
    if (q.length) refreshConversations(q); else refreshConversations();
  });
}

// Settings modal
const settingsBtn = $("btn-settings");
if (settingsBtn) {
  settingsBtn.addEventListener("click", async () => {
    await Promise.allSettled([loadSettings(), refreshStatus()]);
    $("settings-modal").classList.add("open");
    showSettingsTab("general");
  });
}

// Settings tabs
document.querySelectorAll("#settings-tabs button").forEach(b =>
  b.addEventListener("click", () => showSettingsTab(b.dataset.tab)));

// Modal close buttons
const settingsCloseBtn = $("btn-settings-close");
if (settingsCloseBtn) settingsCloseBtn.addEventListener("click", () => $("settings-modal").classList.remove("open"));

const aboutCloseBtn = $("btn-about-close");
if (aboutCloseBtn) aboutCloseBtn.addEventListener("click", () => $("about-modal").classList.remove("open"));

// About button
const aboutBtn = $("btn-about");
if (aboutBtn) {
  aboutBtn.addEventListener("click", () => {
    $("about-version").textContent = VERSION;
    $("about-modal").classList.add("open");
  });
}

// Confirm modal
const confirmYesBtn = $("btn-confirm-yes");
if (confirmYesBtn) {
  confirmYesBtn.addEventListener("click", async () => {
    const id = $("confirm-modal").dataset.id;
    $("confirm-modal").classList.remove("open");
    await api("/api/confirm", { method: "POST", body: JSON.stringify({ id, decision: "yes" }) }).catch(() => {});
  });
}

const confirmNoBtn = $("btn-confirm-no");
if (confirmNoBtn) {
  confirmNoBtn.addEventListener("click", async () => {
    const id = $("confirm-modal").dataset.id;
    $("confirm-modal").classList.remove("open");
    await api("/api/confirm", { method: "POST", body: JSON.stringify({ id, decision: "no" }) }).catch(() => {});
  });
}

// Poll for confirmations
setInterval(async () => {
  try {
    const j = await api_json("/api/confirm/pending");
    const pending = j.pending || [];
    if (pending[0]) {
      $("confirm-text").textContent = pending[0].prompt;
      $("confirm-modal").dataset.id = pending[0].id;
      $("confirm-modal").classList.add("open");
    }
  } catch (e) { /* noop */ }
}, 1200);

// Onboarding
const obByokBtn = $("ob-byok-btn");
if (obByokBtn) {
  obByokBtn.addEventListener("click", async () => {
    const key = $("ob-byok-key").value.trim();
    if (!key) return;
    try {
      const j = await api_json("/api/onboarding/byok", { method: "POST", body: JSON.stringify({ api_key: key }) });
      if (j.ok) { toast("Key sealed"); closeOnboarding(); }
      else toast(j.error || "Failed");
    } catch (e) { toast(e.message); }
  });
}

const obCloudBtn = $("ob-cloud-btn");
if (obCloudBtn) {
  obCloudBtn.addEventListener("click", async () => {
    const url = $("ob-cloud-url").value.trim();
    if (!url) return toast("Enter server URL");
    try {
      const j = await api_json("/api/onboarding/cloud", { method: "POST", body: JSON.stringify({ url }) });
      if (j.ok) { toast("Connected"); closeOnboarding(); }
      else toast(j.error || "Failed");
    } catch (e) { toast(e.message); }
  });
}

const obOfflineBtn = $("ob-offline-btn");
if (obOfflineBtn) {
  obOfflineBtn.addEventListener("click", async () => {
    try {
      await api_json("/api/onboarding/offline", { method: "POST", body: "{}" });
      toast("Offline mode");
      closeOnboarding();
    } catch (e) { toast(e.message); }
  });
}

// Modal backdrop close
document.querySelectorAll(".modal-backdrop").forEach(bk =>
  bk.addEventListener("click", (e) => { if (e.target === bk) bk.classList.remove("open"); }));
