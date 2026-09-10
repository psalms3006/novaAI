/* mind/nodes.js — Node creation, layout, and update logic */
import * as THREE from "three";
import { REGIONS, getRegionForNode } from "./regions.js";

const nodeGeometry = new THREE.IcosahedronGeometry(1, 3);
const nodeMaterialCache = {};

function getNodeMaterial(accent, opacity = 0.9) {
  const key = `${accent}_${opacity}`;
  if (!nodeMaterialCache[key]) {
    nodeMaterialCache[key] = new THREE.MeshStandardMaterial({
      color: new THREE.Color(accent),
      emissive: new THREE.Color(accent),
      emissiveIntensity: 0.3,
      transparent: opacity < 1,
      opacity,
      roughness: 0.4,
      metalness: 0.1,
    });
  }
  return nodeMaterialCache[key];
}

function createNodeLabel(text, color) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  ctx.font = "24px Inter, sans-serif";
  ctx.fillStyle = color;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, 128, 32, 250);

  const texture = new THREE.CanvasTexture(canvas);
  texture.minFilter = THREE.LinearFilter;
  const spriteMat = new THREE.SpriteMaterial({ map: texture, transparent: true, opacity: 0.85 });
  const sprite = new THREE.Sprite(spriteMat);
  sprite.scale.set(40, 10, 1);
  return sprite;
}

export function createNodes(nodesData) {
  const group = new THREE.Group();
  const nodeMap = new Map();

  for (const nd of nodesData) {
    const region = getRegionForNode(nd);
    const size = (nd.size || 0.5) * 6;
    const mat = getNodeMaterial(nd.accent || region.colorHex);
    const mesh = new THREE.Mesh(nodeGeometry, mat);
    mesh.scale.setScalar(size);
    mesh.userData = { nodeId: nd.id, region: nd.region, type: nd.type, label: nd.label, detail: nd.detail };

    // Label
    const label = createNodeLabel(nd.label, region.colorHex);
    label.position.y = size + 4;
    mesh.add(label);

    // Initial position (will be overridden by layout)
    mesh.position.set(
      (Math.random() - 0.5) * 100,
      (Math.random() - 0.5) * 60,
      (Math.random() - 0.5) * 100
    );

    group.add(mesh);
    nodeMap.set(nd.id, mesh);
  }

  return { group, nodeMap };
}

export function layoutNodes(nodeMap, nodesData) {
  // Group nodes by region
  const regionGroups = {};
  for (const nd of nodesData) {
    const r = nd.region || "rim";
    if (!regionGroups[r]) regionGroups[r] = [];
    regionGroups[r].push(nd.id);
  }

  // Place regions in a circle around core
  const regionKeys = Object.keys(REGIONS).sort((a, b) => REGIONS[a].sortOrder - REGIONS[b].sortOrder);
  const regionAngleStep = (Math.PI * 2) / regionKeys.length;
  const regionOrbitRadius = 180;

  for (let ri = 0; ri < regionKeys.length; ri++) {
    const rKey = regionKeys[ri];
    const region = REGIONS[rKey];
    const nodeIds = regionGroups[rKey] || [];
    if (nodeIds.length === 0) continue;

    const baseAngle = ri * regionAngleStep;
    const baseX = Math.cos(baseAngle) * regionOrbitRadius;
    const baseZ = Math.sin(baseAngle) * regionOrbitRadius;
    const baseY = region.y || 0;

    if (rKey === "core") {
      // Core nodes at center
      for (let i = 0; i < nodeIds.length; i++) {
        const mesh = nodeMap.get(nodeIds[i]);
        if (mesh) {
          mesh.position.set(baseX, baseY, baseZ);
          mesh.scale.multiplyScalar(1.5);
        }
      }
    } else {
      // Scatter nodes in the region's area
      const count = nodeIds.length;
      const cols = Math.ceil(Math.sqrt(count));
      const spacing = 25;
      for (let i = 0; i < count; i++) {
        const mesh = nodeMap.get(nodeIds[i]);
        if (!mesh) continue;
        const row = Math.floor(i / cols);
        const col = i % cols;
        const offsetX = (col - cols / 2) * spacing;
        const offsetZ = (row - Math.ceil(count / cols) / 2) * spacing;
        mesh.position.set(baseX + offsetX, baseY + offsetZ * 0.3, baseZ + offsetZ);
      }
    }
  }
}
