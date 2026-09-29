"""The benchmark's harness part runs in the suite: every scenario must pass."""
import pytest

from tests.eval import agentic_benchmark as bench


@pytest.mark.parametrize("scenario", bench.HARNESS, ids=lambda f: f.__name__[2:])
def test_harness_scenario(scenario):
    ok, detail = scenario()
    assert ok, detail
