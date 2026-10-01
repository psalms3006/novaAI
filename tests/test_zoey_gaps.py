"""Capabilities the Zoey self-audit has and NOVA lacked: conversation recall,
reading one page or video, searching mail. Offline: fakes stand in for the
network and Gmail; the database is a real SQLite file in tmp."""
import json
import time
from datetime import datetime, timedelta

import pytest

import nova  # noqa: F401
from actions import email_search, fetch_url, recall_conversations as rc


# ── recall ───────────────────────────────────────────────────────────────────

@pytest.fixture
def history(tmp_path, monkeypatch):
    from desk import store
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "nova_desktop.db")
    import nova_memory
    monkeypatch.setattr(nova_memory, "_default_memory_dir", lambda: tmp_path)
    typed = store.new_conversation("Trip planning")
    store.add_message(typed, "user", "Which VPN is free for testing Zoey OS from Nigeria?")
    store.add_message(typed, "assistant", "Proton VPN has a free plan with servers in ten countries.")
    voice = store.new_conversation("Voice — open settings")
    store.add_message(voice, "user", "Open settings and go to accounts")
    store.add_message(voice, "assistant", "Settings is open at Your info.")
    old = (datetime.now() - timedelta(days=9)).timestamp()
    (tmp_path / "session_archive.jsonl").write_text(json.dumps({"started": "x", "turns": [
        {"timestamp": old, "role": "user", "content": "remind me about the Proton invoice"},
        {"timestamp": old + 1, "role": "assistant", "content": "I'll remind you about the invoice."},
    ]}) + "\n", encoding="utf-8")
    return tmp_path


def test_recall_finds_the_exchange_with_both_sides(history):
    out = rc.execute({"query": "VPN Nigeria"})
    assert "You: Which VPN is free" in out and "NOVA: Proton VPN has a free plan" in out
    assert "typed" in out


def test_recall_marks_voice_and_filters_by_date(history):
    assert "voice" in rc.execute({"query": "settings accounts", "when": "today"})
    assert "no conversation" in rc.execute({"query": "settings accounts", "when": "yesterday"})


def test_recall_reaches_older_voice_sessions_in_the_archive(history):
    out = rc.execute({"query": "Proton invoice", "when": "last 10 days"})
    assert "remind me about the Proton invoice" in out


def test_recall_when_phrases():
    now = datetime(2026, 10, 1, 15, 0)
    s, e = rc.parse_when("yesterday", now)
    assert datetime.fromtimestamp(s) == datetime(2026, 9, 30) and datetime.fromtimestamp(e) == datetime(2026, 10, 1)
    s, _ = rc.parse_when("last week", now)
    assert datetime.fromtimestamp(s) == datetime(2026, 9, 24)
    s, _ = rc.parse_when("2026-09-30", now)
    assert datetime.fromtimestamp(s) == datetime(2026, 9, 30)


def test_where_you_left_off_names_recent_conversations_not_the_current_one(history):
    from desk import continuity, store
    current = store.new_conversation("New chat")
    block = continuity.last_time(exclude_cid=current)
    assert block.startswith("## Where you left off")
    assert "Open settings and go to accounts" in block and "(voice" in block
    assert "Which VPN is free" in block and "(typed" in block
    assert "not a request" in block


# ── fetch_url ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["http://127.0.0.1:8765/api/status", "http://localhost/",
                                 "http://192.168.1.1/admin", "http://169.254.169.254/latest/meta-data",
                                 "file:///C:/Windows/win.ini", "ftp://example.com/x"])
def test_fetch_refuses_local_private_and_non_web_addresses(url):
    assert fetch_url.execute({"url": url}).startswith("I won't fetch that")


def test_fetch_refuses_a_redirect_into_the_machine(monkeypatch):
    import requests

    class R:
        def __init__(self, code, loc=""):
            self.status_code, self.headers = code, ({"Location": loc} if loc else {})
    calls = []

    def get(url, **kw):
        calls.append(url)
        return R(302, "http://127.0.0.1:8765/api/settings")

    def public(h):
        if h == "127.0.0.1":
            raise fetch_url.Refused("local")
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(fetch_url, "_public_host", public)
    out = fetch_url.execute({"url": "https://example.com/go"})
    assert out.startswith("I won't fetch that") and calls == ["https://example.com/go"]


def test_youtube_links_are_read_as_transcripts():
    assert fetch_url._youtube_id("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=3") == "jNQXAC9IVRw"
    assert fetch_url._youtube_id("https://youtu.be/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert fetch_url._youtube_id("https://www.youtube.com/shorts/abcdef123") == "abcdef123"
    assert fetch_url._youtube_id("https://example.com/watch?v=x") == ""


def test_readable_keeps_prose_and_drops_menus():
    text = "\n".join(["Main menu", "Home", "About", "Contact"] +
                     ["Lagos is the most populous city in Nigeria and a major financial centre in Africa."] * 3)
    out = fetch_url._readable(text)
    assert "Main menu" not in out and out.count("Lagos is") == 3


# ── email ────────────────────────────────────────────────────────────────────

def test_email_says_how_to_connect_when_not_connected(monkeypatch):
    class Off:
        def health(self):
            return {"can_read": False}
    monkeypatch.setattr(email_search, "_connector", lambda: Off())
    assert "Gmail isn't connected" in email_search.execute({"query": "from:ada"})


def test_email_search_lists_matching_mail(monkeypatch):
    from integrations.email_importance import Message

    class On:
        def health(self):
            return {"can_read": True}

        def search(self, q, limit=10):
            assert q == "from:ada subject:invoice"
            return [Message(message_id="1", sender="Ada <ada@x.com>", subject="Invoice 42", to=[], cc=[],
                            snippet="Please find the invoice attached", received_at=time.time(),
                            headers={}, labels=[])]
    monkeypatch.setattr(email_search, "_connector", lambda: On())
    out = email_search.execute({"query": "from:ada subject:invoice"})
    assert "Ada <ada@x.com> -- Invoice 42" in out and "invoice attached" in out


def test_gmail_search_uses_the_query_and_stays_metadata_only():
    from integrations.gmail import GmailConnector

    class Call:
        def __init__(self, result):
            self.result = result

        def execute(self):
            return self.result

    class Svc:
        seen = {}

        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **kw):
            Svc.seen["list"] = kw
            return Call({"messages": [{"id": "1"}]})

        def get(self, **kw):
            Svc.seen["get"] = kw
            return Call({"id": "1", "snippet": "hi", "payload": {"headers": [{"name": "From", "value": "a"}]}})
    g = GmailConnector(accounts=object(), service_factory=Svc)
    msgs = g.search("is:unread", limit=5)
    assert Svc.seen["list"]["q"] == "is:unread" and Svc.seen["get"]["format"] == "metadata"
    assert msgs and msgs[0].sender == "a"


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_new_tools_are_declared_permitted_and_classified():
    from desk import confirm
    from nova_core import permissions, trust
    names = {d["name"] for d in nova.TOOL_DECLARATIONS}
    assert {"fetch_url", "recall_conversations", "email_search"} <= names
    assert confirm.scope_for("fetch_url", {}) == "browser_read"
    assert confirm.scope_for("recall_conversations", {}) == ""
    assert {"recall_conversations", "email_search"} <= set(permissions.TOOL_CAPABILITIES)
    assert trust.taints("fetch_url") and trust.taints("email_search")
