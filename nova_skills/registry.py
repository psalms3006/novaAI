"""nova_skills.registry — what NOVA can do, what enables it, and how it grew.

Three things kept apart (the brief's §60):

    Capability   what NOVA can do          "Create product advertisements"
    Provider     what currently enables it  an MCP server, an HTTP API, a tool
    Workflow     how she does it            ordered steps over providers/tools

and none of them is memory (what NOVA knows about the person).

A capability only counts as *learned* once its test has passed on this
machine (`verify`). Researching a route, or connecting a service, is recorded
honestly as what it is -- discovered, connected, failed -- never as learned.

Versions: every change to a capability's workflow or providers creates a new
version and keeps the previous one. A new version that fails its test does not
replace a working one (`propose_version` + `verify` + `promote`), and
`rollback` restores the last version that passed. The same rule as NOVA's own
software updates: never trade a working thing for an unverified one.

Credentials never live here. A provider holds a `credential_ref` -- the name of
an entry in the OS credential store, scoped to the signed-in account -- so the
registry can be shown, exported or synced without leaking a key.

Stored per account: NOVA_DATA_DIR/capabilities.json.
"""
from __future__ import annotations

import copy
import enum
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

FILENAME = "capabilities.json"


class Health(str, enum.Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    AUTH_REQUIRED = "auth_required"
    UPDATING = "updating"
    UNAVAILABLE = "unavailable"
    BROKEN = "broken"
    DEPRECATED = "deprecated"
    UNVERIFIED = "unverified"        # connected or composed, test not yet passed


class ProviderKind(str, enum.Enum):
    BUILTIN_TOOL = "builtin_tool"    # one of NOVA's own tools
    HTTP_API = "http_api"            # a web API reached with the person's credential
    MCP_SERVER = "mcp_server"        # an MCP server (stdio / SSE)
    EXTENSION = "extension"          # a local package through nova_extensions' trust ladder


@dataclass
class Provider:
    id: str
    kind: str
    name: str
    config: dict = field(default_factory=dict)       # base_url, tool name, server command...
    credential_ref: str = ""                          # secure-store key; never the secret
    cost: str = ""                                    # "free", "free tier", "$10/month", ...
    data_leaves_device: bool = False
    health: str = Health.UNVERIFIED.value
    last_checked: float = 0.0
    note: str = ""


@dataclass
class Version:
    number: int
    workflow: list
    provider_ids: list
    created_at: float
    passed_test: bool = False
    test_detail: str = ""


@dataclass
class Capability:
    id: str
    name: str
    description: str
    category: str = "general"
    providers: list = field(default_factory=list)       # [Provider as dict]
    workflow: list = field(default_factory=list)        # [{"tool"|"provider", ...}]
    test: dict = field(default_factory=dict)            # how to prove it works
    permissions: list = field(default_factory=list)     # scopes it needs
    version: int = 1
    versions: list = field(default_factory=list)        # [Version as dict]
    health: str = Health.UNVERIFIED.value
    learned: bool = False
    last_verified: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    source: str = ""                                     # "composed", "research", "catalog"
    owner_scope: str = "user"                            # capabilities here are this person's
    limitations: str = ""
    staged_providers: dict = field(default_factory=dict)  # {"<version>": [Provider]} awaiting a test

    def public(self) -> dict:
        d = asdict(self)
        for p in d.get("providers", []) + [p for ps in d.get("staged_providers", {}).values() for p in ps]:
            p.pop("credential_ref", None)                # present-tense only; never leaks
        return d


_FIELDS = set(Capability.__dataclass_fields__)


def _cap(raw: dict) -> Capability:
    """Tolerates keys written by a newer NOVA (dropped) rather than crashing."""
    return Capability(**{k: v for k, v in raw.items() if k in _FIELDS})


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:48] or uuid.uuid4().hex[:8]


