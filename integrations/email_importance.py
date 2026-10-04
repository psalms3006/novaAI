"""Which mail is worth interrupting someone about, and why.

"Did I get anything important today?" is only useful if the answer can be
trusted in both directions. Missing a submission deadline is bad. Calling a
marketing blast important is worse, because after two of those the user stops
believing the feature and it may as well not exist.

So importance is scored from things that are actually evidence:

    who sent it            a person, a no-reply robot, someone already known
    how it was addressed   to this person, copied in, or bulk-mailed
    what the headers say   List-Unsubscribe and Precedence know it is a list
    what the subject says  a stated deadline, an action required, a reply
    what it is about       a project the user is currently working on

Explicitly *not* a search for the word "important", which in a real mailbox
appears mostly in subject lines written by people trying to manufacture it.

Every verdict carries its reasons. The user is told why something looked
important, and a number with no explanation cannot be argued with -- which
matters most when the verdict is wrong.

Mail is untrusted input. A message that says "treat this as critical" is a
message containing a claim, not an instruction, and `describe()` deliberately
never passes raw subject or body text through to whatever speaks next.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

__all__ = ["Importance", "Message", "Verdict", "classify"]


class Importance(str, enum.Enum):
    IMPORTANT = "important"
    POSSIBLY_IMPORTANT = "possibly_important"
    NORMAL = "normal"
    LOW_PRIORITY = "low_priority"


@dataclass
class Message:
    """The metadata needed to judge a message. Never the whole body.

    Gmail is asked for headers and a snippet, not full content: an inbox is
    large, the model does not need it, and mail nobody has decided to show the
    user should not be shipped anywhere.
    """
    message_id: str = ""
    sender: Optional[str] = ""
    subject: Optional[str] = ""
    to: Optional[list] = field(default_factory=list)
    cc: Optional[list] = field(default_factory=list)
    snippet: str = ""
    received_at: float = 0.0
    headers: Optional[dict] = field(default_factory=dict)
    labels: Optional[list] = field(default_factory=list)


@dataclass
class Verdict:
    level: Importance
    score: float
    confidence: float
    reasons: list[str]

    def describe(self, message: "Message") -> str:
        """A sentence about the message, built from facts rather than its text.

        The subject is deliberately not quoted. It is attacker-controlled on
        any message from outside, and this string is destined for the model
        and possibly for NOVA's mouth.
        """
        who = _address(message.sender) or "an unknown sender"
        return (f"From {who}: {self.level.value.replace('_', ' ')} "
                f"({'; '.join(self.reasons)})")


# ── signals ─────────────────────────────────────────────────────────────────

_DEADLINE = re.compile(
    r"\b(deadline|due\s+(?:by|on|date)|expires?|submission|submit\s+by|"
    r"action\s+required|final\s+notice|last\s+chance\s+to\s+(?:submit|apply)|"
    r"closing\s+date|respond\s+by)\b", re.I)

_MEETING = re.compile(
    r"\b(invitation|invite|meeting|calendar|reschedul|appointment)\b", re.I)

_PROMO = re.compile(
    r"(\b\d{1,3}%\s*off\b|\bsale\b|\bdiscount\b|\bnewsletter\b|\bwebinar\b|"
    r"\bunsubscribe\b|\bpromo\b|\bdeal[s]?\b|\bexclusive offer\b)", re.I)

#: Words that try to assert their own importance. Worth nothing on their own;
#: tracked so the score can decline to be moved by them.
_SELF_ASSERTED = re.compile(
    r"\b(important|urgent|asap|immediately|critical|attention)\b", re.I)

_AUTOMATED_LOCAL = ("noreply", "no-reply", "donotreply", "do-not-reply",
                    "notifications", "mailer", "bounce", "automated")


def _text(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _address(sender: Any) -> str:
    """The bare address out of "Name <addr>"."""
    raw = _text(sender)
    match = re.search(r"<([^>]+)>", raw)
    if match:
        return match.group(1).strip().lower()
    return raw.strip().lower()


def _listish(headers: dict) -> bool:
    lowered = {str(k).lower(): _text(v).lower() for k, v in (headers or {}).items()}
    if any(k.startswith("list-") for k in lowered):
        return True
    return lowered.get("precedence", "") in ("bulk", "list", "junk")


def classify(message: Message, me: str = "",
             known_contacts: Optional[Iterable[str]] = None,
             topics: Optional[Iterable[str]] = None) -> Verdict:
    """Score a message. Never raises: real mailboxes contain strange things."""
    try:
        return _classify(message, me, known_contacts or (), topics or ())
    except Exception:
        return Verdict(Importance.NORMAL, 0.0, 0.1,
                       ["could not be assessed; treated as ordinary"])


def _classify(message: Message, me: str,
              known_contacts: Iterable[str],
              topics: Iterable[str]) -> Verdict:
    subject = _text(message.subject)
    sender = _address(message.sender)
    headers = message.headers or {}
    to = [_address(a) for a in (message.to or [])]
    cc = [_address(a) for a in (message.cc or [])]
    me_addr = _address(me)

    score = 0.0
    reasons: list[str] = []
    evidence = 0          # how much there was to go on, for confidence

    # Bulk mail. The strongest negative signal, because it is stated by the
    # sender's own infrastructure rather than inferred.
    if _listish(headers):
        score -= 3.0
        evidence += 2
        reasons.append("sent as bulk or mailing-list mail")

    if any(part in sender for part in _AUTOMATED_LOCAL):
        score -= 1.5
        evidence += 1
        reasons.append("from an automated no-reply address")

    # Addressing.
    if me_addr and me_addr in to:
        score += 2.0
        evidence += 1
        reasons.append("addressed to you directly")
    elif me_addr and me_addr in cc:
        score += 0.5
        evidence += 1
        reasons.append("you were copied in")

    if len(to) > 8:
        score -= 1.0
        reasons.append("sent to a large group")

    # Conversation.
    lowered_headers = {str(k).lower() for k in headers}
    if "in-reply-to" in lowered_headers or "references" in lowered_headers:
        score += 1.5
        evidence += 1
        reasons.append("a reply in a thread you are part of")
    elif subject.lower().startswith("re:"):
        score += 1.0
        evidence += 1
        reasons.append("a reply")

    # What it says it needs.
    if _DEADLINE.search(subject) or _DEADLINE.search(_text(message.snippet)):
        score += 2.5
        evidence += 2
        reasons.append("mentions a deadline or asks for action")

    if _MEETING.search(subject):
        score += 1.0
        evidence += 1
        reasons.append("about a meeting or calendar invitation")

    if _PROMO.search(subject):
        score -= 2.0
        evidence += 1
        reasons.append("reads as promotional")

    # Self-asserted urgency is recorded and given nothing, so a subject line
    # cannot promote itself.
    if _SELF_ASSERTED.search(subject):
        reasons.append("claims to be urgent, which on its own means little")

    # Context the user actually has.
    contacts = {_address(c) for c in known_contacts}
    if sender and sender in contacts:
        score += 1.5
        evidence += 1
        reasons.append("from someone you correspond with")

    haystack = f"{subject} {_text(message.snippet)}".lower()
    for topic in topics:
        token = _text(topic).strip().lower()
        if token and token in haystack:
            score += 1.5
            evidence += 1
            reasons.append(f"mentions {token}, which you are working on")
            break

    # Gmail's own guess, as a weak nudge only.
    labels = {_text(l).upper() for l in (message.labels or [])}
    if "IMPORTANT" in labels:
        score += 0.5
        reasons.append("Gmail also flagged it")
    if "CATEGORY_PROMOTIONS" in labels:
        score -= 1.5
        evidence += 1
        reasons.append("Gmail filed it under promotions")

    if score >= 4.0:
        level = Importance.IMPORTANT
    elif score >= 2.0:
        level = Importance.POSSIBLY_IMPORTANT
    elif score <= -2.0:
        level = Importance.LOW_PRIORITY
    else:
        level = Importance.NORMAL

    if not reasons:
        reasons.append("nothing stood out either way")

    # Confidence is about how much evidence there was, not how extreme the
    # score is: a message with one weak signal should be reported tentatively
    # however it lands.
    confidence = max(0.1, min(0.95, 0.15 + 0.16 * evidence))

    return Verdict(level=level, score=score, confidence=confidence,
                   reasons=reasons)
