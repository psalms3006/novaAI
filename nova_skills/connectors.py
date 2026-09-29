"""nova_skills.connectors — reaching providers, and knowing whether they work.

Every provider kind answers the same two questions: can I reach it right now
(`check`), and how do I use it (`call_http` / NOVA's tool dispatcher). Health
comes from real checks, never from the fact that a connection was once made.

Credentials: stored in the OS credential store (nova_secure_store) under a key
scoped to the signed-in account -- `cap:<account>:<provider>` -- so one
person's key is never reachable from another person's NOVA on the same PC,
never written to capabilities.json, and never placed in a model prompt.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from .registry import Health, Provider, ProviderKind

#: Replaceable in tests: fn(method, url, headers, json_body, timeout) -> (status, body_text)
http_call: Callable = None
#: Replaceable in tests/runtime: fn(tool_name) -> bool
tool_available: Callable = None


def _account() -> str:
    return os.environ.get("NOVA_ACCOUNT_ID", "") or "local"


def credential_key(provider_id: str) -> str:
    return f"cap:{_account()}:{provider_id}"


def store_credential(provider_id: str, secret: str) -> str:
    import nova_secure_store as st
    key = credential_key(provider_id)
    st.set_secret(key, secret)
    return key


def load_credential(ref: str) -> str:
    if not ref:
        return ""
    # A ref belonging to another account is refused even if it exists on this PC.
    if not ref.startswith(f"cap:{_account()}:"):
        return ""
    import nova_secure_store as st
    return st.get_secret(ref) or ""


def forget_credential(ref: str) -> None:
    import nova_secure_store as st
    if ref.startswith(f"cap:{_account()}:"):
        st.delete_secret(ref)


def _default_http(method, url, headers, body, timeout):
    import requests
    r = requests.request(method, url, headers=headers, json=body, timeout=timeout)
    return r.status_code, r.text[:20000]


def _default_tool_available(name: str) -> bool:
    try:
        import nova
        return bool(nova._tool_available(name))
    except Exception:
        return False


def _mcp_tool_names() -> list:
    try:
        import nova_state
        bridge = getattr(nova_state, "_mcp_bridge", None)
        return [d.get("name", "") for d in (bridge.gemini_declarations() if bridge else [])]
    except Exception:
        return []


def call_http(provider: Provider, method: str, path: str, body: Any = None,
              timeout: float = 30.0) -> tuple:
    cfg = provider.config or {}
    base = str(cfg.get("base_url", "")).rstrip("/")
    if not base.startswith("https://") and not os.getenv("NOVA_ALLOW_HTTP_PROVIDERS"):
        raise ValueError("providers are only reached over https")
    headers = {"Content-Type": "application/json"}
    if provider.credential_ref:
        secret = load_credential(provider.credential_ref)
        if not secret:
            raise PermissionError("auth_required")
        header = cfg.get("auth_header", "Authorization")
        scheme = cfg.get("auth_scheme", "Bearer")
        headers[header] = f"{scheme} {secret}".strip() if scheme else secret
    fn = http_call or _default_http
    return fn(method.upper(), base + "/" + path.lstrip("/"), headers, body, timeout)


def check(provider: Provider) -> tuple:
    """(Health, detail) from a real, harmless check of this provider."""
    kind = provider.kind
    try:
        if kind == ProviderKind.BUILTIN_TOOL.value:
            name = provider.config.get("tool", "")
            ok = (tool_available or _default_tool_available)(name)
            return (Health.AVAILABLE, f"tool {name} is available") if ok else \
                   (Health.UNAVAILABLE, f"tool {name} is not available on this computer")
        if kind == ProviderKind.HTTP_API.value:
            probe = provider.config.get("health_path", "")
            if not probe:
                return Health.UNVERIFIED, "no health check declared"
            status, _ = call_http(provider, provider.config.get("health_method", "GET"), probe, timeout=15)
            if status in (401, 403):
                return Health.AUTH_REQUIRED, f"the service refused the credential ({status})"
            if status == 429:
                return Health.DEGRADED, "rate limited"
            if 200 <= status < 300:
                return Health.AVAILABLE, f"answered {status}"
            if status >= 500:
                return Health.UNAVAILABLE, f"service error {status}"
            return Health.DEGRADED, f"unexpected status {status}"
        if kind == ProviderKind.MCP_SERVER.value:
            want = provider.config.get("tool", "")
            if want and any(want in n for n in _mcp_tool_names()):
                return Health.AVAILABLE, f"MCP tool {want} is connected"
            return Health.UNAVAILABLE, "the MCP server is not connected"
        if kind == ProviderKind.EXTENSION.value:
            from nova_extensions.registry import ExtensionRegistry, TrustState
            ext = ExtensionRegistry().get(provider.config.get("extension", ""))
            if ext is None:
                return Health.UNAVAILABLE, "extension not installed"
            if ext.state != TrustState.ENABLED:
                return Health.UNVERIFIED, f"extension is {ext.state.value}, not enabled"
            return Health.AVAILABLE, "extension enabled"
    except PermissionError:
        return Health.AUTH_REQUIRED, "needs to be reconnected"
    except Exception as e:
        return Health.UNAVAILABLE, f"{type(e).__name__}: {e}"
    return Health.UNVERIFIED, f"unknown provider kind {kind}"


def refresh(provider: Provider) -> Provider:
    h, detail = check(provider)
    provider.health, provider.last_checked, provider.note = h.value, time.time(), detail
    return provider


__all__ = ["store_credential", "load_credential", "forget_credential", "credential_key",
           "call_http", "check", "refresh"]
