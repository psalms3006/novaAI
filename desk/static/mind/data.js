/* mind/data.js — Fetch and cache mind map data from /api/mind-map */
let _cache = null;
let _fetching = null;

export async function fetchMindMap() {
  if (_cache) return _cache;
  if (_fetching) return _fetching;

  _fetching = (async () => {
    try {
      const r = await fetch("/api/mind-map", {
        headers: { "X-Desk-Token": window.DESK_TOKEN || "" }
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      _cache = await r.json();
      return _cache;
    } catch (e) {
      console.warn("[mind-map] fetch failed:", e);
      return { nodes: [], edges: [], stats: { total_nodes: 0, total_edges: 0, regions: {} } };
    } finally {
      _fetching = null;
    }
  })();
  return _fetching;
}

export async function fetchNodeDetail(nodeId) {
  try {
    const r = await fetch(`/api/mind-map/node/${nodeId}`, {
      headers: { "X-Desk-Token": window.DESK_TOKEN || "" }
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (e) {
    console.warn("[mind-map] node detail failed:", e);
    return null;
  }
}

export function invalidateCache() {
  _cache = null;
}
