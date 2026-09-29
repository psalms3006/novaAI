"""nova_skills.service — the one door to NOVA's self-extension.

Both the model (tool `nova_capability`) and the window (/api/capabilities) go
through here, so the rules hold whichever asks:

  * nothing is "learned" until its test passes on this machine;
  * a blocked option (elevation, disabling protections, whole-disk access,
    critical red flags in its own documentation) is refused, with the reason;
  * anything costing money, connecting an account, sending content out, or
    running third-party code waits for the person's yes;
  * credentials go from the window's secure field to the OS credential store
    -- the model never sees them and cannot supply them.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Callable, Optional

from . import connectors, runner
from .discovery import Report, discover
from .registry import CapabilityRegistry, Health, Provider, slug
from .safety import evaluate_option

_listeners: list = []


def on_event(fn: Callable[[dict], None]) -> None:
    """The window subscribes here (capability.* events)."""
    _listeners.append(fn)


def _emit(kind: str, **data) -> None:
    ev = {"type": f"capability.{kind}", "ts": time.time(), **data}
    for fn in list(_listeners):
        try:
            fn(ev)
        except Exception:
            pass


class CapabilityService:
    def __init__(self, tool_exec: Callable, *, declarations: list | None = None,
                 planner: Optional[Callable] = None, catalog: Optional[Callable[[], list]] = None,
                 search: Optional[Callable[[str], list]] = None,
                 registry: Optional[CapabilityRegistry] = None,
                 reporter: Optional[Callable[[str, bool], None]] = None):
        self.tool_exec = tool_exec
        self.declarations = declarations or []
        self.planner = planner
        self.catalog = catalog or (lambda: [])
        self.search = search
        self.registry = registry or CapabilityRegistry()
        #: Tells the backend catalog that a listed provider passed/failed here.
        #: Only the provider id and the outcome -- never inputs or outputs.
        self.reporter = reporter

    # ── the investigation ──────────────────────────────────────────────────────
    def discover(self, need: str) -> Report:
        _emit("discovery_started", need=need)
        try:
            cat = self.catalog()
        except Exception:
            cat = []
        rep = discover(need, self.registry, declarations=self.declarations,
                       planner=self.planner, catalog=cat, search=self.search)
        _emit("discovery_finished", need=need, stage=rep.stage, say=rep.say())
        return rep

    # ── adopting a route ───────────────────────────────────────────────────────
    def adopt_composed(self, name: str, description: str, workflow: list, test: dict) -> dict:
        """Register a workflow over NOVA's own tools, then prove it."""
        cap = self.registry.add(name, description, workflow=workflow, test=test,
                                providers=[Provider(id=f"tool-{s['tool']}", kind="builtin_tool",
                                                    name=s["tool"], config={"tool": s["tool"]},
                                                    cost="free", health=Health.AVAILABLE.value)
                                           for s in workflow if "tool" in s],
                                source="composed")
        _emit("added", capability_id=cap.id, name=name, stage="testing")
        passed, detail = runner.verify(self.registry, cap.id, self.tool_exec)
        _emit("verified" if passed else "test_failed", capability_id=cap.id, name=name, detail=detail)
        return {"id": cap.id, "learned": passed, "detail": detail}

    def propose_provider(self, option: dict, *, name: str, description: str,
                         workflow: list, test: dict) -> dict:
        """Set up an outside provider as a capability -- up to the point where
        the person has to act (approve, or connect their account)."""
        ev = evaluate_option(option)
        if ev.blocked:
            _emit("refused", name=name, reasons=ev.reasons)
            return {"ok": False, "blocked": True, "reasons": ev.reasons,
                    "say": f"I won't use {option.get('name', 'that')}: " + "; ".join(ev.reasons)}
        setup = dict(option.get("setup") or {})
        pid = slug(option.get("id") or option.get("name") or name)
        prov = Provider(id=pid, kind=option.get("kind", "http_api"), name=option.get("name", pid),
                        config=setup, cost=str(option.get("cost", "")),
                        data_leaves_device=bool(option.get("data_leaves_device")))
        cap = self.registry.add(name, description, workflow=workflow, providers=[prov], test=test,
                                source="catalog" if option.get("from_catalog") else "research",
                                permissions=list(option.get("permissions", [])))
        needs = list(ev.needs_approval)
        needs_key = bool(option.get("requires_account") or setup.get("auth_header"))
        _emit("pending", capability_id=cap.id, name=name, needs=needs, needs_credential=needs_key)
        return {"ok": True, "id": cap.id, "provider_id": pid, "needs_approval": needs,
                "needs_credential": needs_key,
                "say": (f"I found a route for {name} through {prov.name}. "
                        + (ev.summary() if needs else "")
                        + (" Connect your account in NOVA's window and I'll test it." if needs_key
                           else " I'll test it now."))}

    def provide_credential(self, cap_id: str, provider_id: str, secret: str) -> dict:
        """Called by the window's secure field -- never by the model."""
        cap = self.registry.get(cap_id)
        if cap is None:
            return {"ok": False, "error": "unknown capability"}
        ref = connectors.store_credential(provider_id, secret)
        with self.registry._lock:                                     # noqa: SLF001
            raw = self.registry._data["capabilities"][cap_id]          # noqa: SLF001
            for p in raw["providers"]:
                if p["id"] == provider_id:
                    p["credential_ref"] = ref
            self.registry._save()                                      # noqa: SLF001
        _emit("connected", capability_id=cap_id, provider_id=provider_id)
        return self.test(cap_id)

    # ── proving, using, keeping healthy ─────────────────────────────────────────
    def test(self, cap_id: str) -> dict:
        passed, detail = runner.verify(self.registry, cap_id, self.tool_exec)
        cap = self.registry.get(cap_id)
        if cap and cap.source == "catalog" and self.reporter:
            for p in cap.providers:
                try:
                    self.reporter(p["id"], passed)
                except Exception:
                    pass
        _emit("verified" if passed else "test_failed", capability_id=cap_id,
              name=cap.name if cap else cap_id, detail=detail)
        return {"ok": passed, "learned": bool(cap and cap.learned), "health": cap.health if cap else "",
                "detail": detail}

    def use(self, cap_id: str, inputs: dict) -> dict:
        res = runner.use(self.registry, cap_id, inputs, self.tool_exec)
        _emit("used", capability_id=cap_id, ok=res.ok)
        out = {"ok": res.ok, "output": res.text()[:4000], "error": res.error,
               "failed_step": res.failed_step}
        if not res.ok and res.error_class:
            from nova_core.errors import ErrorClass, advice
            out["error_class"] = res.error_class
            out["advice"] = advice(ErrorClass(res.error_class))
        return out

    def health_sweep(self) -> list:
        """Re-check every learned capability's providers; mark what changed."""
        changed = []
        for cap in self.registry.all():
            if not cap.learned:
                continue
            worst = Health.AVAILABLE
            for p in cap.providers:
                h, detail = connectors.check(Provider(**p))
                if h in (Health.AUTH_REQUIRED, Health.UNAVAILABLE, Health.BROKEN):
                    worst = h
                    break
                if h == Health.DEGRADED:
                    worst = Health.DEGRADED
            if worst.value != cap.health:
                self.registry.set_health(cap.id, worst, "health check")
                changed.append({"id": cap.id, "health": worst.value})
                _emit("health", capability_id=cap.id, health=worst.value)
        return changed

    def rollback(self, cap_id: str) -> dict:
        cap = self.registry.rollback(cap_id)
        _emit("rolled_back", capability_id=cap_id, version=cap.version)
        return {"ok": True, "version": cap.version}

    def remove(self, cap_id: str) -> dict:
        """The person's decision (window only). Their stored keys go with it."""
        cap = self.registry.get(cap_id)
        if cap is None:
            return {"ok": False, "error": "unknown capability"}
        with self.registry._lock:                                     # noqa: SLF001
            raw = self.registry._data["capabilities"][cap_id]          # noqa: SLF001
            refs = {p.get("credential_ref") for p in raw["providers"]}
            refs |= {p.get("credential_ref") for ps in raw.get("staged_providers", {}).values() for p in ps}
        for ref in filter(None, refs):
            try:
                connectors.forget_credential(ref)
            except Exception:
                pass
        self.registry.remove(cap_id)
        _emit("removed", capability_id=cap_id, name=cap.name)
        return {"ok": True}

    def overview(self) -> dict:
        caps = self.registry.all()
        return {"capabilities": [c.public() for c in caps],
                "timeline": self.registry.timeline(40),
                "pending": [c.public() for c in caps if not c.learned]}


__all__ = ["CapabilityService", "on_event"]
