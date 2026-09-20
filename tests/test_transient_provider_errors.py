"""A provider under load is not an error in NOVA.

From a real session, four of these in three minutes:

    ERROR: Memory extraction failed: 503 UNAVAILABLE. {'error': {'code': 503,
    'message': 'This model is currently experiencing high demand...'}}

429 was already understood as transient and answered with a backoff. 503 --
the same thing said differently, and just as retryable -- fell through to
log.error. Four ERROR lines for a hiccup that needed no attention, in the
same log someone reads to find out why NOVA went quiet.

Logging a transient condition at ERROR is not harmless: it buries the real
failure and teaches whoever reads the log to skim past the word.
"""
from __future__ import annotations

import logging

import pytest

# memory_extra imports nova, and nova imports memory_extra — the cycle
# only resolves if nova is the one that gets imported first.
import nova  # noqa: F401  (import order matters)
import memory_extra


class _Boom:
    def __init__(self, message):
        self.message = message

    def __call__(self, *a, **kw):
        raise RuntimeError(self.message)


@pytest.fixture(autouse=True)
def _clear_backoff():
    memory_extra._reset_rate_limit()
    yield
    memory_extra._reset_rate_limit()


def _provoke(monkeypatch, message):
    """Drive extraction into its failure path with a given provider error."""
    monkeypatch.setattr(memory_extra, "_extract_via_model",
                        _Boom(message), raising=False)
    return message


def test_an_overloaded_provider_backs_off_like_a_rate_limit():
    """503 and 429 are the same situation: come back later."""
    assert memory_extra._is_transient_provider_error(
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': "
        "'This model is currently experiencing high demand.'}}") is True
    assert memory_extra._is_transient_provider_error(
        "429 RESOURCE_EXHAUSTED") is True


def test_a_real_failure_is_still_a_real_failure():
    assert memory_extra._is_transient_provider_error(
        "KeyError: 'candidates'") is False
    assert memory_extra._is_transient_provider_error(
        "400 INVALID_ARGUMENT") is False


def test_an_overloaded_provider_is_not_logged_as_an_error(caplog):
    caplog.set_level(logging.DEBUG)
    memory_extra._note_extraction_failure(
        "503 UNAVAILABLE. This model is currently experiencing high demand.")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, (
        f"a transient provider hiccup was logged at ERROR: "
        f"{[r.getMessage() for r in errors]}"
    )


def test_a_genuine_failure_is_still_logged_loudly(caplog):
    caplog.set_level(logging.DEBUG)
    memory_extra._note_extraction_failure("KeyError: 'candidates'")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a real extraction bug was quietly swallowed"
