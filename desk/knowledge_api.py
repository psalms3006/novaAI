"""desk.knowledge_api — what NOVA has been taught, as the person sees it.

    GET    /api/knowledge                          domains, recent sessions, timeline
    GET    /api/knowledge/domains/<id>             items with their sources, contradictions, files
    POST   /api/knowledge/learn                    {path, domain, scope, project_id}
    POST   /api/knowledge/sessions/<sid>/pause
    POST   /api/knowledge/sessions/<sid>/resume
    DELETE /api/knowledge/domains/<id>             forget a domain (and its index)

Progress is the session's real counts and phase (learning.* events on the
window's event stream), never an invented percentage.
"""
from __future__ import annotations

import logging

from flask import jsonify, request

log = logging.getLogger("nova.desk.knowledge")


def _svc():
    from nova_learning.service import service
    return service()


def _session_summary(s: dict) -> dict:
    v = s.get("verification") or {}
    return {"id": s["id"], "domain": s["domain"], "domain_id": s.get("domain_id"), "source": s["source"],
            "scope": s.get("scope"), "status": s["status"], "phase": s["phase"],
            "counts": s.get("counts", {}), "failed": s.get("failed", {}),
            "skipped": s.get("skipped", {}), "message": s.get("message", ""),
            "created": s.get("created"), "updated": s.get("updated"),
            "verification": ({"verdict": v.get("verdict"), "passed": v.get("passed"),
                              "total": v.get("total"), "tests": v.get("tests", [])} if v else None)}


def _domain_summary(d: dict) -> dict:
    out = {k: d.get(k) for k in ("id", "name", "scope", "project_id", "status", "sources",
                                 "item_count", "contradiction_count", "created", "updated")}
    v = d.get("verification") or {}
    out["verification"] = {k: v.get(k) for k in ("verdict", "passed", "total")}
    return out


def register(app, require_token) -> None:
    @app.get("/api/knowledge")
    @require_token
    def api_knowledge():
        svc = _svc()
        doms = sorted(svc.store.domains().values(), key=lambda d: -d.get("updated", 0))
        return jsonify({"ok": True, "domains": [_domain_summary(d) for d in doms],
                        "sessions": [_session_summary(s) for s in svc.store.sessions(10)],
                        "timeline": svc.store.timeline(40)})

    @app.get("/api/knowledge/domains/<domain_id>")
    @require_token
    def api_knowledge_domain(domain_id):
        svc = _svc()
        dom = svc.store.domain(domain_id)
        if not dom:
            return jsonify({"ok": False, "error": "unknown domain"}), 404
        k = svc.store.knowledge(domain_id)
        return jsonify({"ok": True, "domain": dom, "items": k.get("items", []),
                        "contradictions": k.get("contradictions", []), "files": k.get("files", {})})

    @app.post("/api/knowledge/learn")
    @require_token
    def api_knowledge_learn():
        body = request.get_json(silent=True) or {}
        path = str(body.get("path") or "").strip().strip('"')
        domain = str(body.get("domain") or "").strip()
        if not path or not domain:
            return jsonify({"ok": False, "error": "a folder and a name for what it teaches are required"}), 400
        try:
            s = _svc().learn(path, domain=domain, scope=str(body.get("scope") or "personal"),
                             project_id=str(body.get("project_id") or ""))
        except FileNotFoundError as e:
            return jsonify({"ok": False, "error": str(e)}), 404
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "session": _session_summary(s)})

    @app.post("/api/knowledge/sessions/<sid>/pause")
    @require_token
    def api_knowledge_pause(sid):
        s = _svc().pause(sid)
        if "error" in s:
            return jsonify({"ok": False, "error": s["error"]}), 404
        return jsonify({"ok": True, "session": _session_summary(s)})

    @app.post("/api/knowledge/sessions/<sid>/resume")
    @require_token
    def api_knowledge_resume(sid):
        try:
            s = _svc().resume(sid)
        except KeyError:
            return jsonify({"ok": False, "error": "unknown session"}), 404
        return jsonify({"ok": True, "session": _session_summary(s)})

    @app.delete("/api/knowledge/domains/<domain_id>")
    @require_token
    def api_knowledge_forget(domain_id):
        ok = _svc().forget(domain_id)
        return jsonify({"ok": ok}), (200 if ok else 404)


def attach_events(publish) -> None:
    from nova_learning import service
    service.on_event(publish)


__all__ = ["register", "attach_events"]
