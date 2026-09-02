/* mind/main.js — Entry point: wires scene, nodes, edges, live events, navigation */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { fetchMindMap, invalidateCache } from "./data.js";
import { REGIONS } from "./regions.js";
import { createNodes, layoutNodes } from "./nodes.js";
import { createEdges, updateEdges } from "./edges.js";
import { createScene, resizeScene, renderScene } from "./scene.js";
import { initInspector, showInspector, hideInspector } from "./inspector.js";

let sceneCtx = null;
let nodeGroup = null;
let nodeMap = null;
let edgeGroup = null;
let edgeLines = null;
let controls = null;
let raycaster = null;
let mouse = null;
let hoveredNode = null;
let statsEl = null;
let legendEl = null;
let observeWs = null;
let frameCount = 0;
let lastFpsTime = 0;
let fps = 0;

// ── Stats HUD ────────────────────────────────────────────────────────────────

function updateStats(stats) {
  if (!statsEl) return;
  const regions = stats.regions || {};
  const lines = [
    `<span style="color:var(--accent)">◉</span> NOVA Mind Map`,
    `Nodes: ${stats.total_nodes}  Edges: ${stats.total_edges}`,
    `FAISS: ${stats.has_faiss ? "yes" : "no"}`,
  ];
  for (const [r, count] of Object.entries(regions)) {
    const col = REGIONS[r]?.colorHex || "#64748b";
    lines.push(`<span style="color:${col}">●</span> ${r}: ${count}`);
  }
  statsEl.innerHTML = lines.join("<br>");
}

function buildLegend() {
  if (!legendEl) return;
  legendEl.innerHTML = "";
  for (const [key, region] of Object.entries(REGIONS)) {
    const chip = document.createElement("div");
    chip.className = "legend-chip";
    chip.innerHTML = `<span class="dot" style="background:${region.colorHex}"></span>${region.label}`;
    chip.addEventListener("click", () => focusRegion(key));
    legendEl.appendChild(chip);
  }
}

function focusRegion(regionKey) {
  if (!nodeMap) return;
  const region = REGIONS[regionKey];
  if (!region) return;

  // Find center of region nodes
  let cx = 0, cy = 0, cz = 0, count = 0;
  for (const [id, mesh] of nodeMap) {
    if (mesh.userData.region === regionKey) {
      cx += mesh.position.x;
      cy += mesh.position.y;
      cz += mesh.position.z;
      count++;
    }
  }
  if (count === 0) return;
  cx /= count; cy /= count; cz /= count;

  // Animate camera to look at region
  const target = new THREE.Vector3(cx, cy, cz);
  const offset = new THREE.Vector3(cx + 120, cy + 80, cz + 120);
  animateCamera(offset, target);
}

function animateCamera(targetPos, lookAt) {
  if (!sceneCtx) return;
  const cam = sceneCtx.camera;
  const start = cam.position.clone();
  const startTarget = controls ? controls.target.clone() : new THREE.Vector3();
  const duration = 800;
  const startTime = performance.now();

  function step(now) {
    const t = Math.min((now - startTime) / duration, 1);
    const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
    cam.position.lerpVectors(start, targetPos, ease);
    if (controls) {
      controls.target.lerpVectors(startTarget, lookAt, ease);
      controls.update();
    }
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

// ── Raycasting / hover / click ────────────────────────────────────────────────

function onPointerMove(event) {
  if (!sceneCtx || !nodeGroup) return;
  const canvas = sceneCtx.renderer.domElement;
  const rect = canvas.getBoundingClientRect();
  mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

  raycaster.setFromCamera(mouse, sceneCtx.camera);
  const intersects = raycaster.intersectObjects(nodeGroup.children, true);

  const hit = intersects.length > 0 ? intersects[0].object : null;
  if (hit && hit.userData && hit.userData.nodeId) {
    if (hoveredNode !== hit) {
      if (hoveredNode) hoveredNode.material.emissiveIntensity = 0.3;
      hoveredNode = hit;
      hoveredNode.material.emissiveIntensity = 0.8;
      canvas.style.cursor = "pointer";
    }
  } else {
    if (hoveredNode) {
      hoveredNode.material.emissiveIntensity = 0.3;
      hoveredNode = null;
      canvas.style.cursor = "grab";
    }
  }
}

function onPointerClick(event) {
  if (!sceneCtx || !nodeGroup) return;
  const canvas = sceneCtx.renderer.domElement;
  const rect = canvas.getBoundingClientRect();
  mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

  raycaster.setFromCamera(mouse, sceneCtx.camera);
  const intersects = raycaster.intersectObjects(nodeGroup.children, true);

  const hit = intersects.length > 0 ? intersects[0].object : null;
  if (hit && hit.userData && hit.userData.nodeId) {
    showInspector(hit.userData.nodeId, hit.userData);
    // Focus camera on node
    const pos = hit.position.clone();
    const offset = pos.clone().add(new THREE.Vector3(60, 40, 60));
    animateCamera(offset, pos);
  } else {
    hideInspector();
  }
}

// ── Live event stream ─────────────────────────────────────────────────────────

function connectObserver() {
  if (observeWs) return;
  try {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    observeWs = new WebSocket(`${proto}//${location.host}/ws/observe?token=${window.DESK_TOKEN || ""}`);
    observeWs.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "update") {
          invalidateCache();
          refreshMindMap();
        }
      } catch (e) { /* noop */ }
    };
    observeWs.onclose = () => { observeWs = null; setTimeout(connectObserver, 5000); };
    observeWs.onerror = () => { observeWs?.close(); observeWs = null; };
  } catch (e) {
    setTimeout(connectObserver, 5000);
  }
}

