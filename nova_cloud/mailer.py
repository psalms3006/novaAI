"""nova_cloud.mailer — account email that actually sends.

Providers are selected by `EMAIL_PROVIDER`:

    resend    Resend HTTP API (recommended; set RESEND_API_KEY)
    smtp      any SMTP server (Zoho, Google Workspace, Fastmail, ...)
    console   print to the log -- development only, never in production
    none      no delivery configured (the default)

The honesty rule this module exists to enforce: **NOVA never claims to have
sent an email it did not send.** When no provider is configured, verification
is not silently skipped and the UI is not told to check an inbox. The account
is created unverified, the caller is told delivery is unconfigured, and
`/v1/auth/email/resend` works the moment credentials appear.

Delivery happens on a background thread, because a user creating an account
should not wait on a third-party API, and a provider outage must not take
sign-up down with it. Permanent failures are recorded as EMAIL_DELIVERY_FAILED
error events, so an operator can see them in the admin console rather than
discovering them from a confused user.
"""
from __future__ import annotations

import html
import logging
import os
import queue
import smtplib
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

log = logging.getLogger("nova.mail")

_SEND_TIMEOUT_S = 15.0
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = (2.0, 8.0)


class EmailNotConfigured(RuntimeError):
    """No provider is configured. Never swallowed into a fake success."""


@dataclass
class EmailConfig:
    provider: str
    from_address: str
    support_email: str
    admin_email: str
    base_url: str
    brand: str
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True

    @property
    def configured(self) -> bool:
        if self.provider == "resend":
            return bool(self.resend_api_key and self.from_address)
        if self.provider == "smtp":
            return bool(self.smtp_host and self.from_address)
        if self.provider == "console":
            return True
        return False


def email_config() -> EmailConfig:
    """Read the environment. Every address is configurable; no domain is
    hard-coded anywhere in the codebase."""
    def _s(name: str, default: str = "") -> str:
        return (os.getenv(name, "") or default).strip()

    brand = _s("NOVA_BRAND", "NOVA")
    support = _s("SUPPORT_EMAIL")
    admin = _s("ADMIN_EMAIL")
    frm = _s("EMAIL_FROM") or (f"{brand} <{support}>" if support else "")
    try:
        port = int(_s("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    return EmailConfig(
        provider=_s("EMAIL_PROVIDER", "none").lower(),
        from_address=frm,
        support_email=support,
        admin_email=admin,
        base_url=_s("APP_BASE_URL").rstrip("/"),
        brand=brand,
        resend_api_key=_s("RESEND_API_KEY"),
        smtp_host=_s("SMTP_HOST"),
        smtp_port=port,
        smtp_user=_s("SMTP_USER"),
        smtp_password=_s("SMTP_PASSWORD"),
        smtp_starttls=_s("SMTP_STARTTLS", "1") not in ("0", "false", "no"),
    )


# -- providers ---------------------------------------------------------------

_tls_ready = False


def _ensure_tls() -> None:
    """Route outbound HTTPS through the OS trust store.

    Antivirus products and corporate proxies that inspect TLS present their
    own certificate, which certifi does not know about, so every request to
    the mail provider fails with CERTIFICATE_VERIFY_FAILED. This is the same
    fix the desktop already applies; without it, email works on some machines
    and silently fails on others.
    """
    global _tls_ready
    if _tls_ready:
        return
    try:
        import nova_tls
        nova_tls.ensure_tls_trust()
    except Exception:
        pass                      # certifi still works where nothing intercepts
    _tls_ready = True


def _send_resend(cfg: EmailConfig, to: str, subject: str,
                 text: str, html_body: str) -> None:
    _ensure_tls()
    import requests
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {cfg.resend_api_key}",
                 "Content-Type": "application/json"},
        json={"from": cfg.from_address, "to": [to], "subject": subject,
              "text": text, "html": html_body},
        timeout=_SEND_TIMEOUT_S,
    )
    if r.status_code >= 400:
        # Include the status, never the API key.
        raise RuntimeError(f"resend rejected the message ({r.status_code})")


def _send_smtp(cfg: EmailConfig, to: str, subject: str,
               text: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.from_address
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                                  timeout=_SEND_TIMEOUT_S)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port,
                              timeout=_SEND_TIMEOUT_S)
    try:
        if cfg.smtp_port != 465 and cfg.smtp_starttls:
            server.starttls()
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _send_console(cfg: EmailConfig, to: str, subject: str,
                  text: str, html_body: str) -> None:
    log.info("[MAIL:console] to=%s subject=%s\n%s", to, subject, text)


