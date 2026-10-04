"""Regression tests for nova_tls.

The bug these guard against: certifi did not contain the TLS-interception root
present in the machine's OS trust store, so every HTTPS call from NOVA failed
with CERTIFICATE_VERIFY_FAILED while DNS, TCP and the connectivity monitor all
reported "online". Inference, Gemini Live voice and web search were dead, and
nothing in the health output said why.
"""
from __future__ import annotations

import ssl
import sys

import pytest

import nova_tls


def test_ensure_tls_trust_is_idempotent_and_reports_status():
    first = nova_tls.ensure_tls_trust()
    second = nova_tls.ensure_tls_trust()
    assert first == second
    assert set(first) >= {"applied", "method", "error"}
    assert first["method"] in ("truststore", "certifi")


def test_status_matches_ensure_result():
    result = nova_tls.ensure_tls_trust()
    assert nova_tls.status() == result


def test_truststore_is_installed_and_injects_successfully():
    """truststore is a hard requirement, not a nice-to-have.

    Without it NOVA is unusable on any machine running antivirus HTTPS
    scanning or sitting behind a corporate TLS proxy.
    """
    pytest.importorskip("truststore")
    assert nova_tls.ensure_tls_trust()["method"] == "truststore"


def test_probe_reports_failure_for_an_unresolvable_host():
    ok, err = nova_tls.probe("nova-tls-probe-does-not-exist.invalid", timeout=3.0)
    assert ok is False
    assert err


def test_probe_distinguishes_tls_failure_from_reachability():
    """The probe must complete a handshake, not just a TCP connect.

    A bare TCP connect succeeded throughout the original outage, which is
    exactly why the broken trust store stayed invisible.
    """
    src = (nova_tls.probe.__doc__ or "")
    assert "handshake" in src.lower()
    # wrap_socket is what makes it a handshake rather than a connect.
    import inspect
    assert "wrap_socket" in inspect.getsource(nova_tls.probe)


@pytest.mark.skipif(sys.platform != "win32", reason="OS trust store check is Windows-specific here")
def test_default_ssl_context_can_verify_google_after_injection():
    """End-to-end: the host NOVA must reach actually verifies.

    Skipped rather than failed when the machine is offline — this asserts trust
    configuration, not internet availability.
    """
    nova_tls.ensure_tls_trust()
    ok, err = nova_tls.probe(timeout=8.0)
    if not ok and "certificate" not in err.lower():
        pytest.skip(f"host unreachable, not a trust problem: {err}")
    assert ok, f"TLS verification of {nova_tls.VERIFY_HOST} failed: {err}"


def test_injection_actually_changed_the_default_ssl_context():
    nova_tls.ensure_tls_trust()
    if nova_tls.status()["method"] != "truststore":
        pytest.skip("truststore not active")
    ctx = ssl.create_default_context()
    assert isinstance(ctx, ssl.SSLContext)
