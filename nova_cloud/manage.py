"""nova_cloud.manage — operator commands.

    python -m nova_cloud.manage create-admin --email x@y --role SUPER_ADMIN
    python -m nova_cloud.manage list-admins
    python -m nova_cloud.manage set-role --email x@y --role SUPPORT
    python -m nova_cloud.manage retention [--dry-run]
    python -m nova_cloud.manage serve [--host --port]

The password is read from a prompt or NOVA_ADMIN_PASSWORD, never from a command
line argument, because arguments end up in shell history and process listings.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

from sqlalchemy import select

from . import security as sec
from .config import config
from .db import init_db, session_scope
from .models import (
    ActivityEvent, AdminAuditLog, AdminUser, ErrorEvent, ModelCall,
)


def _read_password() -> str:
    pw = os.getenv("NOVA_ADMIN_PASSWORD", "")
    if pw:
        return pw
    pw = getpass.getpass("Password: ")
    again = getpass.getpass("Confirm: ")
    if pw != again:
        print("Passwords do not match.", file=sys.stderr)
        raise SystemExit(2)
    return pw


def create_admin(args) -> int:
    init_db()
    email = sec.normalise_email(args.email)
    if not sec.valid_email(email):
        print("Invalid email address.", file=sys.stderr)
        return 2
    role = (args.role or "ANALYST").upper()
    if role not in sec.PERMISSIONS:
        print(f"Unknown role. Choose one of: {', '.join(sec.PERMISSIONS)}",
              file=sys.stderr)
        return 2
    password = _read_password()
    try:
        sec.check_password_policy(password)
    except sec.PasswordPolicyError as e:
        print(str(e), file=sys.stderr)
        return 2

    with session_scope() as s:
        if s.scalar(select(AdminUser).where(AdminUser.email == email)):
            print("That administrator already exists.", file=sys.stderr)
            return 1
        s.add(AdminUser(email=email, password_hash=sec.hash_password(password),
                        role=role))
    print(f"Created {email} ({role}).")
    if config().admin_require_mfa:
        print("Two-factor enrolment is required before this account can sign in.")
        print("Enrol at POST /admin/api/auth/mfa/enrol, or run:")
        print(f"  python -m nova_cloud.manage enrol-mfa --email {email}")
    return 0


def enrol_mfa(args) -> int:
    """Enrol an administrator in TOTP from the command line."""
    import pyotp
    init_db()
    email = sec.normalise_email(args.email)
    with session_scope() as s:
        admin = s.scalar(select(AdminUser).where(AdminUser.email == email))
        if admin is None:
            print("No such administrator.", file=sys.stderr)
            return 1
        secret = pyotp.random_base32()
        uri = pyotp.TOTP(secret).provisioning_uri(name=email,
                                                  issuer_name="NOVA Admin")
        print("\nAdd this to your authenticator app:\n")
        print(f"  Secret: {secret}")
        print(f"  URL:    {uri}\n")
        code = input("Enter the 6-digit code to confirm: ").strip()
        if not pyotp.TOTP(secret).verify(code, valid_window=1):
            print("That code did not verify. Nothing was changed.", file=sys.stderr)
            return 1
        admin.totp_secret_enc = sec.encrypt_totp_secret(secret)
        admin.mfa_enabled = True
    print("Two-factor authentication enabled.")
    return 0


def list_admins(_args) -> int:
    init_db()
    with session_scope() as s:
        rows = s.scalars(select(AdminUser)).all()
        if not rows:
            print("No administrators yet.")
            return 0
        print(f"{'EMAIL':<36} {'ROLE':<14} {'MFA':<6} STATUS")
        for a in rows:
            print(f"{a.email:<36} {a.role:<14} "
                  f"{'yes' if a.mfa_enabled else 'no':<6} {a.status}")
    return 0


def set_role(args) -> int:
    init_db()
    role = (args.role or "").upper()
    if role not in sec.PERMISSIONS:
        print(f"Unknown role. Choose one of: {', '.join(sec.PERMISSIONS)}",
              file=sys.stderr)
        return 2
    with session_scope() as s:
        admin = s.scalar(select(AdminUser).where(
            AdminUser.email == sec.normalise_email(args.email)))
        if admin is None:
            print("No such administrator.", file=sys.stderr)
            return 1
        admin.role = role
    print(f"{args.email} is now {role}.")
    return 0


def retention(args) -> int:
    """Delete data past its retention window.

    Retention is a scheduled job, not a background thread in the web process:
    it must be predictable and it must be possible to run it dry.
    """
    cfg = config()
    init_db()
    now = time.time()
    plan = [
        ("activity events", ActivityEvent, ActivityEvent.ts,
         cfg.retention_telemetry_d),
        ("model calls", ModelCall, ModelCall.ts, cfg.retention_telemetry_d),
        ("error events", ErrorEvent, ErrorEvent.ts, cfg.retention_errors_d),
        ("admin audit logs", AdminAuditLog, AdminAuditLog.ts,
         cfg.retention_admin_audit_d),
    ]
    total = 0
    with session_scope() as s:
        for label, model, column, days in plan:
            if not days:
                print(f"{label:<20} keep indefinitely")
                continue
            cutoff = now - days * 86400
            rows = s.scalars(select(model).where(column < cutoff)).all()
            print(f"{label:<20} {len(rows):>6} older than {days}d")
            total += len(rows)
            if not args.dry_run:
                for r in rows:
                    s.delete(r)
        if args.dry_run:
            s.rollback()
    print(("Would delete " if args.dry_run else "Deleted ") + f"{total} rows.")
    return 0


def test_email(args) -> int:
    """Prove the email configuration works before relying on it.

    Sends synchronously so a misconfiguration surfaces here rather than
    silently failing behind the background sender.
    """
    from . import mailer as mail
    cfg = mail.email_config()
    print(f"provider   : {cfg.provider}")
    print(f"from       : {cfg.from_address or '(unset)'}")
    print(f"support    : {cfg.support_email or '(unset)'}")
    print(f"base url   : {cfg.base_url or '(unset)'}")
    if not cfg.configured:
        print()
        print("Not configured. Set EMAIL_PROVIDER and its credentials; see "
              "nova_cloud/.env.example.", file=sys.stderr)
        return 1
    if not cfg.base_url:
        print()
        print("APP_BASE_URL is unset: verification and reset links would "
              "point nowhere.", file=sys.stderr)
        return 1

    mc = mail.Mailer(cfg)
    subject, text, html_body = mail.verification_email(cfg, "test-token-not-valid")
    try:
        mc.send_now(args.to, "[test] " + subject, text, html_body)
    except Exception as e:
        print()
        print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print()
    print(f"Sent a test verification email to {args.to}.")
    print("If it does not arrive, check the provider dashboard and your SPF/"
          "DKIM records.")
    return 0


def check(_args) -> int:
    """Report whether this deployment is actually ready to serve users."""
    from . import mailer as mail
    from sqlalchemy import text as _text
    from .db import engine as _engine

    cfg = config()
    problems, warnings = [], []

    print(f"environment        : {cfg.env}")
    print(f"database           : {_redact(cfg.database_url)}")

    try:
        with _engine().connect() as c:
            c.execute(_text("SELECT 1"))
        print("database connection: ok")
    except Exception as e:
        problems.append(f"cannot reach the database: {type(e).__name__}")
        print("database connection: FAILED")

    if cfg.database_url.startswith("sqlite") and cfg.is_production:
        problems.append("production is using SQLite; point DATABASE_URL at Postgres")

    mc = mail.email_config()
    print(f"email provider     : {mc.provider}"
          f"{'' if mc.configured else '  (not configured)'}")
    if cfg.require_email_verification and not mc.configured:
        problems.append("email verification is required but no provider is "
                        "configured; new users could not finish signing up")
    if mc.configured and not mc.base_url:
        problems.append("APP_BASE_URL is unset; email links would point nowhere")

    print(f"admin MFA required : {cfg.admin_require_mfa}")
    if not cfg.admin_require_mfa:
        warnings.append("admin MFA is disabled")

    init_db()
    with session_scope() as s:
        admins = s.scalars(select(AdminUser)).all()
    print(f"administrators     : {len(admins)}")
    if not admins:
        problems.append("no administrator exists; run create-admin")
    elif cfg.admin_require_mfa and not any(a.mfa_enabled for a in admins):
        problems.append("no administrator has enrolled in MFA, so none can "
                        "sign in; run enrol-mfa")

    for name in ("NOVA_SECRET_KEY", "NOVA_ADMIN_SECRET_KEY"):
        if cfg.is_production and not os.getenv(name):
            problems.append(f"{name} is not set")

    print()
    for w in warnings:
        print(f"WARNING  {w}")
    for p in problems:
        print(f"PROBLEM  {p}")
    if not problems:
        print("Ready." if not warnings else "Ready, with warnings above.")
    return 1 if problems else 0


def _redact(url: str) -> str:
    """Never print a database password, not even to the operator's terminal."""
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\g<1>:***@", url or "")


def serve(args) -> int:
    from .app import create_app
    app = create_app()
    print(f"NOVA Cloud on http://{args.host}:{args.port}")
    print(f"  admin console  http://{args.host}:{args.port}/admin")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="nova_cloud.manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create-admin")
    c.add_argument("--email", required=True)
    c.add_argument("--role", default="ANALYST")
    c.set_defaults(fn=create_admin)

    c = sub.add_parser("enrol-mfa")
    c.add_argument("--email", required=True)
    c.set_defaults(fn=enrol_mfa)

    sub.add_parser("list-admins").set_defaults(fn=list_admins)

    c = sub.add_parser("set-role")
    c.add_argument("--email", required=True)
    c.add_argument("--role", required=True)
    c.set_defaults(fn=set_role)

    c = sub.add_parser("test-email")
    c.add_argument("--to", required=True)
    c.set_defaults(fn=test_email)

    sub.add_parser("check").set_defaults(fn=check)

    c = sub.add_parser("retention")
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(fn=retention)

    c = sub.add_parser("serve")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8080)
    c.set_defaults(fn=serve)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