_PROVIDERS: dict[str, Callable[..., None]] = {
    "resend": _send_resend,
    "smtp": _send_smtp,
    "console": _send_console,
}


# -- templates ---------------------------------------------------------------
#
# Plain, short and free of marketing. A security email should read like one.

_WRAP = """<!doctype html><html><body style="margin:0;padding:0;background:#f5f6f8">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#f5f6f8;padding:32px 16px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="max-width:480px;background:#ffffff;border-radius:12px;
              border:1px solid #e4e6eb;padding:32px;
              font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1a1d24">
<tr><td>
<div style="font-size:18px;font-weight:600;letter-spacing:.08em">{brand}</div>
<h1 style="font-size:19px;font-weight:600;margin:22px 0 12px">{heading}</h1>
{body}
</td></tr></table>
<div style="max-width:480px;margin-top:18px;font-size:12px;color:#7a8090;
            font-family:-apple-system,Segoe UI,Roboto,sans-serif;text-align:center">
{footer}
</div>
</td></tr></table></body></html>"""

_BUTTON = """<p style="margin:24px 0">
<a href="{url}" style="display:inline-block;background:#2f6feb;color:#ffffff;
   text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600">{label}</a>
</p>
<p style="font-size:13px;color:#5c6270;margin:0 0 4px">
Or paste this into your browser:</p>
<p style="font-size:12px;color:#7a8090;word-break:break-all;margin:0">{url}</p>"""


def _render(cfg: EmailConfig, heading: str, body_html: str) -> str:
    footer = f"Sent by {html.escape(cfg.brand)}."
    if cfg.support_email:
        footer += (f" Questions? <a href='mailto:{html.escape(cfg.support_email)}' "
                   f"style='color:#7a8090'>{html.escape(cfg.support_email)}</a>")
    return _WRAP.format(brand=html.escape(cfg.brand), heading=html.escape(heading),
                        body=body_html, footer=footer)


def _link(cfg: EmailConfig, path: str, token: str) -> str:
    base = cfg.base_url or ""
    return f"{base}{path}?token={token}"


def verification_email(cfg: EmailConfig, token: str) -> tuple[str, str, str]:
    url = _link(cfg, "/verify", token)
    subject = f"Verify your {cfg.brand} account"
    text = (f"Welcome to {cfg.brand}.\n\n"
            f"Confirm this address to finish setting up your account:\n{url}\n\n"
            "This link expires in 24 hours. If you did not create a "
            f"{cfg.brand} account, you can ignore this message.\n")
    body = ("<p style='font-size:14px;line-height:1.6;margin:0'>"
            "Confirm this address to finish setting up your account.</p>"
            + _BUTTON.format(url=html.escape(url), label="Verify email") +
            "<p style='font-size:13px;color:#5c6270;margin:22px 0 0'>"
            "This link expires in 24 hours. If you did not create an account, "
            "you can ignore this message.</p>")
    return subject, text, _render(cfg, "Verify your email", body)


def reset_email(cfg: EmailConfig, token: str) -> tuple[str, str, str]:
    url = _link(cfg, "/reset", token)
    subject = f"Reset your {cfg.brand} password"
    text = (f"Someone asked to reset the password for this {cfg.brand} "
            f"account.\n\n{url}\n\n"
            "This link expires in 1 hour and can be used once. If this was "
            "not you, no action is needed -- your password has not changed.\n")
    body = ("<p style='font-size:14px;line-height:1.6;margin:0'>"
            "Someone asked to reset the password for this account.</p>"
            + _BUTTON.format(url=html.escape(url), label="Reset password") +
            "<p style='font-size:13px;color:#5c6270;margin:22px 0 0'>"
            "This link expires in one hour and can be used once. If this was "
            "not you, no action is needed &mdash; your password has not "
            "changed.</p>")
    return subject, text, _render(cfg, "Reset your password", body)


def new_device_email(cfg: EmailConfig, device_name: str, platform: str,
                     when: str) -> tuple[str, str, str]:
    subject = f"New sign-in to your {cfg.brand} account"
    text = (f"{cfg.brand} was signed in on a new device.\n\n"
            f"Device:   {device_name}\nPlatform: {platform}\nWhen:     {when}\n\n"
            "If this was you, nothing to do. If it was not, change your "
            "password and remove the device from your account settings.\n")
    body = (f"<p style='font-size:14px;line-height:1.6;margin:0'>"
            f"{html.escape(cfg.brand)} was signed in on a new device.</p>"
            "<table style='font-size:14px;margin:20px 0;border-collapse:collapse'>"
            f"<tr><td style='color:#7a8090;padding:3px 18px 3px 0'>Device</td>"
            f"<td>{html.escape(device_name)}</td></tr>"
            f"<tr><td style='color:#7a8090;padding:3px 18px 3px 0'>Platform</td>"
            f"<td>{html.escape(platform)}</td></tr>"
            f"<tr><td style='color:#7a8090;padding:3px 18px 3px 0'>When</td>"
            f"<td>{html.escape(when)}</td></tr></table>"
            "<p style='font-size:13px;color:#5c6270;margin:0'>"
            "If this was you, there is nothing to do. If not, change your "
            "password and remove the device from your account settings.</p>")
    return subject, text, _render(cfg, "New device sign-in", body)


