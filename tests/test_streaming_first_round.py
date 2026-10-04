"""The first round has to stream, and still call tools.

It was the only round that never streamed. The reasoning was that tool calls
arrive in a complete response rather than as stream chunks, so a round that
might call a tool had to wait for all of it. Measured against a real key, the
first word of a twenty-word reply appeared after 9-10 seconds — the entire
round trip — when the model had started producing it in about one. The wait
was NOVA's, not the model's.

Function calls do arrive in the stream; they just have to be accumulated on
the way past instead of read once at the end. These tests pin both halves:
words reach the caller as they arrive, and tool calls still come out whole.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nova_intelligence.provider import GenerateResult


class FakeRouter:
    """A router whose stream_complete yields a scripted sequence."""

    def __init__(self, chunks=(), result=None, raises=None):
        self.chunks = list(chunks)
        self.result = result
        self.raises = raises
        self.calls = 0

    def stream_complete(self, **kw):
        self.calls += 1
        self.kwargs = kw
        if self.raises:
            raise self.raises
        for c in self.chunks:
            yield ("text", c)
        yield ("done", self.result if self.result is not None
               else GenerateResult(text="".join(self.chunks), provider="fake"))


def drive(router, messages=None):
    """Run _first_round to completion, collecting events and the return value."""
    import desk.chat as chat
    messages = messages or [{"role": "user", "content": "hello"}]
    gen = chat._first_round(messages, None, True)
    events = []
    with mock.patch.object(chat, "_nova") as nova_mod:
        nova_mod._nova_router = router
        try:
            while True:
                events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


class StreamingTests(unittest.TestCase):
    def setUp(self):
        import desk.chat  # noqa: F401  (import cost paid once)

    def _patch_router(self, router):
        """_first_round reaches the router through `import nova`."""
        fake_nova = mock.Mock()
        fake_nova._nova_router = router
        return mock.patch.dict(sys.modules, {"nova": fake_nova})

    def test_each_chunk_reaches_the_caller_as_it_arrives(self):
        import desk.chat as chat
        router = FakeRouter(chunks=["Hel", "lo ", "there"])
        with self._patch_router(router):
            gen = chat._first_round([{"role": "user", "content": "hi"}], None, True)
            events = []
            try:
                while True:
                    events.append(next(gen))
            except StopIteration as stop:
                text, calls = stop.value
        tokens = [e["text"] for e in events if e.get("type") == "token"]
        self.assertEqual(tokens, ["Hel", "lo ", "there"],
                         "words must be forwarded one at a time, not batched")
        self.assertEqual(text, "Hello there")
        self.assertEqual(calls, [])

    def test_tool_calls_survive_the_stream(self):
        """The whole reason this round did not stream before."""
        import desk.chat as chat
        result = GenerateResult(
            text="", provider="fake",
            tool_calls=[{"name": "open_app", "args": {"name": "Spotify"}}])
        router = FakeRouter(chunks=[], result=result)
        with self._patch_router(router):
            gen = chat._first_round([{"role": "user", "content": "open spotify"}], None, True)
            try:
                while True:
                    next(gen)
            except StopIteration as stop:
                text, calls = stop.value
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "open_app")
        self.assertEqual(calls[0]["args"]["name"], "Spotify")

    def test_text_and_a_tool_call_can_arrive_together(self):
        import desk.chat as chat
        result = GenerateResult(
            text="Opening it", provider="fake",
            tool_calls=[{"name": "open_app", "args": {}}])
        router = FakeRouter(chunks=["Opening ", "it"], result=result)
        with self._patch_router(router):
            gen = chat._first_round([{"role": "user", "content": "x"}], None, True)
            events = []
            try:
                while True:
                    events.append(next(gen))
            except StopIteration as stop:
                text, calls = stop.value
        self.assertEqual([e["text"] for e in events if e.get("type") == "token"],
                         ["Opening ", "it"])
        self.assertEqual(text, "Opening it")
        self.assertEqual(len(calls), 1)

    def test_the_round_is_asked_for_tools(self):
        """Streaming must not quietly drop the tool declarations."""
        import desk.chat as chat
        router = FakeRouter(chunks=["hi"])
        with self._patch_router(router):
            gen = chat._first_round([{"role": "user", "content": "x"}], None, True)
            try:
                while True:
                    next(gen)
            except StopIteration:
                pass
        self.assertTrue(router.kwargs.get("require_tools"))
        self.assertTrue(router.kwargs.get("tools"))

    def test_a_provider_error_becomes_a_readable_answer(self):
        import desk.chat as chat
        router = FakeRouter(chunks=[], result=GenerateResult(
            error="429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel",
            provider="gemini"))
        with self._patch_router(router):
            gen = chat._first_round([{"role": "user", "content": "x"}], None, True)
            events = []
            try:
                while True:
                    events.append(next(gen))
            except StopIteration as stop:
                text, calls = stop.value
        self.assertIn("today", text.lower())
        self.assertEqual(calls, [])
        self.assertTrue([e for e in events if e.get("type") == "token"],
                        "the user must see the explanation, not an empty turn")

    def test_an_empty_result_says_so_rather_than_going_silent(self):
        import desk.chat as chat
        router = FakeRouter(chunks=[], result=GenerateResult(text="", provider="fake"))
        with self._patch_router(router):
            gen = chat._first_round([{"role": "user", "content": "x"}], None, True)
            try:
                while True:
                    next(gen)
            except StopIteration as stop:
                text, _ = stop.value
        self.assertTrue(text.strip())


class RouterFailoverTests(unittest.TestCase):
    """A provider that cannot stream must still be usable."""

    def test_a_provider_without_stream_complete_falls_back_to_complete(self):
        from nova_intelligence.router import IntelligenceRouter

        prov = mock.Mock(spec=["complete", "name", "health", "is_available",
                               "capabilities"])
        prov.name = "legacy"
        prov.complete.return_value = GenerateResult(text="whole answer",
                                                    provider="legacy")
        router = IntelligenceRouter.__new__(IntelligenceRouter)
        with mock.patch.object(IntelligenceRouter, "rank_providers",
                               return_value=[prov]), \
             mock.patch.object(IntelligenceRouter, "_note_provider_used",
                               create=True), \
             mock.patch("nova_intelligence.router._telemetry_call"), \
             mock.patch("nova_intelligence.router._telemetry_event"), \
             mock.patch("nova_intelligence.router.provider_is_local",
                        return_value=True):
            out = list(router.stream_complete(messages=[{"role": "user", "content": "x"}]))
        kinds = [k for k, _ in out]
        self.assertEqual(kinds[-1], "done")
        self.assertIn("text", kinds, "the whole answer should still be delivered")
        self.assertEqual(out[-1][1].text, "whole answer")
        prov.complete.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
