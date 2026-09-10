/* mind/edges.js — Edge creation and update logic */
import * as THREE from "three";
import { REGIONS } from "./regions.js";

const edgeMaterialSemantic = new THREE.LineBasicMaterial({
  color: 0x2dd4a8,
  transparent: true,
  opacity: 0.25,
  linewidth: 1,
});

const edgeMaterialRegional = new THREE.LineBasicMaterial({
  color: 0x4e5b67,
  transparent: true,
  opacity: 0.15,
  linewidth: 1,
});

export function createEdges(edgesData, nodeMap) {
  const group = new THREE.Group();
  const edgeLines = [];

  for (const ed of edgesData) {
    const sourceMesh = nodeMap.get(ed.source);
    const targetMesh = nodeMap.get(ed.target);
    if (!sourceMesh || !targetMesh) continue;

    const points = [sourceMesh.position.clone(), targetMesh.position.clone()];
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    const material = ed.type === "semantic" ? edgeMaterialSemantic : edgeMaterialRegional;
    const line = new THREE.Line(geometry, material);
    line.userData = { source: ed.source, target: ed.target, weight: ed.weight, type: ed.type };
    group.add(line);
    edgeLines.push(line);
  }

  return { group, edgeLines };
}

export function updateEdges(edgeLines, nodeMap) {
  for (const line of edgeLines) {
    const sourceMesh = nodeMap.get(line.userData.source);
    const targetMesh = nodeMap.get(line.userData.target);
    if (!sourceMesh || !targetMesh) continue;

    const positions = line.geometry.attributes.position;
    if (positions) {
      positions.array[0] = sourceMesh.position.x;
      positions.array[1] = sourceMesh.position.y;
      positions.array[2] = sourceMesh.position.z;
      positions.array[3] = targetMesh.position.x;
      positions.array[4] = targetMesh.position.y;
      positions.array[5] = targetMesh.position.z;
      positions.needsUpdate = true;
    }
  }
}
