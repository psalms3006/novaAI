/* mind/inspector.js — Node detail inspector panel (right side on click) */
import { fetchNodeDetail } from "./data.js";

let _inspectorEl = null;
let _currentNode = null;

export function initInspector() {
  _inspectorEl = document.getElementById("mindmap-inspector");
}

export async function showInspector(nodeId, nodeData) {
  if (!_inspectorEl) return;
  _currentNode = nodeId;
  _inspectorEl.style.display = "block";
  _inspectorEl.innerHTML = `<div style="color:var(--muted);font-size:11px;">Loading…</div>`;

  const detail = await fetchNodeDetail(nodeId);
  if (!detail || _currentNode !== nodeId) return;

  const node = detail.node;
  const connections = detail.connections || [];

  let html = `
    <div style="margin-bottom:12px;">
      <div style="font:600 14px var(--font-brand);color:var(--text);margin-bottom:4px;">${escHtml(node.label)}</div>
      <div style="font:10px var(--font-mono);color:var(--faint);">${node.region} / ${node.type}</div>
    </div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:12px;white-space:pre-wrap;word-break:break-word;">${escHtml(node.detail || "")}</div>
  `;

  if (connections.length > 0) {
    html += `<div style="font:10px var(--font-mono);color:var(--faint);margin-bottom:6px;letter-spacing:0.08em;">CONNECTIONS</div>`;
    for (const c of connections) {
      html += `<div class="mindmap-conn" data-node="${c.id}" style="padding:4px 6px;margin:2px 0;border-radius:4px;cursor:pointer;font-size:12px;color:var(--muted);border-left:2px solid ${c.accent || "#4e5b67"};">${escHtml(c.label)}</div>`;
    }
  }

  _inspectorEl.innerHTML = html;

  // Click on connection to navigate
  _inspectorEl.querySelectorAll(".mindmap-conn").forEach(el => {
    el.addEventListener("click", () => {
      const ev = new CustomEvent("mindmap:focus-node", { detail: { nodeId: el.dataset.node } });
      document.dispatchEvent(ev);
    });
    el.addEventListener("mouseenter", () => { el.style.background = "rgba(255,255,255,0.04)"; });
    el.addEventListener("mouseleave", () => { el.style.background = "none"; });
  });
}

export function hideInspector() {
  if (_inspectorEl) _inspectorEl.style.display = "none";
  _currentNode = null;
}

function escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