# -- the sender --------------------------------------------------------------

class Mailer:
    """Background sender with bounded retries.

    Sending inline would put a third-party API on the sign-up path. Queuing it
    keeps sign-up fast and survives a provider blip, at the cost of the send
    being eventually-consistent -- which is the right trade for an email a
    human reads seconds later.
    """

    def __init__(self, cfg: EmailConfig | None = None):
        self.cfg = cfg or email_config()
        self._q: queue.Queue = queue.Queue(maxsize=1000)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.sent = 0
        self.failed = 0

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nova-mailer",
                                        daemon=True)
        self._thread.start()

    def send(self, to: str, subject: str, text: str, html_body: str,
             *, kind: str = "generic") -> None:
        """Queue one message. Raises if no provider is configured, so a caller
        can tell the user the truth rather than implying delivery."""
        if not self.configured:
            raise EmailNotConfigured(
                f"No email provider configured (EMAIL_PROVIDER={self.cfg.provider})")
        self._ensure_worker()
        try:
            self._q.put_nowait({"to": to, "subject": subject, "text": text,
                                "html": html_body, "kind": kind, "attempts": 0})
        except queue.Full:
            self.failed += 1
            log.error("[MAIL] queue full; dropped %s to %s", kind, _mask(to))
            _record_failure(kind, "queue_full")

    def send_now(self, to: str, subject: str, text: str, html_body: str) -> None:
        """Synchronous send. Used by the operator CLI to test configuration,
        where a caller genuinely wants the error."""
        if not self.configured:
            raise EmailNotConfigured("No email provider configured")
        _PROVIDERS[self.cfg.provider](self.cfg, to, subject, text, html_body)
        self.sent += 1

    def flush(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and not self._q.empty():
            time.sleep(0.1)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            self._deliver(item)

    def _deliver(self, item: dict) -> None:
        fn = _PROVIDERS.get(self.cfg.provider)
        if fn is None:
            self.failed += 1
            return
        try:
            fn(self.cfg, item["to"], item["subject"], item["text"], item["html"])
            self.sent += 1
            log.info("[MAIL] sent %s to %s", item["kind"], _mask(item["to"]))
            return
        except Exception as e:
            item["attempts"] += 1
            if item["attempts"] < _MAX_ATTEMPTS:
                delay = _RETRY_BACKOFF_S[min(item["attempts"] - 1,
                                             len(_RETRY_BACKOFF_S) - 1)]
                threading.Timer(delay, lambda: self._q.put(item)).start()
                return
            self.failed += 1
            # The address is masked and the error type recorded, never the
            # message body or any credential.
            log.error("[MAIL] giving up on %s to %s: %s",
                      item["kind"], _mask(item["to"]), type(e).__name__)
            _record_failure(item["kind"], type(e).__name__)


def _mask(address: str) -> str:
    """Log which account, not the whole address."""
    if not address or "@" not in address:
        return "***"
    name, _, domain = address.partition("@")
    keep = name[:2] if len(name) > 2 else name[:1]
    return f"{keep}***@{domain}"


def _record_failure(kind: str, error: str) -> None:
    """Surface delivery failure in the admin console rather than only the log."""
    try:
        from .db import session_scope
        from .telemetry_sink import record_error
        with session_scope() as s:
            record_error(s, user_id=None, device_id=None,
                         code="EMAIL_DELIVERY_FAILED",
                         context={"reason": error[:60], "source": kind[:40]})
    except Exception:
        pass


_mailer: Mailer | None = None
_lock = threading.Lock()


def mailer() -> Mailer:
    global _mailer
    with _lock:
        if _mailer is None:
            _mailer = Mailer()
        return _mailer


def reset_mailer() -> None:
    """Test hook: re-read the environment."""
    global _mailer
    with _lock:
        if _mailer is not None:
            _mailer.stop()
        _mailer = None


__all__ = ["Mailer", "EmailConfig", "EmailNotConfigured", "email_config",
           "mailer", "reset_mailer", "verification_email", "reset_email",
           "new_device_email"]
