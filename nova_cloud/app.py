"""nova_cloud.app — the Flask application factory.

One process serves three surfaces, kept in separate blueprints with separate
authorisation:

    /v1/...        the NOVA desktop client (user tokens)
    /admin/api/... the admin control plane (admin tokens, different key)
    /admin/...     the admin web application (static)

They share a database, not a trust domain: nothing in /v1 can reach an admin
route, because the guard verifies a different signing key and audience.
"""
from __future__ import annotations

import time

from flask import Flask, jsonify, request, send_from_directory
from sqlalchemy import text

from .config import config
from .db import engine, init_db

START_TIME = time.time()
APP_VERSION = "0.1.0"


def create_app(cfg=None) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    cfg = cfg or config()
    app.config["NOVA_CFG"] = cfg
    app.config["JSON_SORT_KEYS"] = False
    # Bodies are JSON metadata; a megabyte is already generous.
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

    init_db()

    from .api_auth import bp as auth_bp
    from .api_devices import bp as devices_bp
    from .api_sync import bp as sync_bp
    from .api_telemetry import bp as telemetry_bp
    from .api_admin import bp as admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(telemetry_bp)
    app.register_blueprint(admin_bp)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'")
        if cfg.is_production:
            resp.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
        return resp

    @app.get("/health")
    def health():
        """Real health: the database is actually queried, not assumed."""
        checks = {}
        ok = True
        t0 = time.time()
        try:
            with engine().connect() as c:
                c.execute(text("SELECT 1"))
            checks["database"] = {"status": "healthy",
                                  "latency_ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            ok = False
            checks["database"] = {"status": "degraded", "error": type(e).__name__}
        checks["api"] = {"status": "healthy"}

        # Email is reported, not assumed. "configured" means credentials are
        # present and a provider is selected -- it does not claim a message
        # has ever been delivered, which only a real send can establish.
        try:
            from .mailer import email_config
            mc = email_config()
            if mc.configured:
                checks["email"] = {"status": "healthy", "provider": mc.provider,
                                   "sender_configured": bool(mc.from_address)}
            else:
                # Not an outage: a deployment may legitimately run without
                # email. It becomes degraded only when verification is
                # mandatory and therefore cannot complete.
                mandatory = cfg.require_email_verification
                checks["email"] = {
                    "status": "degraded" if mandatory else "not_configured",
                    "provider": mc.provider,
                    "detail": ("email verification is required but no provider "
                               "is configured, so no one can finish signing up"
                               if mandatory else
                               "no provider configured; verification and reset "
                               "emails will not be sent"),
                }
                if mandatory:
                    ok = False
        except Exception as e:
            checks["email"] = {"status": "unknown", "error": type(e).__name__}

        return jsonify({
            "ok": ok,
            "status": "healthy" if ok else "degraded",
            "version": APP_VERSION,
            "uptime_s": int(time.time() - START_TIME),
            "checks": checks,
        }), (200 if ok else 503)

    @app.get("/admin")
    @app.get("/admin/")
    def admin_index():
        return send_from_directory(app.static_folder, "admin.html")

    @app.errorhandler(404)
    def _404(_e):
        return jsonify({"ok": False, "error": "not_found"}), 404

    @app.errorhandler(413)
    def _413(_e):
        return jsonify({"ok": False, "error": "payload_too_large"}), 413

    @app.errorhandler(500)
    def _500(e):
        # Never leak internals to a client; the detail belongs in the log.
        app.logger.exception("unhandled error on %s", request.path)
        return jsonify({"ok": False, "error": "internal_error"}), 500

    return app


def main() -> None:
    import os
    app = create_app()
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "127.0.0.1")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
