"""Connect NOVA to Gmail, read-only.

Run it:

    python tools/connect_gmail.py

A browser opens, Google asks whether NOVA may *read* your mail, and the
refresh token is stored in the credential layer -- Windows Credential Manager
where available. Nothing is written to the repository.

Only `gmail.readonly` is requested. NOVA cannot send, delete or archive
anything with the token this produces; that would need a different scope and
a separate consent.

If Google refuses with `redirect_uri_mismatch`, the OAuth client in Google
Cloud Console is a **Web application** rather than a **Desktop app**. Either
create a Desktop app client, or add the printed redirect URI to the existing
client's authorised list.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    import nova_secure_store as secure

    client_id = secure.get_secret("nova.oauth.google.client_id")
    if not client_id:
        print("No Google OAuth client is stored.")
        print("Store one first with nova_secure_store.set_secret under")
        print("  nova.oauth.google.client_id / nova.oauth.google.client_secret")
        return 2

    print(f"Using OAuth client ending {client_id[-28:]}")
    print("Requesting read-only access to Gmail.")
    print("A browser window will open for consent.\n")

    from integrations.gmail import GmailUnavailable, authorise

    try:
        result = authorise()
    except GmailUnavailable as exc:
        print("Could not connect:", exc)
        return 1
    except Exception as exc:
        text = str(exc)
        print("Consent failed:", text[:400])
        if "redirect_uri_mismatch" in text:
            print("\nThat error means the OAuth client is a 'Web application'.")
            print("Either create a 'Desktop app' client in Google Cloud")
            print("Console, or add http://localhost to the existing client's")
            print("authorised redirect URIs.")
        elif "access_denied" in text:
            print("\nConsent was declined, or this account is not listed as a")
            print("test user on an app still in Testing mode.")
        return 1

    print("\nConnected:", result.get("account") or "(address unavailable)")
    print("Scopes   :", ", ".join(result.get("scopes", [])))
    print("\nThe token is in the credential layer, not in the repository.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
