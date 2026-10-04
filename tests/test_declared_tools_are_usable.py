"""Every tool NOVA is told about must be one she can actually use.

`wake_detector` was declared to the model as the way to "start listening" and
"stop listening". It declared no capabilities, so `nova_core.permissions`
denied it at every trust level -- correctly, that is the fail-closed default --
and it also looked for `nova_wake.py` on disk, which does not exist in a frozen
build. Three independent failures, and the model was actively encouraged to
reach for it, so asking NOVA to stop listening always failed.

Offering a capability that cannot succeed is worse than not offering it: the
user asks, NOVA refuses, and nothing in the refusal explains that the request
was impossible from the start.
"""
from __future__ import annotations

import nova
from nova_core.permissions import Effect, Trust, check_tool


def test_no_declared_tool_is_refused_at_every_trust_level():
    """A tool that can never be authorised should not be advertised."""
    impossible = []
    for decl in nova.TOOL_DECLARATIONS:
        name = decl.get("name")
        if not name:
            continue
        verdicts = {
            check_tool("nova", name, trust=trust).effect
            for trust in (Trust.USER, Trust.UNTRUSTED)
        }
        if verdicts == {Effect.DENY}:
            impossible.append(name)

    assert impossible == [], (
        f"declared to the model but refused at every trust level: "
        f"{impossible}. The model will call these and always fail."
    )
