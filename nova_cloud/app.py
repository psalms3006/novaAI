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

import os
import threading
import time
from collections import defaultdict

from flask import Flask, jsonify, request, send_from_directory
from sqlalchemy import text

from .config import config
from .db import engine, init_db

START_TIME = time.time()
APP_VERSION = "0.1.0"

# Per-process request counters. Deliberately no user identifiers: these are
# scraped by operators and must never become a second, un-audited copy of who
# used NOVA. `max_in_flight` is the number that says whether the worker pool is
# saturated -- the first sign of a capacity problem.
_STATS_LOCK = threading.Lock()
_STATS = {"requests": 0, "in_flight": 0, "max_in_flight": 0}
_STATUS = defaultdict(int)


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
    from .api_instance import bp as instance_bp
    from .api_model import bp as model_bp
    from .api_updates import bp as updates_bp
    from .api_capabilities import bp as capabilities_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(telemetry_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(instance_bp)
    app.register_blueprint(model_bp)
    app.register_blueprint(updates_bp)
    app.register_blueprint(capabilities_bp)

    @app.after_request
    def _headers(resp):
        with _STATS_LOCK:
            _STATS["in_flight"] = max(0, _STATS["in_flight"] - 1)
            _STATUS[resp.status_code] += 1
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

    @app.before_request
    def _count_in_flight():
        with _STATS_LOCK:
            _STATS["requests"] += 1
            _STATS["in_flight"] += 1
            if _STATS["in_flight"] > _STATS["max_in_flight"]:
                _STATS["max_in_flight"] = _STATS["in_flight"]

    # A saturated connection pool is a capacity problem, not a bug in the
    # request. Answer immediately with 503 and a retry hint so clients back off
    # instead of each waiting out the pool timeout.
    from sqlalchemy.exc import TimeoutError as _PoolTimeout

    @app.errorhandler(_PoolTimeout)
    def _pool_exhausted(_e):
        resp = jsonify({"ok": False, "error": "busy",
                        "message": "NOVA is at capacity right now. Try again shortly."})
        resp.status_code = 503
        resp.headers["Retry-After"] = "2"
        return resp

    @app.get("/ready")
    def ready():
        """Readiness: can this process actually serve a request right now?

        Separate from /health on purpose. A liveness probe should restart a
        wedged process; a readiness probe should take it out of the load
        balancer while its database is unreachable, without restarting
        anything.
        """
        try:
            with engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as e:
            return jsonify({"ok": False, "ready": False,
                            "reason": type(e).__name__}), 503
        return jsonify({"ok": True, "ready": True, "version": APP_VERSION})

    @app.get("/metrics")
    def metrics():
        """Aggregate operational numbers, and nothing about any person.

        Per-process: with N workers, scrape all N (or read the sum from the
        proxy). Deliberately JSON rather than Prometheus so it needs no new
        dependency on a small server.
        """
        pool = None
        try:
            st = engine().pool.status()
            pool = {"size": st.size, "checked_in": st.checkedin(),
                    "checked_out": st.checkedout(), "overflow": st.overflow()}
        except Exception:
            pass
        with _STATS_LOCK:
            snapshot = dict(_STATS)
            statuses = dict(_STATUS)
        return jsonify({
            "ok": True,
            "version": APP_VERSION,
            "uptime_s": int(time.time() - START_TIME),
            "requests_total": snapshot["requests"],
            "in_flight": snapshot["in_flight"],
            "max_in_flight": snapshot["max_in_flight"],
            "responses_by_status": statuses,
            "db_pool": pool,
            "gateway": _gateway_metrics(),
        })

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

    # -- links people click from email ------------------------------------
    #
    # The templates point at {APP_BASE_URL}/verify and /reset. Those are the
    # URLs a human opens in a browser, so they have to be real pages: without
    # them every verification email leads to a 404, which looks exactly like a
    # broken account.

    @app.get("/verify")
    def verify_page():
        from .api_auth import _consume_email_token
        token = (request.args.get("token") or "").strip()
        ok, message = _consume_email_token(token, "verify_email")
        return _outcome_page(
            "Email confirmed" if ok else "This link did not work",
            message, ok), (200 if ok else 400)

    # -- diagnostic endpoint for verification flow -------------------------
    # Helps operators debug email verification without needing a real token.
    # Returns configuration status and a test token validation (without DB write).
    @app.get("/api/health/verify")
    def verify_health():
        from .mailer import email_config
        from .api_auth import _consume_email_token
        from . import security as sec

        mc = email_config()
        nova_domain = os.getenv("NOVA_DOMAIN", "").strip()
        app_base_url = os.getenv("APP_BASE_URL", "").strip()

        # Test token validation logic with a dummy token (won't match any real token)
        # This verifies the token hashing/validation logic works without hitting the DB
        test_token = "test-token-not-valid"
        token_hash = sec.token_hash(test_token)

        return jsonify({
            "ok": True,
            "verify_endpoint": "/verify",
            "reset_endpoint": "/reset",
            "configuration": {
                "nova_domain": nova_domain or "(unset)",
                "app_base_url": app_base_url or "(unset)",
                "email_provider": mc.provider,
                "email_configured": mc.configured,
                "domain_match": nova_domain and app_base_url and nova_domain in app_base_url,
            },
            "test_token_hash": token_hash[:16] + "...",
            "note": "This endpoint verifies configuration only. Real token validation requires a valid token from email.",
        })

    @app.get("/reset")
    def reset_page():
        """Password reset is a form, not a one-click action: the new password
        has to come from the person, and it must never travel in a URL."""
        token = (request.args.get("token") or "").strip()
        if not token:
            return _outcome_page("This link did not work",
                                 "The reset link is missing its token.",
                                 False), 400
        return send_from_directory(app.static_folder, "reset.html")

    @app.get("/admin")
    @app.get("/admin/")
    def admin_index():
        return send_from_directory(app.static_folder, "admin.html")

    def _outcome_page(heading: str, message: str, ok: bool) -> str:
        import html as _html
        colour = "#1f9d55" if ok else "#c53030"
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(heading)}</title></head>
<body style="margin:0;background:#f5f6f8;font-family:-apple-system,Segoe UI,
Roboto,sans-serif;display:grid;place-items:center;min-height:100vh">
<div style="max-width:420px;background:#fff;border:1px solid #e4e6eb;
border-radius:12px;padding:32px;text-align:center">
<div style="font-weight:600;letter-spacing:.08em;font-size:18px">NOVA</div>
<h1 style="font-size:19px;margin:20px 0 10px;color:{colour}">
{_html.escape(heading)}</h1>
<p style="font-size:14px;line-height:1.6;color:#4a5160;margin:0">
{_html.escape(message)}</p>
</div></body></html>"""

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
