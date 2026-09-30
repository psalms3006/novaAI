"""desk.skills_api — NOVA's skills, as the person sees and controls them.

    GET    /api/skills                     skills, timeline, pending
    POST   /api/skills/<id>/credential     {provider_id, secret}  connect an account
    POST   /api/skills/<id>/test           prove it again
    POST   /api/skills/<id>/rollback       back to the last working version
    DELETE /api/skills/<id>                remove (and forget its stored keys)
    POST   /api/skills/check               re-check every skill's health now

The credential route is the only way a key reaches NOVA: typed into the
window, stored in the OS credential store, never shown to the model and never
returned by any route. Events (capability.*) go to the window's event stream.

A background sweep re-checks learned skills' health every few hours, so a
service that went down or an expired connection shows up before the person
asks for it -- not when it fails in the middle of something.
"""
from __future__ import annotations

import logging
import threading

from flask import jsonify, request

log = logging.getLogger("nova.desk.skills")

SWEEP_FIRST_S = 600
SWEEP_EVERY_S = 6 * 3600
_sweeper: threading.Thread | None = None
_stop = threading.Event()


def _svc():
    from nova_skills import model_tool
    return model_tool.service()


def sweep_once() -> list:
    svc = _svc()
    if not any(c.learned for c in svc.registry.all()):
        return []
    return svc.health_sweep()


def _sweep_loop() -> None:
    delay = SWEEP_FIRST_S
    while not _stop.wait(delay):
        delay = SWEEP_EVERY_S
        try:
            changed = sweep_once()
            if changed:
                log.info("[SKILLS] health changed: %s", changed)
        except Exception as e:
            log.warning("[SKILLS] health sweep failed: %s", e)


def start_sweeper() -> None:
    global _sweeper
    if _sweeper is None or not _sweeper.is_alive():
        _stop.clear()
        _sweeper = threading.Thread(target=_sweep_loop, name="nova-skills-health", daemon=True)
        _sweeper.start()


def register(app, require_token, *, sweep: bool = True) -> None:
    @app.get("/api/skills")
    @require_token
    def api_skills():
        return jsonify({"ok": True, **_svc().overview()})

    @app.post("/api/skills/<cap_id>/credential")
    @require_token
    def api_skill_credential(cap_id):
        body = request.get_json(silent=True) or {}
        provider_id = str(body.get("provider_id") or "").strip()
        secret = str(body.get("secret") or "").strip()
        if not provider_id or not secret:
            return jsonify({"ok": False, "error": "provider_id and secret are required"}), 400
        svc = _svc()
        cap = svc.registry.get(cap_id)
        if cap is None or provider_id not in {p["id"] for p in cap.providers}:
            return jsonify({"ok": False, "error": "unknown capability or provider"}), 404
        out = svc.provide_credential(cap_id, provider_id, secret)
        return jsonify({"ok": bool(out.get("ok")), "learned": out.get("learned", False),
                        "health": out.get("health", ""), "detail": out.get("detail", "")})

    @app.post("/api/skills/<cap_id>/test")
    @require_token
    def api_skill_test(cap_id):
        svc = _svc()
        if svc.registry.get(cap_id) is None:
            return jsonify({"ok": False, "error": "unknown capability"}), 404
        return jsonify(svc.test(cap_id))

    @app.post("/api/skills/<cap_id>/rollback")
    @require_token
    def api_skill_rollback(cap_id):
        try:
            return jsonify(_svc().rollback(cap_id))
        except (KeyError, ValueError) as e:
            return jsonify({"ok": False, "error": str(e).strip("'\"")}), 409

    @app.delete("/api/skills/<cap_id>")
    @require_token
    def api_skill_remove(cap_id):
        out = _svc().remove(cap_id)
        return jsonify(out), (200 if out.get("ok") else 404)

    @app.post("/api/skills/check")
    @require_token
    def api_skills_check():
        return jsonify({"ok": True, "changed": _svc().health_sweep()})

    if sweep:
        start_sweeper()


def attach_events(publish) -> None:
    """Forward capability.* events to the window's event stream."""
    from nova_skills import service
    service.on_event(publish)


__all__ = ["register", "attach_events", "sweep_once", "start_sweeper"]
