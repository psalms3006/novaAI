/* mind/regions.js — Region definitions, colors, and layout positions */

export const REGIONS = {
  core: {
    label: "Core",
    color: 0x2dd4a8,
    colorHex: "#2dd4a8",
    radius: 120,
    y: 0,
    sortOrder: 0,
  },
  memory: {
    label: "Memory",
    color: 0xa78bfa,
    colorHex: "#a78bfa",
    radius: 200,
    y: 60,
    sortOrder: 1,
  },
  working: {
    label: "Working Memory",
    color: 0x38bdf8,
    colorHex: "#38bdf8",
    radius: 160,
    y: -60,
    sortOrder: 2,
  },
  agents: {
    label: "Agents",
    color: 0xfb7185,
    colorHex: "#fb7185",
    radius: 180,
    y: 40,
    sortOrder: 3,
  },
  knowledge: {
    label: "Knowledge & Tools",
    color: 0xfbbf24,
    colorHex: "#fbbf24",
    radius: 220,
    y: -40,
    sortOrder: 4,
  },
  rim: {
    label: "Rim",
    color: 0x64748b,
    colorHex: "#64748b",
    radius: 300,
    y: 0,
    sortOrder: 5,
  },
};

export function getRegionForNode(node) {
  return REGIONS[node.region] || REGIONS.rim;
}
