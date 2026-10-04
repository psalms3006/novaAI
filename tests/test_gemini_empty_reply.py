"""An empty reply is a failure, and thinking must not eat the whole answer.

Found live in the packaged app (2026-09-30): a design request came back with
0 characters. The chat path never set an output budget, so every call used
the router's default of 1024 tokens -- and current Gemini models spend part
of that budget thinking before they write. The same request with no cap
produced 581 thinking + 2598 answer tokens.
"""
import types

from nova_intelligence import gemini_provider as gp


class _Chunk:
    def __init__(self, text):
        self.text = text
        self.candidates = []


class _Models:
    def __init__(self, script):
        self.script, self.calls = script, []

    def generate_content_stream(self, model, contents, config):
        self.calls.append((model, config.max_output_tokens))
        for piece in self.script.get(model, []):
            yield _Chunk(piece)


def _provider(script):
    p = gp.GeminiProvider(api_key="test-key-not-real")
    models = _Models(script)
    p._client = types.SimpleNamespace(models=models)
    return p, models


def test_an_empty_stream_moves_on_to_the_next_model():
    p, models = _provider({"gemini-flash-latest": [], "gemini-2.5-flash": ["Here is ", "the design."]})
    out = list(p.stream_complete([{"role": "user", "content": "Design a landing page"}]))
    done = out[-1][1]
    assert done.text == "Here is the design." and done.model == "gemini-2.5-flash"
    assert [m for m, _ in models.calls] == ["gemini-flash-latest", "gemini-2.5-flash"]


def test_nothing_from_any_model_is_reported_as_an_error_not_an_answer():
    p, _ = _provider({})
    done = list(p.stream_complete([{"role": "user", "content": "hi"}]))[-1][1]
    assert done.error and "empty response" in done.error


def test_the_output_budget_leaves_room_to_think():
    p, models = _provider({"gemini-flash-latest": ["ok"]})
    list(p.stream_complete([{"role": "user", "content": "hi"}], max_tokens=1024))
    assert models.calls[0][1] >= gp.MIN_OUTPUT_TOKENS
    assert gp._output_budget(20000) == 20000
