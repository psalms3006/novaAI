"""nova_learning.service — learning sessions, in the background, resumable.

    learn(path, domain, scope)   start (or update) learning; returns at once
    status(sid) / sessions()     real counts, the current phase -- never a
                                 made-up percentage
    pause(sid) / resume(sid)     "continue" picks up from saved state
    forget(domain)               remove a learned domain and its index

Re-learning a folder that was learned before only processes what changed
(§100): new and changed files are extracted, knowledge from deleted or
changed files is retired, and verification runs again.

A session that cannot finish -- the model is out of quota, the network is
down -- stops in a resumable state and says why. It never reports success.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Callable, Optional

from . import consolidate, extract, inventory, model, verify
from .store import SCOPES, KnowledgeStore, source_rank

PHASES = ["discovering files", "classifying material", "reading documents",
          "analyzing visual references", "extracting knowledge", "cross-referencing",
          "resolving contradictions", "building domain model", "validating understanding",
          "registering knowledge", "ready"]

_listeners: list = []


def on_event(fn: Callable[[dict], None]) -> None:
    _listeners.append(fn)


def _emit(kind: str, **data) -> None:
    ev = {"type": f"learning.{kind}", "ts": time.time(), **data}
    for fn in list(_listeners):
        try:
            fn(ev)
        except Exception:
            pass


class _Stop(Exception):
    pass


class LearningService:
    def __init__(self, store: Optional[KnowledgeStore] = None, index: bool = True):
        self.store = store or KnowledgeStore()
        self.index = index                     # also index sources in the document library
        self._threads: dict = {}
        self._pause: dict = {}

    # ── starting ─────────────────────────────────────────────────────────────
    def learn(self, path: str, *, domain: str, scope: str = "personal", project_id: str = "",
              background: bool = True) -> dict:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        if scope == "project" and not project_id:
            raise ValueError("a project-scoped domain needs a project")
        if scope == "global":
            raise ValueError("contributing knowledge to the shared NOVA catalog is not open yet; "
                             "learn it as personal or project knowledge")
        if not os.path.exists(os.path.expanduser(path)):
            raise FileNotFoundError(f"There is no folder or file at {path}.")
        unfinished = [x for x in self.store.sessions(50)
                      if x.get("source") == path and x.get("status") in
                      ("running", "paused", "interrupted", "waiting_for_model")]
        if unfinished:
            x = unfinished[0]
            alive = x["id"] in self._threads and self._threads[x["id"]].is_alive()
            return x if (x["status"] == "running" and alive) else self.resume(x["id"], background=background)
        dom = self.store.upsert_domain(domain, scope=scope, source=path, project_id=project_id)
        s = {"id": "learn_" + uuid.uuid4().hex[:8], "source": path, "domain": dom["name"],
             "domain_id": dom["id"], "scope": scope, "project_id": project_id,
             "status": "running", "phase": PHASES[0], "created": time.time(),
             "counts": {}, "extracted": [], "raw_items": [], "raw_contradictions": [],
             "image_notes": {}, "failed": {}, "skipped": {}, "message": "", "models": []}
        self.store.save_session(s)
        self.store.event("learning_started", dom["name"], f"from {path}")
        _emit("started", session_id=s["id"], domain=dom["name"], source=path)
        self._start(s, background)
        return self.store.session(s["id"])

    def _start(self, s: dict, background: bool) -> None:
        self._pause[s["id"]] = threading.Event()
        if background:
            t = threading.Thread(target=self._run, args=(s["id"],), name=f"nova-learn-{s['id']}",
                                 daemon=True)
            self._threads[s["id"]] = t
            t.start()
        else:
            self._run(s["id"])

    def pause(self, sid: str) -> dict:
        ev = self._pause.get(sid)
        if ev:
            ev.set()
        return self.store.session(sid) or {"error": "no such session"}

    def resume(self, sid: str, *, background: bool = True) -> dict:
        s = self.store.session(sid)
        if not s:
            raise KeyError(sid)
        if s["status"] == "running" and sid in self._threads and self._threads[sid].is_alive():
            return s
        s["status"], s["message"] = "running", "continuing from where it stopped"
        self.store.save_session(s)
        _emit("resumed", session_id=sid, domain=s["domain"])
        self._start(s, background)
        return self.store.session(sid)

    def mark_interrupted(self) -> int:
        """At startup: sessions that were running when NOVA closed can be continued."""
        n = 0
        for s in self.store.sessions(100):
            if s.get("status") == "running":
                s["status"] = "interrupted"
                s["message"] = "NOVA closed while learning; say continue to resume"
                self.store.save_session(s)
                n += 1
        return n

    def status(self, sid: str = "") -> Optional[dict]:
        if sid:
            return self.store.session(sid)
        ss = self.store.sessions(1)
        return ss[0] if ss else None

    def forget(self, domain: str) -> bool:
        dom = self.store.find_domain(domain)
        if not dom:
            return False
        if self.index:
            self._unindex(dom["id"], None)
        self.store.remove_domain(dom["id"])
        self.store.event("forgotten", dom["name"])
        _emit("forgotten", domain=dom["name"])
        return True

    # ── the run ──────────────────────────────────────────────────────────────
    def _phase(self, s: dict, phase: str, **counts) -> None:
        if self._pause[s["id"]].is_set():
            s["status"] = "paused"
            s["message"] = f"paused during {s['phase']}; say continue to resume"
            self.store.save_session(s)
            _emit("paused", session_id=s["id"], domain=s["domain"])
            raise _Stop()
        s["phase"] = phase
        s["counts"].update(counts)
        self.store.save_session(s)
        _emit("progress", session_id=s["id"], domain=s["domain"], phase=phase, counts=dict(s["counts"]))

    def _run(self, sid: str) -> None:
        s = self.store.session(sid)
        try:
            self._run_inner(s)
        except _Stop:
            pass
        except model.ModelUnavailable as e:
            s["status"] = "waiting_for_model"
            s["message"] = (f"stopped during {s['phase']}: {e}. Nothing is lost; say continue "
                            f"when the model is available again.")
            self.store.save_session(s)
            self.store.update_domain(s["domain_id"], status=self._domain_state(s["domain_id"]))
            _emit("stopped", session_id=sid, domain=s["domain"], reason=str(e))
        except Exception as e:
            s["status"], s["message"] = "failed", f"{type(e).__name__}: {e}"[:300]
            self.store.save_session(s)
            self.store.update_domain(s["domain_id"], status=self._domain_state(s["domain_id"]))
            self.store.event("learning_failed", s["domain"], s["message"])
            _emit("failed", session_id=sid, domain=s["domain"], reason=s["message"])

    def _domain_state(self, domain_id: str) -> str:
        """A domain keeps its last verified state while a later session fails."""
        dom = self.store.domain(domain_id) or {}
        v = dom.get("verification") or {}
        if v.get("verdict") == "learned":
            return "learned"
        return "unverified" if dom.get("item_count") else "failed"

    def _run_inner(self, s: dict) -> None:
        store, did = self.store, s["domain_id"]
        rules = store.priority_rules()

        def rank_of(rel):
            return source_rank(rel, rules)

        # 1-2. discover and classify; compare with what was learned before
        self._phase(s, PHASES[0])
        files = inventory.scan(s["source"])
        summ = inventory.summary(files)
        k = store.knowledge(did)
        prev = k.get("files", {})
        now = {f.rel: f for f in files if f.status != "skipped"}
        new = [r for r in now if r not in prev]
        changed = [r for r in now if r in prev and prev[r].get("checksum") != now[r].checksum]
        deleted = [r for r in prev if r not in now and prev[r].get("status") == "processed"]
        todo = [now[r] for r in sorted(new + changed) if now[r].checksum not in s["extracted"]]
        s["skipped"] = {f.rel: f.reason for f in files if f.status == "skipped"}
        self._phase(s, PHASES[1], discovered=summ["discovered"], by_kind=summ["by_kind"],
                    skipped=summ["skipped"], new=len(new), changed=len(changed), deleted=len(deleted),
                    unchanged=len(now) - len(new) - len(changed), to_process=len(todo))

        # retire what changed or disappeared, before learning its replacement
        gone = set(changed) | set(deleted)
        if gone:
            for it in k["items"]:
                it["support"] = [x for x in it["support"] if x["rel"] not in gone]
                if not it["support"] and it.get("status") == "active":
                    it["status"] = "retired"
            for r in deleted:
                prev[r]["status"] = "deleted"
            store.save_knowledge(did, k)
            if self.index:
                self._unindex(did, gone)

        # 3-4. read documents, prepare images
        docs = [f for f in todo if f.kind == "document"]
        imgs = [f for f in todo if f.kind == "image"]
        sources, n = [], 0
        self._phase(s, PHASES[2], documents=len(docs))
        for f in docs:
            try:
                text, cut = extract.read_document(f.path)
            except Exception as e:
                s["failed"][f.rel] = f"could not be read: {e}"[:200]
                continue
            n += 1
            sources.append(extract.Source(f"S{len(sources)+1}", f.rel, f.checksum, f.mtime,
                                          "document", text=text, truncated=cut))
            if self.index:
                self._index_file(did, f)
        self._phase(s, PHASES[3], documents_read=n, images=len(imgs))
        for f in imgs:
            try:
                sources.append(extract.Source(f"S{len(sources)+1}", f.rel, f.checksum, f.mtime,
                                              "image", image=extract.load_image(f.path, f.mime)))
            except Exception as e:
                s["failed"][f.rel] = f"image could not be opened: {e}"[:200]

        # 5. extract, batch by batch, saving after each (resumable)
        batches = extract.batches(sources)
        for i, b in enumerate(batches, start=1):
            self._phase(s, PHASES[4], batch=f"{i} of {len(batches)}")
            try:
                r = extract.extract_batch(s["domain"], b, rank_of)
            except ValueError as e:                       # the model answered, but not usefully
                for src in b:
                    s["failed"][src.rel] = f"the model's answer could not be used ({e})"
                continue
            s["raw_items"] += r.items
            s["raw_contradictions"] += r.contradictions
            s["image_notes"].update(r.image_notes)
            s["extracted"] += [src.checksum for src in b]
            if r.model not in s["models"]:
                s["models"].append(r.model)
            store.save_session(s)
        if self.index:
            for rel, note in s["image_notes"].items():
                self._index_text(did, rel, note)
        done = set(s["extracted"])
        s["counts"]["documents_analyzed"] = sum(1 for f in now.values() if f.kind == "document" and f.checksum in done)
        s["counts"]["images_analyzed"] = sum(1 for f in now.values() if f.kind == "image" and f.checksum in done)

        # 6-8. cross-reference with what was known, resolve, build the model
        self._phase(s, PHASES[5], items_extracted=len(s["raw_items"]))
        keep = [i for i in k["items"] if i.get("status") == "active"]
        for i in keep:
            i.pop("contested", None)
        merged = consolidate.merge(keep + s["raw_items"])
        self._phase(s, PHASES[6], items_after_merge=len(merged))
        conflicts = consolidate.find_conflicts(s["domain"], merged)
        contradictions = consolidate.resolve(merged, conflicts, s["raw_contradictions"])
        retired = [i for i in k["items"] if i.get("status") != "active"]
        for rel, f in now.items():
            if rel in s["failed"]:
                prev[rel] = {"checksum": f.checksum, "mtime": f.mtime, "kind": f.kind,
                             "status": "failed", "reason": s["failed"][rel]}
            elif f.checksum in done or rel not in prev or prev[rel].get("checksum") == f.checksum:
                prev[rel] = {"checksum": f.checksum, "mtime": f.mtime, "kind": f.kind,
                             "status": "processed", "reason": ""}
        k = {"items": merged + retired, "contradictions": contradictions, "files": prev}
        active = [i for i in merged if i["status"] == "active"]
        self._phase(s, PHASES[7],
                    principles=sum(1 for i in active if i["kind"] == "principle"),
                    preferences=sum(1 for i in active if i["kind"] in ("preference", "rule")),
                    patterns=sum(1 for i in active if i["kind"] == "pattern"),
                    facts=sum(1 for i in active if i["kind"] == "fact"),
                    contradictions=len(contradictions),
                    unresolved=sum(1 for c in contradictions if not c.get("resolved_by")),
                    failed=len(s["failed"]))
        store.save_knowledge(did, k)

        # 9. verify
        self._phase(s, PHASES[8])
        result = verify.run(did, store=store,
                            progress=lambda what: _emit("progress", session_id=s["id"], domain=s["domain"],
                                                        phase=PHASES[8], detail=what))
        s["verification"] = {k2: v for k2, v in result.items() if k2 != "tests"}
        s["verification"]["tests"] = [{x: t.get(x) for x in ("id", "kind", "pass", "reason", "question")}
                                      for t in result["tests"]]

        # 10-11. register
        self._phase(s, PHASES[9])
        verdict = result["verdict"] if result["verdict"] in ("learned", "unverified") else "failed"
        store.update_domain(did, status=verdict, verification=result, last_session=s["id"])
        s["status"], s["phase"] = "done", PHASES[10]
        words = {"learned": "Learned and verified", "unverified": "Analyzed but NOT verified",
                 "failed": "Could not learn"}[verdict]
        s["message"] = (f"{words}: {s['domain']} — {result['passed']}/{result['total']} checks passed"
                        + (f"; {len(s['failed'])} file(s) failed" if s["failed"] else "")
                        + (f"; {s['counts'].get('unresolved', 0)} unresolved contradiction(s)"
                           if s["counts"].get("unresolved") else ""))
        store.save_session(s)
        store.event("learned" if verdict == "learned" else "learning_" + verdict, s["domain"], s["message"])
        _emit("finished", session_id=s["id"], domain=s["domain"], verdict=verdict, message=s["message"])

    # ── source index (for grounded passages) ─────────────────────────────────
    def _index_file(self, did: str, f) -> None:
        try:
            from nova_core.rag.library import library
            library().add_file(f.path, scope=f"knowledge:{did}", source=f.rel, keep_copy=False)
        except Exception:
            pass

    def _index_text(self, did: str, rel: str, text: str) -> None:
        try:
            from nova_core.rag.library import library
            library().add_text(text, scope=f"knowledge:{did}", title=rel, source=rel)
        except Exception:
            pass

    def _unindex(self, did: str, rels) -> None:
        try:
            from nova_core.rag.library import library
            lib = library()
            for d in lib.documents([f"knowledge:{did}"]):
                if rels is None or d.source in rels:
                    lib.forget(d.id)
        except Exception:
            pass


_service: Optional[LearningService] = None
_service_dir = ""


def service() -> LearningService:
    """One service per signed-in account (NOVA_DATA_DIR is bound at sign-in)."""
    global _service, _service_dir
    d = os.getenv("NOVA_DATA_DIR", "")
    if _service is None or d != _service_dir:
        _service, _service_dir = LearningService(), d
        _service.mark_interrupted()
    return _service


__all__ = ["LearningService", "service", "on_event", "PHASES"]
