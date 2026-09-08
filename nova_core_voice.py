"""nova_core_voice — what NOVA has to say on her own initiative.

Proactive speech belongs to NOVA Core, not to a UI surface. A surface asks
"is there anything worth saying?" and renders/speaks the answer; it never
invents the content itself. That keeps the terminal, the fullscreen window and
the ambient orb saying the same thing for the same reason.

Relevance is the point: a heartbeat firing is not itself a reason to talk.
"""
from __future__ import annotations

import logging

log = logging.getLogger("NOVA")


def opening_line(meta: dict | None = None) -> str:
    """Something genuinely worth surfacing on launch, or "" for a plain greeting.

    Checked in order of how much the user is likely to care:
      1. Missed notices held while NOVA was away (heartbeat inbox).
      2. Anything the proactive agent considers pending.

    Returns plain text for NOVA to deliver in her own words — never a
    pre-phrased sentence to read out, so she still sounds like herself.
    """
    parts: list[str] = []

    try:
        import nova_state
        hb = getattr(nova_state, "_heartbeat", None)
        if hb is not None:
            missed = hb.show_missed()
            if missed:
                parts.append(missed)
    except Exception as e:
        log.debug("heartbeat catch-up unavailable: %s", e)

    try:
        import nova
        pro = getattr(nova, "_proactive", None)
        pending = getattr(pro, "pending", None) if pro is not None else None
        if callable(pending):
            item = pending()
            if item:
                parts.append(str(item))
    except Exception as e:
        log.debug("proactive pending unavailable: %s", e)

    return "\n".join(p for p in parts if p).strip()


__all__ = ["opening_line"]
