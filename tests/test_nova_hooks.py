"""Hooks around every tool call: they can refuse or narrow a call, and can
never grant what NOVA's permission layer refuses."""
import pytest

from nova_core import hooks


@pytest.fixture(autouse=True)
def _clean_hooks():
    import nova  # noqa: F401  -- its import-time hooks belong to the saved set
    saved = (list(hooks._pre), list(hooks._post))
    hooks.clear()
    yield
    hooks.clear()
    hooks._pre.extend(saved[0])
    hooks._post.extend(saved[1])


def test_pre_hook_can_deny_with_a_reason():
    hooks.add_pre(lambda t, a, m: {"deny": "no deleting on Sundays"}, "file_*")
    assert hooks.run_pre("file_controller", {}, {}) == (False, {}, "no deleting on Sundays")
    assert hooks.run_pre("web_search", {"q": 1}, {})[0] is True     # matcher scopes it


def test_pre_hook_can_narrow_arguments():
    hooks.add_pre(lambda t, a, m: {"args": {**a, "max_results": 3}})
    ok, args, _ = hooks.run_pre("web_search", {"query": "x"}, {})
    assert ok and args == {"query": "x", "max_results": 3}


def test_there_is_no_allow_decision_to_give():
    hooks.add_pre(lambda t, a, m: {"allow": True, "decision": "allow"})
    assert hooks.run_pre("anything", {"a": 1}, {}) == (True, {"a": 1}, "")


def test_broken_hook_is_skipped_but_a_critical_one_fails_closed():
    def boom(*a):
        raise RuntimeError("bug")
    hooks.add_pre(boom)
    assert hooks.run_pre("web_search", {}, {})[0] is True
    hooks.add_pre(boom, critical=True, name="guard")
    ok, _, why = hooks.run_pre("web_search", {}, {})
    assert not ok and "guard" in why


def test_post_hook_can_rewrite_the_result():
    hooks.add_post(lambda t, a, r, m: r.replace("secret", "[redacted]"))
    assert hooks.run_post("x", {}, "the secret is out", {}) == "the [redacted] is out"


def test_dispatcher_runs_hooks_once_around_the_call():
    import nova
    seen = []
    hooks.add_pre(lambda t, a, m: seen.append(("pre", t)) or None)
    hooks.add_post(lambda t, a, r, m: seen.append(("post", t)) or None)
    out = nova._execute_tool_sync("nova_capability", {"cmd": "list"}, {})
    assert "No learned skills yet" in out
    assert seen == [("pre", "nova_capability"), ("post", "nova_capability")]


def test_dispatcher_refuses_what_a_pre_hook_denies():
    import nova
    ran = []
    hooks.add_pre(lambda t, a, m: {"deny": "blocked by test"}, "nova_capability")
    hooks.add_post(lambda t, a, r, m: ran.append(1))
    out = nova._execute_tool_sync("nova_capability", {"cmd": "list"}, {})
    assert out == "Refused: blocked by test." and ran == []


def test_a_hook_cannot_get_a_call_past_the_permission_layer(monkeypatch):
    """Whatever a hook passes on is still checked: the hook runs first, the policy after."""
    import nova
    from nova_core import permissions as perm
    hooks.add_pre(lambda t, a, m: {"args": {**a, "cmd": "list"}})
    monkeypatch.setattr(perm, "check_tool", lambda principal, tool, **kw: perm.Decision(
        perm.Effect.DENY, None, principal, "test policy denies all"))
    out = nova._execute_tool_sync("nova_capability", {"cmd": "list"}, {})
    assert out.startswith("Refused: test policy denies all")
