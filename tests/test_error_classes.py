"""Failure classes: a timeout and a revoked key call for opposite responses."""
import pytest

from nova_core.errors import ErrorClass, advice, classify, retryable


@pytest.mark.parametrize("err, cls", [
    (TimeoutError("read timed out"), ErrorClass.TRANSIENT),
    (ConnectionError("connection reset by peer"), ErrorClass.TRANSIENT),
    ("503 Service Unavailable", ErrorClass.TRANSIENT),
    ("429 RESOURCE_EXHAUSTED: quota", ErrorClass.TRANSIENT),
    ("Error: rate limit exceeded, try again later", ErrorClass.TRANSIENT),
    (PermissionError("auth_required: ImgAPI needs to be reconnected"), ErrorClass.AUTH),
    ("401 Unauthorized", ErrorClass.AUTH),
    ("API key not valid. Please pass a valid API key.", ErrorClass.AUTH),
    ("Refused: tool 'x' declares no capabilities", ErrorClass.PERMISSION),
    ("The user declined.", ErrorClass.PERMISSION),
    (PermissionError("Access is denied"), ErrorClass.PERMISSION),
    ("Tool 'open_app' is unavailable (module missing).", ErrorClass.UNAVAILABLE),
    (ModuleNotFoundError("No module named 'pyautogui'"), ErrorClass.UNAVAILABLE),
    ("getaddrinfo failed", ErrorClass.UNAVAILABLE),
    (ValueError("bad value"), ErrorClass.INVALID_INPUT),
    ("missing required argument 'path'", ErrorClass.INVALID_INPUT),
    ("Traceback (most recent call last): ZeroDivisionError", ErrorClass.INTERNAL),
    (None, ErrorClass.INTERNAL),
])
def test_classify(err, cls):
    assert classify(err) is cls


@pytest.mark.parametrize("status, cls", [(401, ErrorClass.AUTH), (403, ErrorClass.AUTH),
                                         (429, ErrorClass.TRANSIENT), (502, ErrorClass.TRANSIENT),
                                         (400, ErrorClass.INVALID_INPUT), (422, ErrorClass.INVALID_INPUT)])
def test_status_codes(status, cls):
    assert classify(status=status) is cls


def test_only_transient_failures_are_retried():
    assert [c for c in ErrorClass if retryable(c)] == [ErrorClass.TRANSIENT]
    assert all(advice(c) for c in ErrorClass)


def test_a_timeout_does_not_break_a_learned_skill(tmp_path, monkeypatch):
    from nova_skills import runner
    from nova_skills.registry import CapabilityRegistry
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path))
    reg = CapabilityRegistry()
    cap = reg.add("Echo", "d", workflow=[{"tool": "echo", "args": {"t": "{x}"}}],
                  test={"inputs": {"x": "hi"}, "expect": {"contains": "hi"}})
    ok_exec = lambda name, args, meta: f"said {args['t']}"                     # noqa: E731
    assert runner.verify(reg, cap.id, ok_exec)[0]

    slow = lambda name, args, meta: "Error: request timed out"                # noqa: E731
    passed, detail = runner.verify(reg, cap.id, slow)
    c = reg.get(cap.id)
    assert not passed and "inconclusive" in detail
    assert c.learned and c.health == "degraded"
    assert reg.timeline()[0]["event"] == "test_inconclusive"

    broken = lambda name, args, meta: "Error: output format changed"          # noqa: E731
    runner.verify(reg, cap.id, broken)
    assert reg.get(cap.id).health == "broken"


def test_run_reports_the_class_and_what_to_do(tmp_path, monkeypatch):
    from nova_skills.registry import CapabilityRegistry
    from nova_skills.service import CapabilityService
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path))
    calls = {"n": 0}

    def exe(name, args, meta):
        calls["n"] += 1
        return "said hi" if calls["n"] == 1 else "Error: 503 service unavailable"
    svc = CapabilityService(exe, registry=CapabilityRegistry())
    cid = svc.adopt_composed("Echo", "d", [{"tool": "echo", "args": {}}],
                             {"inputs": {}, "expect": {"contains": "hi"}})["id"]
    out = svc.use(cid, {})
    assert out["error_class"] == "transient" and "temporary" in out["advice"]
