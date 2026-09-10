"""A declared tool that is never answered is a silent assistant.

Gemini Live blocks on a function call until the client returns a response. The
desktop session advertised sixteen tools and handled none of them, so the
moment NOVA decided to look something up she produced no audio, no transcript
and no reply — the turn simply ended. Conversational questions still worked,
which is exactly why it looked intermittent rather than broken: "hello" was
answered and "what is the capital of Nigeria" was not.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import live_session as ls


class FakeCall:
    def __init__(self, name, args=None, call_id="call-1"):
        self.name = name
        self.args = args or {}
        self.id = call_id


class FakeToolCall:
    def __init__(self, calls):
        self.function_calls = calls


class FakeSession:
    def __init__(self, fail=False):
        self.responses = []
        self.fail = fail

    async def send_tool_response(self, function_responses):
        if self.fail:
            raise RuntimeError("socket closed")
        self.responses.append(function_responses)


def manager():
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    return m


class ToolDispatchTests(unittest.TestCase):
    def setUp(self):
        self.m = manager()
        self.calls = []

        def fake_exec(name, args):
            self.calls.append((name, args))
            return f"{name} done"

        self.m._execute_tool = staticmethod(fake_exec)
        self.m._execute_tool = fake_exec

    def test_a_tool_call_is_answered(self):
        s = FakeSession()
        asyncio.run(self.m._handle_tool_calls(
            s, FakeToolCall([FakeCall("web_search", {"query": "abuja"})])))
        self.assertEqual(len(s.responses), 1,
                         "the model was left waiting for a tool response")
        self.assertEqual(self.calls, [("web_search", {"query": "abuja"})])

    def test_every_call_in_a_batch_is_answered(self):
        """One unanswered call in a batch stalls the turn just as thoroughly."""
        s = FakeSession()
        asyncio.run(self.m._handle_tool_calls(s, FakeToolCall([
            FakeCall("web_search", {"query": "a"}, "1"),
            FakeCall("open_app", {"app_name": "notepad"}, "2"),
        ])))
        self.assertEqual(len(s.responses[0]), 2)
        self.assertEqual([c[0] for c in self.calls], ["web_search", "open_app"])

    def test_the_response_carries_the_call_id(self):
        """Without it the model cannot match the answer to its question."""
        s = FakeSession()
        asyncio.run(self.m._handle_tool_calls(
            s, FakeToolCall([FakeCall("web_search", {}, "abc123")])))
        fr = s.responses[0][0]
        self.assertEqual(fr.id, "abc123")
        self.assertEqual(fr.name, "web_search")

    def test_a_failing_tool_still_produces_a_response(self):
        """NOVA should say the tool failed, not go quiet."""
        def boom(name, args):
            raise RuntimeError("no network")
        self.m._execute_tool = boom
        s = FakeSession()
        asyncio.run(self.m._handle_tool_calls(
            s, FakeToolCall([FakeCall("web_search")])))
        self.assertEqual(len(s.responses), 1)
        self.assertIn("error", str(s.responses[0][0].response).lower())

    def test_a_hanging_tool_does_not_hang_the_conversation(self):
        """What matters is when the *model* is unblocked.

        Measured inside the loop deliberately. A wedged tool leaves its thread
        running — nothing can kill a thread mid-call — so timing
        `asyncio.run()` would measure the executor being drained at shutdown,
        which is not what the conversation is waiting on.
        """
        done = threading.Event()

        def sleepy(name, args):
            done.wait(5)
            return "eventually"

        self.m._execute_tool = sleepy
        s = FakeSession()
        original = ls.TOOL_TIMEOUT_S

        async def drive():
            t0 = time.time()
            await self.m._handle_tool_calls(
                s, FakeToolCall([FakeCall("web_search")]))
            return time.time() - t0

        try:
            ls.TOOL_TIMEOUT_S = 0.2
            elapsed = asyncio.run(drive())
        finally:
            ls.TOOL_TIMEOUT_S = original
            done.set()          # let the stranded worker finish

        self.assertLess(elapsed, 3.0, "a wedged tool blocked the session")
        self.assertEqual(len(s.responses), 1, "no response after a timeout")
        self.assertIn("longer", str(s.responses[0][0].response).lower())

    def test_a_failed_send_is_reported_not_swallowed(self):
        """The tool ran, the model never heard: NOVA will look broken."""
        s = FakeSession(fail=True)
        asyncio.run(self.m._handle_tool_calls(
            s, FakeToolCall([FakeCall("web_search")])))
        self.assertTrue(any(e.type == "error" for e in self.m._published))

    def test_the_surfaces_are_told_what_is_running(self):
        s = FakeSession()
        asyncio.run(self.m._handle_tool_calls(
            s, FakeToolCall([FakeCall("web_search")])))
        kinds = [e.type for e in self.m._published]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)


class WiringTests(unittest.TestCase):
    def test_the_receiver_dispatches_tool_calls(self):
        src = inspect.getsource(ls.LiveManager._receiver)
        self.assertIn("msg.tool_call", src,
                      "tool calls are declared to the model but never handled")
        self.assertIn("_handle_tool_calls", src)

    def test_tools_run_off_the_event_loop(self):
        """They open apps and drive browsers; blocking here stops the audio."""
        src = inspect.getsource(ls.LiveManager._run_tool)
        self.assertIn("to_thread", src)

    def test_tools_are_actually_declared_to_the_model(self):
        """If this ever empties, the handler above is dead code."""
        decls = ls._resolve("TOOL_DECLARATIONS", [])
        self.assertTrue(decls, "no tools are declared to the Live session")

    def test_execution_is_delegated_to_nova_core(self):
        """One definition of what a tool is, shared with every other surface."""
        src = inspect.getsource(ls.LiveManager._execute_tool)
        self.assertIn("_execute_tool_sync", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
