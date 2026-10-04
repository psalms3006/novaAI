"""Reading the user's mail, and nothing else.

Deliberately read-only. The OAuth scope requested is `gmail.readonly`, so
even a compromised prompt cannot send, delete or archive anything: the token
NOVA holds is not capable of it. Sending would be a second connector, a second
scope and a second consent, which is the point of separating read from write
rather than asking for full access once and being trusted with it forever.

Three things this is careful about:

**Cost.** An inbox is large and the model does not need it. Gmail is queried
with a date filter, asked for `metadata` rather than full messages, and given
an explicit header allow-list. Bodies are never fetched; the snippet Gmail
already returns is enough to notice a deadline.

**Trust.** Mail is attacker-controlled. Nothing here interprets message
content as an instruction, and what leaves this module is a description built
from facts -- who sent it, how it was addressed, why it scored -- not the
sender's prose.

**Failure.** A provider outage, an expired token or a missing credential must
degrade to "I could not check your mail", never to a crash. Gmail being down
is not a reason for NOVA to stop working.

Connecting requires a Google Cloud OAuth client, which the account holder
creates; `authorise()` drives the consent flow and stores the refresh token in
the credential layer.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from integrations.accounts import AccountStore, Grant, get_account_store
from integrations.email_importance import Importance, Message, Verdict, classify

log = logging.getLogger("nova.gmail")

__all__ = ["GmailConnector", "GmailUnavailable", "SCOPES", "authorise"]

#: Read-only, on purpose. Widening this is a deliberate act requiring fresh
#: consent, not a configuration tweak.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

PROVIDER = "gmail"

#: Only these headers are requested. Everything else is either unnecessary or
#: is content, and content is not needed to decide whether to mention a
#: message to someone.
WANTED_HEADERS = ["From", "To", "Cc", "Subject", "Date",
                  "List-Unsubscribe", "Precedence", "In-Reply-To", "References"]


class GmailUnavailable(Exception):
    """Mail could not be checked. Never a crash, always this."""


@dataclass
class ScoredMessage:
    message: Message
    verdict: Verdict

    @property
    def level(self) -> Importance:
        return self.verdict.level


class GmailConnector:
    """Reads mail. Knows nothing about scheduling or speech."""

    kind = "email_sweep"

    def __init__(self, accounts: Optional[AccountStore] = None,
                 service_factory=None) -> None:
        self._accounts = accounts or get_account_store()
        #: Injected so the whole connector is testable without a network.
        self._service_factory = service_factory or self._build_service

    # ── connection ──────────────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        """What NOVA can say when asked whether mail is working."""
        described = self._accounts.describe(PROVIDER)
        ok = described.get("status") == "connected" and \
            Grant.READ.value in described.get("grants", [])
        return {
            "provider": PROVIDER,
            "connected": described.get("status") == "connected",
            "can_read": ok,
            "account": described.get("account", ""),
            "status": described.get("status", "disconnected"),
        }

    def _build_service(self):
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials

        token = self._accounts.token(PROVIDER, Grant.READ)
        try:
            import json
            data = json.loads(token)
        except Exception as exc:
            raise GmailUnavailable(
                "The stored Gmail credential is unreadable; reconnect it."
            ) from exc

        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes", SCOPES),
        )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ── reading ─────────────────────────────────────────────────────────────
    def messages_since(self, since_epoch: float, limit: int = 40) -> list[Message]:
        """Metadata for mail received since a moment. Never bodies."""
        return self.search(f"after:{int(since_epoch)}", limit=limit)

    def search(self, query: str, limit: int = 20) -> list[Message]:
        """Metadata for mail matching a Gmail query ("from:ada subject:invoice
        newer_than:7d", "is:unread"). Read-only, never bodies -- the snippet
        Gmail returns is the most of a message NOVA sees."""
        try:
            service = self._service_factory()
        except GmailUnavailable:
            raise
        except Exception as exc:
            self._note_auth_failure(exc)
            raise GmailUnavailable(f"Could not reach Gmail: {exc}") from exc

        try:
            listing = service.users().messages().list(
                userId="me", q=query, maxResults=int(limit)).execute()
        except Exception as exc:
            self._note_auth_failure(exc)
            raise GmailUnavailable(f"Could not list messages: {exc}") from exc

        out: list[Message] = []
        for stub in (listing.get("messages") or []):
            try:
                raw = service.users().messages().get(
                    userId="me", id=stub["id"], format="metadata",
                    metadataHeaders=WANTED_HEADERS).execute()
            except Exception:
                log.debug("[GMAIL] could not fetch %s", stub.get("id"),
                          exc_info=True)
                continue
            out.append(self._to_message(raw))
        return out

    @staticmethod
    def _to_message(raw: dict) -> Message:
        payload = raw.get("payload") or {}
        headers = {}
        for header in (payload.get("headers") or []):
            name = str(header.get("name", ""))
            if name:
                headers[name] = header.get("value", "")

        def split(value: str) -> list:
            return [part.strip() for part in str(value or "").split(",")
                    if part.strip()]

        received = 0.0
        try:
            received = float(raw.get("internalDate", 0)) / 1000.0
        except Exception:
            pass

        return Message(
            message_id=raw.get("id", ""),
            sender=headers.get("From", ""),
            subject=headers.get("Subject", ""),
            to=split(headers.get("To", "")),
            cc=split(headers.get("Cc", "")),
            snippet=raw.get("snippet", "") or "",
            received_at=received,
            headers=headers,
            labels=raw.get("labelIds") or [],
        )

    def important_since(self, since_epoch: float, me: str = "",
                        known_contacts: Optional[Iterable[str]] = None,
                        topics: Optional[Iterable[str]] = None,
                        limit: int = 40) -> list[ScoredMessage]:
        """Mail worth mentioning, most notable first."""
        scored = [
            ScoredMessage(m, classify(m, me=me, known_contacts=known_contacts,
                                      topics=topics))
            for m in self.messages_since(since_epoch, limit=limit)
        ]
        notable = [s for s in scored
                   if s.level in (Importance.IMPORTANT,
                                  Importance.POSSIBLY_IMPORTANT)]
        notable.sort(key=lambda s: s.verdict.score, reverse=True)
        return notable

    def summarise_since(self, since_epoch: float, **kw) -> str:
        """What NOVA says out loud. Built from facts, not from message text."""
        try:
            notable = self.important_since(since_epoch, **kw)
        except GmailUnavailable as exc:
            return f"I couldn't check your mail: {exc}"

        if not notable:
            return "Nothing in your mail today looks like it needs you."

        lines = [f"{len(notable)} message(s) worth a look:"]
        for scored in notable[:5]:
            lines.append("  " + scored.verdict.describe(scored.message))
        return "\n".join(lines)

    def _note_auth_failure(self, exc: Exception) -> None:
        """An expired token should say so, not look like an outage."""
        text = str(exc).lower()
        if "invalid_grant" in text or "unauthorized" in text or "401" in text:
            self._accounts.mark_needs_reauth(PROVIDER)
            log.warning("[GMAIL] credential rejected; marked for reauth")


# ── consent ─────────────────────────────────────────────────────────────────

#: How long the consent page may stay open before the attempt is abandoned.
CONSENT_TIMEOUT_S = 300


def _consent_via_local_server(flow, port: int = 0,
                              timeout_s: float = CONSENT_TIMEOUT_S,
                              open_browser: bool = True):
    """Open Google's consent page and wait for its redirect to localhost.

    Replaces InstalledAppFlow.run_local_server, which serves exactly one
    connection: a browser's speculative preconnect to the redirect port
    (opened, nothing sent, closed) used that up and the flow failed with
    "Timed out waiting for response from authorization server" seconds
    after the page opened. This keeps serving until a request actually
    carries Google's answer -- ``code`` or ``error`` -- or the deadline passes.
    """
    import webbrowser
    import wsgiref.simple_server
    import wsgiref.util
    from urllib.parse import parse_qs, urlparse

    answer: dict[str, str] = {}

    def app(environ, start_response):
        uri = wsgiref.util.request_uri(environ)
        query = parse_qs(urlparse(uri).query)
        if "code" in query or "error" in query:
            answer["uri"] = uri
            answer["error"] = (query.get("error") or [""])[0]
            body = (b"NOVA received Google's answer. You can close this tab."
                    if not answer["error"] else
                    b"Google did not grant access. You can close this tab.")
        else:
            body = b""
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
        return [body]

    class _Quiet(wsgiref.simple_server.WSGIRequestHandler):
        def log_message(self, *args):   # stderr is None in the windowed EXE
            pass

    server = wsgiref.simple_server.make_server("localhost", port, app,
                                               handler_class=_Quiet)
    try:
        flow.redirect_uri = f"http://localhost:{server.server_port}/"
        auth_url, _ = flow.authorization_url(prompt="consent")
        log.info("[GMAIL] waiting for consent on port %s", server.server_port)
        if open_browser:
            webbrowser.open(auth_url, new=1, autoraise=True)
        server.timeout = 0.5
        deadline = time.time() + timeout_s
        while "uri" not in answer and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "uri" not in answer:
        raise GmailUnavailable(
            f"Google sign-in was not finished within {int(timeout_s)} seconds.")
    if answer["error"] == "access_denied":
        raise GmailUnavailable(
            "Google refused access (access_denied). If the page said the app "
            "has not completed verification, add this Google account as a "
            "test user on the OAuth consent screen in Google Cloud Console.")
    if answer["error"]:
        raise GmailUnavailable(f"Google refused access ({answer['error']}).")
    # oauthlib insists on https even for the loopback redirect.
    flow.fetch_token(authorization_response=answer["uri"].replace("http", "https", 1))
    return flow.credentials


def authorise(accounts: Optional[AccountStore] = None,
              port: int = 0) -> dict[str, Any]:
    """Run Google's consent flow and store the result. Opens a browser.

    The client id and secret come from the credential layer, never from
    source. Only a read scope is requested.
    """
    import json

    import nova_secure_store as secure
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_id = secure.get_secret("nova.oauth.google.client_id")
    client_secret = secure.get_secret("nova.oauth.google.client_secret")
    if not client_id or not client_secret:
        raise GmailUnavailable(
            "No Google OAuth client is configured. Create one in Google Cloud "
            "Console and store it with nova_secure_store."
        )

    config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    creds = _consent_via_local_server(flow, port=port)

    store = accounts or get_account_store()
    payload = json.dumps({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    })

    email = ""
    try:
        from googleapiclient.discovery import build
        profile = build("gmail", "v1", credentials=creds,
                        cache_discovery=False).users().getProfile(
                            userId="me").execute()
        email = profile.get("emailAddress", "")
    except Exception:
        log.debug("[GMAIL] could not read the profile address", exc_info=True)

    # READ only. A write grant would need its own scope and its own consent.
    store.connect(PROVIDER, account=email or "connected", token=payload,
                  grants=[Grant.READ])
    return {"account": email, "scopes": SCOPES}
