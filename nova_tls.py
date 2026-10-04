"""nova_tls — make NOVA's outbound TLS work on real user machines.

Problem this solves
-------------------
Python's `requests`, `google-genai`, `websockets` and `httpx` all verify TLS
against the **certifi** CA bundle, not the operating system trust store. On any
machine where TLS is intercepted — corporate proxies, and (very commonly on
Windows) antivirus/endpoint products that do HTTPS scanning — the interception
root lives in the OS trust store and is *absent* from certifi. Every HTTPS call
then dies with:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate

For NOVA that is fatal: Gemini REST, Gemini Live (voice), and web search all
stop working, while a plain TCP connectivity probe still reports "online" — so
the app looks healthy and answers every request with an error.

Fix
---
Install `truststore`, which makes Python's `ssl` module verify against the
native OS trust store (Windows CryptoAPI / macOS SecureTransport / OpenSSL on
Linux). That is a superset of certifi on an intercepted machine, and equivalent
elsewhere.

This module is deliberately import-safe and never raises: if `truststore` is
missing or injection fails, NOVA continues with certifi and `probe()` reports
the degraded state so the UI can say so honestly rather than failing silently.
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
import threading

log = logging.getLogger("NOVA")

_lock = threading.Lock()
_applied = False
_status: dict = {"applied": False, "method": "certifi", "error": ""}

# Hosts NOVA must be able to reach over TLS for its online brain to work.
VERIFY_HOST = "generativelanguage.googleapis.com"


def ensure_tls_trust() -> dict:
    """Route Python TLS verification through the OS trust store. Idempotent.

    Safe to call from any entry point, as early as possible. Returns the status
    dict (also available later via :func:`status`).
    """
    global _applied
    with _lock:
        if _applied:
            return dict(_status)
        _applied = True

        # An explicit operator override always wins — if someone has pointed
        # SSL_CERT_FILE at a bundle on purpose, do not second-guess them.
        if os.environ.get("NOVA_TLS_USE_CERTIFI") == "1":
            _status.update(applied=False, method="certifi", error="disabled by NOVA_TLS_USE_CERTIFI=1")
            return dict(_status)

        try:
            import truststore
            truststore.inject_into_ssl()
            _status.update(applied=True, method="truststore", error="")
            log.info("[TLS] verification routed through the OS trust store (truststore)")
        except Exception as e:
            _status.update(applied=False, method="certifi", error=str(e))
            log.warning(
                "[TLS] truststore unavailable (%s) — falling back to certifi. "
                "HTTPS may fail on machines with TLS interception (corporate "
                "proxy or antivirus HTTPS scanning).", e,
            )
        return dict(_status)


def status() -> dict:
    """Current TLS trust posture, for the /api/status diagnostics payload."""
    return dict(_status)


def probe(host: str = VERIFY_HOST, timeout: float = 6.0) -> tuple[bool, str]:
    """Do a real TLS handshake against *host*.

    This is the check that distinguishes "the network is up" from "NOVA can
    actually talk to its model provider" — a plain TCP connect succeeds even
    when certificate verification is broken, which is exactly how this class of
    failure stayed invisible.
    """
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                return True, ""
    except ssl.SSLCertVerificationError as e:
        return False, f"certificate verification failed: {e}"
    except Exception as e:
        return False, str(e)


__all__ = ["ensure_tls_trust", "status", "probe", "VERIFY_HOST"]