// ── Refresh ───────────────────────────────────────────────────────────────────

async function refreshMindMap() {
  if (!sceneCtx) return;
  const data = await fetchMindMap();

  // Remove old objects
  if (nodeGroup) sceneCtx.scene.remove(nodeGroup);
  if (edgeGroup) sceneCtx.scene.remove(edgeGroup);

  // Create new
  const nodes = createNodes(data.nodes);
  nodeGroup = nodes.group;
  nodeMap = nodes.nodeMap;
  layoutNodes(nodeMap, data.nodes);
  sceneCtx.scene.add(nodeGroup);

  const edges = createEdges(data.edges, nodeMap);
  edgeGroup = edges.group;
  edgeLines = edges.edgeLines;
  sceneCtx.scene.add(edgeGroup);

  updateStats(data.stats);
}

// ── Focus node (from inspector connection click) ──────────────────────────────

document.addEventListener("mindmap:focus-node", (ev) => {
  const { nodeId } = ev.detail;
  if (!nodeMap) return;
  const mesh = nodeMap.get(nodeId);
  if (mesh) {
    showInspector(nodeId, mesh.userData);
    const pos = mesh.position.clone();
    const offset = pos.clone().add(new THREE.Vector3(60, 40, 60));
    animateCamera(offset, pos);
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────

async function initMindMap() {
  const canvas = document.getElementById("mindmap-canvas");
  if (!canvas) return;

  statsEl = document.getElementById("mindmap-stats");
  legendEl = document.getElementById("mindmap-legend");
  initInspector();
  buildLegend();

  sceneCtx = createScene(canvas);
  raycaster = new THREE.Raycaster();
  mouse = new THREE.Vector2();

  // Controls
  controls = new OrbitControls(sceneCtx.camera, sceneCtx.renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.rotateSpeed = 0.5;
  controls.zoomSpeed = 0.8;
  controls.minDistance = 60;
  controls.maxDistance = 800;
  controls.target.set(0, 0, 0);

  // Events
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("click", onPointerClick);
  window.addEventListener("resize", () => {
    const rect = canvas.parentElement.getBoundingClientRect();
    resizeScene(sceneCtx, rect.width, rect.height);
  });

  // Initial size
  const rect = canvas.parentElement.getBoundingClientRect();
  resizeScene(sceneCtx, rect.width, rect.height);

  // Load data
  await refreshMindMap();
  connectObserver();

  // Render loop (60fps target, skip frames if idle)
  let lastInteract = performance.now();
  controls.addEventListener("start", () => { lastInteract = performance.now(); });

  function animate() {
    requestAnimationFrame(animate);
    controls.update();

    // FPS counter
    frameCount++;
    const now = performance.now();
    if (now - lastFpsTime >= 1000) {
      fps = frameCount;
      frameCount = 0;
      lastFpsTime = now;
    }

    // Update edges when nodes move
    if (edgeLines) updateEdges(edgeLines, nodeMap);

    renderScene(sceneCtx);
  }
  animate();
}

// Auto-init when mind map view becomes active
const observer = new MutationObserver(() => {
  const view = document.getElementById("view-mindmap");
  if (view && view.classList.contains("active") && !sceneCtx) {
    initMindMap();
  }
});
observer.observe(document.body, { subtree: true, attributes: true, attributeFilter: ["class"] });

// Also init if already active
if (document.getElementById("view-mindmap")?.classList.contains("active")) {
  initMindMap();
}
