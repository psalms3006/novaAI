/* hud.js — drives the command-centre panels from real backend state.
 *
 * Every value rendered here is measured: vitals come from psutil on the host,
 * the agent roll call reflects the live event bus, latency is the last real
 * turn time, and link rates are actual socket counters. Nothing is simulated —
 * a field the backend cannot supply renders as "--" rather than a plausible
 * number, because a dense HUD full of invented readouts is worse than an
 * honest sparse one.
 *
 * Polling is deliberately modest (2 s) and pauses when the window is hidden,
 * so the HUD costs almost nothing while NOVA is in ambient mode.
 */
(function () {
  "use strict";

  const POLL_MS = 2000;
  const $ = (id) => document.getElementById(id);
  const TOKEN = window.DESK_TOKEN || "";

  let telemetry = [];
  let lastAgents = {};
  let timer = null;

  /* ── helpers ───────────────────────────────────────────────────────── */

  function pad(n, w = 2) { return String(n).padStart(w, "0"); }

  function fmtRate(bps) {
    if (bps == null) return "--";
    if (bps > 1e6) return (bps / 1e6).toFixed(2) + " MB/s";
    if (bps > 1e3) return (bps / 1e3).toFixed(0) + " KB/s";
    return bps + " B/s";
  }

  function logLine(text, cls) {
    const d = new Date();
    telemetry.unshift({
      t: `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`,
      text, cls: cls || "",
    });
    if (telemetry.length > 60) telemetry.length = 60;
    renderTelemetry();
  }
  window.hudLog = logLine;

  function renderTelemetry() {
    const el = $("hud-telemetry");
    if (!el) return;
    el.innerHTML = telemetry
      .map((l) => `<div><span class="t">${l.t}</span><span class="${l.cls}">${escapeHtml(l.text)}</span></div>`)
      .join("");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function setPill(id, state, label) {
    const el = $(id);
    if (!el) return;
    el.className = "pill " + (state || "");
    el.innerHTML = "<i></i>" + label;
  }

  /* ── clock ─────────────────────────────────────────────────────────── */

  function tickClock() {
    const d = new Date();
    const c = $("hud-clock");
    const dt = $("hud-date");
    if (c) {
      const cs = pad(Math.floor(d.getMilliseconds() / 10));
      c.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${cs}`;
    }
    if (dt) {
      dt.textContent = d.toLocaleDateString(undefined, {
        day: "2-digit", month: "short", year: "numeric",
      }).toUpperCase();
    }
  }

  /* ── vitals ────────────────────────────────────────────────────────── */

  function renderVitals(v) {
    const el = $("hud-vitals");
    if (!el) return;
    const rows = [];
    const add = (label, value, pct, unit, hot) => {
      const shown = value == null ? "--" : value + (unit || "");
      const w = pct == null ? 0 : Math.max(0, Math.min(100, pct));
      rows.push(
        `<div class="vital-row">
           <div class="vital-top"><span>${label}</span><b>${shown}</b></div>
           <div class="vital-track"><div class="vital-fill${hot ? " hot" : ""}" style="width:${w}%"></div></div>
         </div>`
      );
    };
    add("CPU LOAD", v.cpu_pct != null ? v.cpu_pct.toFixed(1) : null, v.cpu_pct, "%", v.cpu_pct > 85);
    add("MEMORY", v.mem_pct != null ? v.mem_pct.toFixed(1) : null, v.mem_pct, "%", v.mem_pct > 90);
    if (v.mem_used_gb != null) {
      add("MEM USED", `${v.mem_used_gb} / ${v.mem_total_gb} GB`,
          (v.mem_used_gb / v.mem_total_gb) * 100, "", false);
    }
    if (v.thermal_c != null) add("THERMAL", v.thermal_c, Math.min(100, v.thermal_c), "°C", v.thermal_c > 80);
    el.innerHTML = rows.join("");
  }

  /* ── roll call ─────────────────────────────────────────────────────── */

  function renderAgents(agents) {
    const el = $("hud-agents");
    if (!el) return;
    let standby = 0;
    el.innerHTML = agents.map((a) => {
      const active = a.state === "active";
      if (!active) standby++;
      const cls = ["agent-cell"];
      if (active) cls.push("active");
      if (a.id === "orchestrator") cls.push("manager");
      const sub = active ? (a.action || "working") : a.role;
      return `<div class="${cls.join(" ")}" title="${escapeHtml(a.role)}">
                <div class="an">${escapeHtml(a.label)}</div>
                <div class="ar">${escapeHtml(sub)}</div>
              </div>`;
    }).join("");
    const sb = $("hud-standby");
    if (sb) sb.textContent = pad(standby, 3);

    // narrate transitions into the telemetry feed
    for (const a of agents) {
      const was = lastAgents[a.id];
      if (was !== a.state) {
        if (a.state === "active") logLine(`${a.label} engaged — ${a.action || a.role}`, "ok");
        else if (was === "active") logLine(`${a.label} released`, "");
      }
      lastAgents[a.id] = a.state;
    }
  }

  /* ── radar + waveform ──────────────────────────────────────────────── */

  let radarAngle = 0;

  function drawRadar(connected) {
    const cv = $("radar-canvas");
    if (!cv) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const r = cv.getBoundingClientRect();
    if (!r.width) return;
    if (cv.width !== r.width * dpr) {
      cv.width = r.width * dpr; cv.height = r.height * dpr;
    }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = r.width, h = r.height, cx = w / 2, cy = h / 2;
    const R = Math.min(w, h) / 2 - 2;
    ctx.clearRect(0, 0, w, h);

    ctx.strokeStyle = "rgba(87,200,255,0.22)";
    ctx.lineWidth = 1;
    for (const f of [1, 0.66, 0.33]) {
      ctx.beginPath(); ctx.arc(cx, cy, R * f, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();

    if (connected) {
      radarAngle = (radarAngle + 0.02) % (Math.PI * 2);
      const g = ctx.createConicGradient
        ? ctx.createConicGradient(radarAngle, cx, cy)
        : null;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, R, radarAngle - 0.5, radarAngle);
      ctx.closePath();
      if (g) {
        g.addColorStop(0, "rgba(87,200,255,0.35)");
        g.addColorStop(0.12, "rgba(87,200,255,0)");
        ctx.fillStyle = g;
      } else {
        ctx.fillStyle = "rgba(87,200,255,0.18)";
      }
      ctx.fill();
      ctx.restore();
    }
  }

  const waveBuf = new Array(64).fill(0);

  function drawWave(level) {
    const cv = $("wave-canvas");
    if (!cv) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const r = cv.getBoundingClientRect();
    if (!r.width) return;
    if (cv.width !== r.width * dpr) { cv.width = r.width * dpr; cv.height = r.height * dpr; }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = r.width, h = r.height;
    waveBuf.push(level); waveBuf.shift();
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(87,200,255,0.75)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    waveBuf.forEach((v, i) => {
      const x = (i / (waveBuf.length - 1)) * w;
      const y = h / 2 - (v * h * 0.42);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
    ctx.strokeStyle = "rgba(87,200,255,0.15)";
    ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
  }

  /* ── poll ──────────────────────────────────────────────────────────── */

  async function poll() {
    try {
      const res = await fetch("/api/system", {
        headers: { "X-NOVA-Desk": TOKEN },
        signal: AbortSignal.timeout(4000),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();

      renderVitals(d.vitals || {});
      renderAgents(d.agents || []);

      const intel = d.intelligence || {};
      const conn = intel.connectivity || "unknown";
      const online = conn === "online";
      setPill("pill-link", online ? "ok" : (conn === "degraded" ? "warn" : "bad"),
              conn.toUpperCase());
      setPill("pill-brain", intel.brain_ready ? "ok" : "warn",
              intel.brain_ready ? "BRAIN" : "BRAIN INIT");

      const tls = d.tls || {};
      setPill("pill-tls", tls.applied ? "ok" : "warn",
              tls.applied ? "TLS " + (tls.method || "").toUpperCase() : "TLS CERTIFI");

      const vs = (d.voice || {}).state || "idle";
      setPill("pill-voice", vs === "streaming" || vs === "connected" ? "ok"
              : (vs === "error" ? "bad" : ""), "VOICE " + vs.toUpperCase());

      const eng = $("hud-engine");
      if (eng) eng.textContent = (intel.model || intel.provider || "----").slice(0, 22);
      const lat = $("hud-latency");
      if (lat) lat.textContent = intel.last_turn_ms ? Math.round(intel.last_turn_ms) + " ms" : "--";

      const v = d.vitals || {};
      const up = $("hud-up"), dn = $("hud-down");
      if (up) up.textContent = fmtRate(v.up_bps);
      if (dn) dn.textContent = fmtRate(v.down_bps);

      // objective = the running task, else idle
      const obj = $("hud-objective");
      if (obj) {
        const running = (d.tasks || []).find((t) => t.status === "running" || t.status === "in_progress");
        obj.textContent = running ? running.title.toUpperCase().slice(0, 54) : "AWAITING INSTRUCTION";
      }

      const sess = $("hud-session");
      if (sess && TOKEN) sess.textContent = TOKEN.slice(0, 4).toUpperCase() + "-" + TOKEN.slice(4, 8).toUpperCase();

      drawRadar(online);
    } catch (e) {
      setPill("pill-link", "bad", "LINK LOST");
    }
  }

  /* ── animation for radar/wave between polls ────────────────────────── */

  function frame() {
    if (!document.hidden) {
      drawRadar(document.getElementById("pill-link")?.classList.contains("ok"));
      const amp = window.__novaAudioLevel || 0;
      drawWave(amp);
    }
    requestAnimationFrame(frame);
  }

  /* ── boot ──────────────────────────────────────────────────────────── */

  function start() {
    tickClock();
    setInterval(tickClock, 100);
    poll();
    timer = setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
    requestAnimationFrame(frame);
    logLine("command centre online", "ok");

    // drawer toggle
    const drawer = $("left-panel");
    $("btn-toggle-panel")?.addEventListener("click", () => drawer?.classList.toggle("open"));
    $("btn-close-panel")?.addEventListener("click", () => drawer?.classList.remove("open"));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") drawer?.classList.remove("open");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
