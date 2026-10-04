"""nova_learning.store — learned domains, on disk, per account.

    NOVA_DATA_DIR/knowledge/
        domains.json                 {domain_id: Domain}
        domains/<id>/knowledge.json  items, contradictions, file manifest
        sessions/<session_id>.json   one learning session each
        timeline.json                what NOVA learned, when

Nothing here depends on a model or a conversation: a restart, a new chat, or a
switch from Gemini to the local model reads the same files.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

SCOPES = ("reference", "personal", "project", "global")

#: How much a source is trusted when sources disagree (§102). Higher wins.
#: Matched against the file's path; configurable in knowledge/priority.json.
DEFAULT_PRIORITY = [
    (r"(^|/)(instructions?|rules?|must|standards?)(/|[-_. ])", 80, "explicit instruction"),
    (r"(spec|specification|requirements?)", 70, "project specification"),
    (r"(brand|guideline|style[-_ ]?guide|design[-_ ]?system)", 60, "brand / company guideline"),
    (r"(notes?|preferences?|decisions?|my[-_ ])", 50, "user-authored note"),
    (r"(principles?|reference|references)", 40, "curated reference"),
    (r"(tutorial|course|book|lesson|guide)", 30, "educational material"),
    (r"(example|examples|sample|mockup|screenshot|inspiration)", 20, "example"),
]


def _now() -> float:
    return time.time()


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or uuid.uuid4().hex[:8]


class KnowledgeStore:
    def __init__(self, root: Optional[Path] = None):
        base = os.getenv("NOVA_DATA_DIR", "").strip() or "."
        self.root = Path(root) if root else Path(base) / "knowledge"
        self._lock = threading.RLock()

    # ── files ────────────────────────────────────────────────────────────────
    def _read(self, p: Path, default):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write(self, p: Path, data) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    # ── domains ──────────────────────────────────────────────────────────────
    def domains(self) -> dict:
        with self._lock:
            return self._read(self.root / "domains.json", {})

    def domain(self, domain_id: str) -> Optional[dict]:
        return self.domains().get(domain_id)

    def find_domain(self, name_or_id: str) -> Optional[dict]:
        d = self.domains()
        if name_or_id in d:
            return d[name_or_id]
        want = slug(name_or_id)
        for dom in d.values():
            if slug(dom["name"]) == want:
                return dom
        return None

    def upsert_domain(self, name: str, *, scope: str, source: str, project_id: str = "") -> dict:
        with self._lock:
            d = self.domains()
            existing = next((x for x in d.values()
                             if slug(x["name"]) == slug(name) and x.get("project_id", "") == project_id), None)
            if existing:
                if source and source not in existing["sources"]:
                    existing["sources"].append(source)
                existing["scope"] = scope or existing["scope"]
                existing["updated"] = _now()
                dom = existing
            else:
                did = slug(name) + ("-" + slug(project_id) if project_id else "")
                dom = {"id": did, "name": name, "scope": scope, "project_id": project_id,
                       "sources": [source] if source else [], "status": "learning",
                       "created": _now(), "updated": _now(), "verification": {},
                       "item_count": 0}
                d[did] = dom
            self._write(self.root / "domains.json", d)
            return dom

    def update_domain(self, domain_id: str, **fields) -> dict:
        with self._lock:
            d = self.domains()
            d[domain_id].update(fields, updated=_now())
            self._write(self.root / "domains.json", d)
            return d[domain_id]

    def remove_domain(self, domain_id: str) -> bool:
        with self._lock:
            d = self.domains()
            if domain_id not in d:
                return False
            d.pop(domain_id)
            self._write(self.root / "domains.json", d)
            kp = self.root / "domains" / domain_id / "knowledge.json"
            if kp.exists():
                kp.unlink()
            return True

    # ── knowledge ────────────────────────────────────────────────────────────
    def knowledge(self, domain_id: str) -> dict:
        with self._lock:
            return self._read(self.root / "domains" / domain_id / "knowledge.json",
                              {"items": [], "contradictions": [], "files": {}})

    def save_knowledge(self, domain_id: str, k: dict) -> None:
        with self._lock:
            self._write(self.root / "domains" / domain_id / "knowledge.json", k)
            active = [i for i in k.get("items", []) if i.get("status") == "active"]
            if domain_id in self.domains():
                self.update_domain(domain_id, item_count=len(active),
                                   contradiction_count=len([c for c in k.get("contradictions", [])
                                                            if not c.get("resolved_by")]))

    # ── sessions ─────────────────────────────────────────────────────────────
    def save_session(self, s: dict) -> None:
        with self._lock:
            s["updated"] = _now()
            self._write(self.root / "sessions" / f"{s['id']}.json", s)

    def session(self, sid: str) -> Optional[dict]:
        return self._read(self.root / "sessions" / f"{sid}.json", None)

    def sessions(self, limit: int = 20) -> list:
        d = self.root / "sessions"
        if not d.is_dir():
            return []
        out = [self._read(p, None) for p in d.glob("*.json")]
        return sorted([s for s in out if s], key=lambda s: -s.get("created", 0))[:limit]

    # ── timeline ─────────────────────────────────────────────────────────────
    def event(self, kind: str, domain: str, detail: str = "") -> None:
        with self._lock:
            p = self.root / "timeline.json"
            t = self._read(p, [])
            t.append({"at": _now(), "event": kind, "domain": domain, "detail": detail[:300]})
            self._write(p, t[-500:])

    def timeline(self, limit: int = 50) -> list:
        return list(reversed(self._read(self.root / "timeline.json", [])[-limit:]))

    # ── source priority ──────────────────────────────────────────────────────
    def priority_rules(self) -> list:
        custom = self._read(self.root / "priority.json", None)
        if isinstance(custom, list) and custom:
            return [(r["pattern"], int(r["rank"]), r.get("label", "")) for r in custom]
        return DEFAULT_PRIORITY


def source_rank(rel: str, rules: list) -> tuple:
    low = (rel or "").lower().replace("\\", "/")
    best = (35, "unclassified source")
    for pattern, rank, label in rules:
        if re.search(pattern, low) and rank > best[0]:
            best = (rank, label)
    return best


__all__ = ["KnowledgeStore", "SCOPES", "source_rank", "slug", "DEFAULT_PRIORITY"]