class CapabilityRegistry:
    def __init__(self, path: Optional[Path] = None):
        base = os.getenv("NOVA_DATA_DIR", "").strip() or "."
        self.path = Path(path) if path else Path(base) / FILENAME
        self._lock = threading.RLock()
        self._data = self._load()

    # ── persistence ────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("capabilities", {})
                d.setdefault("timeline", [])
                return d
        except Exception:
            pass
        return {"schema": 1, "capabilities": {}, "timeline": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _event(self, event: str, cap_id: str, detail: str = "") -> None:
        self._data["timeline"].append({"at": time.time(), "event": event,
                                       "capability_id": cap_id, "detail": detail[:300]})
        self._data["timeline"] = self._data["timeline"][-500:]

    # ── reading ────────────────────────────────────────────────────────────────
    def get(self, cap_id: str) -> Optional[Capability]:
        with self._lock:
            raw = self._data["capabilities"].get(cap_id)
            return _cap(raw) if raw else None

    def all(self) -> list:
        with self._lock:
            return [_cap(c) for c in self._data["capabilities"].values()]

    def timeline(self, limit: int = 50) -> list:
        with self._lock:
            return list(reversed(self._data["timeline"][-limit:]))

    def usable(self) -> list:
        """Capabilities NOVA may promise: learned and currently healthy enough."""
        return [c for c in self.all()
                if c.learned and c.health in (Health.AVAILABLE.value, Health.DEGRADED.value)]

    # ── writing ────────────────────────────────────────────────────────────────
    def add(self, name: str, description: str, *, workflow: list, providers: list | None = None,
            test: dict | None = None, permissions: list | None = None, category: str = "general",
            source: str = "", limitations: str = "", cap_id: str = "") -> Capability:
        """Record a capability NOVA has worked out how to do -- not yet learned."""
        with self._lock:
            cid = cap_id or slug(name)
            if cid in self._data["capabilities"]:
                raise ValueError(f"capability {cid!r} already exists; propose a new version instead")
            provs = [asdict(p) if isinstance(p, Provider) else dict(p) for p in (providers or [])]
            now = time.time()
            cap = Capability(
                id=cid, name=name, description=description, category=category,
                providers=provs, workflow=copy.deepcopy(workflow), test=dict(test or {}),
                permissions=list(permissions or []), source=source, limitations=limitations,
                versions=[asdict(Version(1, copy.deepcopy(workflow),
                                         [p["id"] for p in provs], now))])
            self._data["capabilities"][cid] = asdict(cap)
            self._event("discovered" if source == "research" else "composed", cid,
                        f"{name}: {description}")
            self._save()
            return cap

    def record_test(self, cap_id: str, passed: bool, detail: str = "") -> Capability:
        """The only way a capability becomes learned: its test passed here."""
        with self._lock:
            raw = self._data["capabilities"][cap_id]
            now = time.time()
            for v in raw["versions"]:
                if v["number"] == raw["version"]:
                    v["passed_test"], v["test_detail"] = passed, detail[:300]
            if passed:
                first = not raw["learned"]
                raw.update(learned=True, health=Health.AVAILABLE.value, last_verified=now,
                           updated_at=now)
                self._event("learned" if first else "verified", cap_id, detail)
            else:
                # A capability that had been working and now fails is broken;
                # one that never passed simply is not learned yet.
                raw.update(health=(Health.BROKEN.value if raw["learned"] else Health.UNVERIFIED.value),
                           updated_at=now)
                self._event("test_failed", cap_id, detail)
            self._save()
            return _cap(raw)

    def record_inconclusive(self, cap_id: str, health: Health, detail: str = "") -> None:
        """A test that could not reach a verdict (timeout, service down). A
        learned capability stays learned; its health says why it can't run."""
        with self._lock:
            raw = self._data["capabilities"][cap_id]
            raw.update(health=health.value, updated_at=time.time())
            self._event("test_inconclusive", cap_id, detail)
            self._save()

    def set_health(self, cap_id: str, health: Health, detail: str = "") -> None:
        with self._lock:
            raw = self._data["capabilities"][cap_id]
            if raw["health"] != health.value:
                raw["health"] = health.value
                raw["updated_at"] = time.time()
                self._event(f"health_{health.value}", cap_id, detail)
                self._save()

    def propose_version(self, cap_id: str, *, workflow: list, providers: list | None = None,
                        why: str = "") -> int:
        """Stage a new workflow/provider set. The current version keeps running
        until the new one has passed its test and been promoted."""
        with self._lock:
            raw = self._data["capabilities"][cap_id]
            n = max(v["number"] for v in raw["versions"]) + 1
            provs = [asdict(p) if isinstance(p, Provider) else dict(p) for p in (providers or raw["providers"])]
            raw["versions"].append(asdict(Version(n, copy.deepcopy(workflow),
                                                  [p["id"] for p in provs], time.time())))
            raw.setdefault("staged_providers", {})[str(n)] = provs
            self._event("version_proposed", cap_id, f"v{n}: {why}")
            self._save()
            return n

    def record_version_test(self, cap_id: str, number: int, passed: bool, detail: str = "") -> None:
        with self._lock:
            raw = self._data["capabilities"][cap_id]
            for v in raw["versions"]:
                if v["number"] == number:
                    v["passed_test"], v["test_detail"] = passed, detail[:300]
            self._event("version_tested", cap_id, f"v{number} {'passed' if passed else 'failed'}: {detail}")
            self._save()

    def promote(self, cap_id: str, number: int) -> Capability:
        """Make a tested version current. Refuses one that has not passed."""
        with self._lock:
            raw = self._data["capabilities"][cap_id]
            v = next((v for v in raw["versions"] if v["number"] == number), None)
            if v is None:
                raise ValueError(f"no version {number}")
            if not v["passed_test"]:
                raise ValueError(f"version {number} has not passed its test; the working version stays")
            raw["workflow"] = copy.deepcopy(v["workflow"])
            staged = raw.get("staged_providers", {}).pop(str(number), None)
            if staged is not None:
                raw["providers"] = staged
            raw.update(version=number, health=Health.AVAILABLE.value, learned=True,
                       last_verified=time.time(), updated_at=time.time())
            self._event("updated", cap_id, f"now v{number}")
            self._save()
            return _cap(raw)

    def rollback(self, cap_id: str) -> Capability:
        """Back to the most recent earlier version that passed its test."""
        with self._lock:
            raw = self._data["capabilities"][cap_id]
            earlier = [v for v in raw["versions"] if v["number"] < raw["version"] and v["passed_test"]]
            if not earlier:
                raise ValueError("no earlier working version to go back to")
            v = max(earlier, key=lambda x: x["number"])
            raw["workflow"] = copy.deepcopy(v["workflow"])
            raw.update(version=v["number"], health=Health.AVAILABLE.value, updated_at=time.time())
            self._event("rolled_back", cap_id, f"back to v{v['number']}")
            self._save()
            return _cap(raw)

    def remove(self, cap_id: str) -> None:
        with self._lock:
            if self._data["capabilities"].pop(cap_id, None) is not None:
                self._event("removed", cap_id)
                self._save()


__all__ = ["CapabilityRegistry", "Capability", "Provider", "ProviderKind", "Health", "Version", "slug"]
