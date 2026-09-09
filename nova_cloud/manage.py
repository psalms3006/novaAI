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
