"""Regression tests for the offline (ZIM/Kiwix) knowledge path.

Findings this pins down:

  * libzim IS installable on Windows. The earlier conclusion that it was not
    came from pip itself failing TLS verification on this machine — the same
    interception that broke NOVA's own HTTPS. Do not re-derive that from a pip
    error without checking pip's TLS first.
  * With libzim present, lookups succeeded but ``Item.content`` is a memoryview
    of raw HTML. It was passed straight through, so an "offline article" came
    out as "<memory at 0x...>" or as a wall of TemplateStyles CSS.
  * web_search's cascade was network-only (OpenRouter -> Gemini -> DuckDuckGo),
    so losing connectivity, or merely exhausting the Gemini quota, left it with
    nothing — with a full offline Wikipedia sitting on disk.
"""
from __future__ import annotations

import pytest

import offline_extra
from offline_extra import _zim_text


# ── HTML -> prose extraction ──────────────────────────────────────────────────

def test_memoryview_content_is_decoded():
    raw = memoryview(b"<html><body><p>Hello world.</p></body></html>")
    assert _zim_text(raw) == "Hello world."


def test_bytes_content_is_decoded():
    assert _zim_text(b"<p>Plain bytes.</p>") == "Plain bytes."


def test_undecodable_input_returns_empty_string():
    assert _zim_text(object()) == ""


def test_style_blocks_are_dropped():
    raw = (b"<style>.mw-parser-output .ambox{border:1px solid #a2a9b1}</style>"
           b"<p>Tokyo is the capital of Japan.</p>")
    out = _zim_text(raw)
    assert "Tokyo is the capital of Japan." in out
    assert "mw-parser-output" not in out
    assert "border" not in out


def test_script_blocks_are_dropped():
    raw = b"<script>var x = 1;</script><p>Real prose.</p>"
    out = _zim_text(raw)
    assert out == "Real prose."


def test_paragraphs_are_preferred_over_whole_document_text():
    raw = (b"<div>NAVIGATION CHROME</div><p>First para.</p><p>Second para.</p>")
    out = _zim_text(raw)
    assert out == "First para. Second para."
    assert "NAVIGATION CHROME" not in out


def test_documents_without_paragraphs_fall_back_to_a_full_strip():
    assert _zim_text(b"<div>Only a div here.</div>") == "Only a div here."


def test_html_entities_are_unescaped():
    out = _zim_text(b"<p>Tokyo&nbsp;&amp; Kyoto &lt;test&gt;</p>")
    assert "&nbsp;" not in out and "&amp;" not in out and "&lt;" not in out
    assert "Kyoto" in out and "<test>" in out


def test_limit_is_respected():
    raw = b"<p>" + b"x" * 5000 + b"</p>"
    assert len(_zim_text(raw, limit=100)) == 100


def test_whitespace_is_collapsed():
    assert _zim_text(b"<p>a\n\n\t  b</p>") == "a b"


# ── search cascade falls through to offline knowledge ─────────────────────────

def test_web_search_falls_back_to_offline_knowledge(monkeypatch):
    """With every network backend down, a local ZIM hit must still answer."""
    import actions.web_search as ws

    def _boom(*a, **kw):
        raise RuntimeError("simulated: no network")

    monkeypatch.setattr(ws, "_gemini_search", _boom)
    monkeypatch.setattr(ws, "_ddg_search", _boom)

    class _FakeWiki:
        libzim_available = True

        def search(self, query, limit=3):
            return [{"source": "Wikipedia (test)", "title": "Photosynthesis",
                     "content": "Photosynthesis is how plants make food."}]

    monkeypatch.setattr(offline_extra, "get_offline_wiki", lambda: _FakeWiki())

    out = ws.web_search({"query": "Photosynthesis"})
    assert "Photosynthesis is how plants make food." in out
    assert "Offline results" in out


def test_web_search_reports_honestly_when_everything_fails(monkeypatch):
    import actions.web_search as ws

    def _boom(*a, **kw):
        raise RuntimeError("simulated: no network")

    monkeypatch.setattr(ws, "_gemini_search", _boom)
    monkeypatch.setattr(ws, "_ddg_search", _boom)

    class _NoWiki:
        libzim_available = False

        def search(self, query, limit=3):
            return []

    monkeypatch.setattr(offline_extra, "get_offline_wiki", lambda: _NoWiki())

    out = ws.web_search({"query": "anything"})
    assert "couldn't search" in out.lower()
    # Must not fabricate an answer when it has none.
    assert "anything is" not in out.lower()
