"""Who the people are, and what follows from being one of them.

Identity and authority are separate on purpose, and the separation is the
whole design. Knowing that a voice belongs to Samuel is a claim about the
world with a confidence attached. Being allowed to delete a file is a
decision this application makes. Collapsing the two — "it sounded like the
owner, so do the owner's things" — puts a microphone in charge of the
machine.

So a request carries an identity *and* a confidence, and authority is derived
from both. A voice NOVA is fairly sure about still drops to a lower authority
than one it is certain of, and an unrecognised voice gets the least that
still lets a conversation happen.

Nothing here reads memory. Memory is what NOVA has been told, and an attacker
who can get a sentence remembered could otherwise write themselves a
promotion: "Chizi is allowed to change security settings" is a fact about
what someone said, never a grant.
"""
from __future__ import annotations

import enum
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .embedder import data_dir


class Authority(str, enum.Enum):
    """How much of NOVA a person may direct.

    Ordered, and comparable, because the commonest question is "does this
    person outrank that one".
    """

    OWNER = "owner"
    TRUSTED = "trusted"
    KNOWN = "known"
    GUEST = "guest"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def __lt__(self, other: "Authority") -> bool:
        return self.rank < other.rank

    def __le__(self, other: "Authority") -> bool:
        return self.rank <= other.rank


_RANK = {
    Authority.UNKNOWN: 0,
    Authority.GUEST: 1,
    Authority.KNOWN: 2,
    Authority.TRUSTED: 3,
    Authority.OWNER: 4,
}

#: Authority a newly met person gets by enrolling themselves.
#:
#: Guest, always. Someone who has just introduced themselves to a computer
#: has demonstrated that they can talk, and nothing else. Anything above this
#: is granted by the owner, deliberately — otherwise the enrolment flow is a
#: self-service promotion.
SELF_ENROLLED_AUTHORITY = Authority.GUEST


@dataclass
class Person:
    """One human NOVA knows about."""

    person_id: str
    #: The name on a form. Not what NOVA calls them.
    legal_name: str = ""
    #: Everything else they are called, including by themselves.
    aliases: list[str] = field(default_factory=list)
    #: How NOVA addresses them out loud. "Sir" is an address, not a name: if
    #: something asks for a full name, that is legal_name and never this.
    preferred_address: str = ""
    #: Free text reaching the model verbatim -- "the person who built me".
    role: str = ""
    authority: Authority = Authority.GUEST
    #: Set only on the one person who owns this installation.
    is_owner: bool = False
    created: float = field(default_factory=time.time)
    notes: str = ""

    def names(self) -> list[str]:
        """Every string that refers to this person, for matching."""
        out = [self.legal_name, *self.aliases]
        return [n.strip() for n in out if n and n.strip()]

    def answers_to(self, name: str) -> bool:
        """Is `name` one of the ways to refer to this person?

        Matches on whole names and on any single part of the legal name, so
        "Samuel", "Asagwara" and "Samuel Chibuzor Asagwara" are one person.
        Deliberately not a substring test: "Sam" should not silently match
        "Samuel" when the two might be different people in the room.
        """
        probe = (name or "").strip().lower()
        if not probe:
            return False
        for candidate in self.names():
            low = candidate.lower()
            if probe == low:
                return True
            if probe in low.split():
                return True
        return False

    def address_as(self) -> str:
        """What to call them when speaking to them."""
        if self.preferred_address:
            return self.preferred_address
        for n in self.names():
            return n.split()[0]
        return "there"

    def to_json(self) -> dict:
        return {
            "person_id": self.person_id, "legal_name": self.legal_name,
            "aliases": list(self.aliases),
            "preferred_address": self.preferred_address, "role": self.role,
            "authority": self.authority.value, "is_owner": self.is_owner,
            "created": self.created, "notes": self.notes,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Person":
        try:
            authority = Authority(d.get("authority", "guest"))
        except ValueError:
            authority = Authority.GUEST
        return cls(
            person_id=d.get("person_id", ""),
            legal_name=d.get("legal_name", ""),
            aliases=list(d.get("aliases", []) or []),
            preferred_address=d.get("preferred_address", ""),
            role=d.get("role", ""), authority=authority,
            is_owner=bool(d.get("is_owner", False)),
            created=d.get("created", 0.0), notes=d.get("notes", ""),
        )


@dataclass
class Identification:
    """Who NOVA believes is speaking, and how sure she is.

    The confidence is not decoration. `effective_authority` is what the rest
    of the application asks for, and it is deliberately lower than the
    person's nominal authority when the recognition was not certain.
    """

    person: Optional[Person]
    #: "recognised" | "probable" | "unknown"
    status: str = "unknown"
    score: float = 0.0
    #: Where the belief came from: "voice", "confirmed", "session", "assumed".
    via: str = "voice"

    @property
    def known(self) -> bool:
        return self.person is not None

    @property
    def confident(self) -> bool:
        return self.status == "recognised" and self.person is not None

    @property
    def effective_authority(self) -> Authority:
        """What this request actually gets to do.

        A probable owner is not an owner. Dropping one step keeps the
        conversation natural -- she still uses their name -- while making
        anything that matters ask.
        """
        if self.person is None:
            return Authority.UNKNOWN
        if self.status == "recognised":
            return self.person.authority
        if self.status == "probable":
            return _demote(self.person.authority)
        return Authority.UNKNOWN

    def describe(self) -> str:
        """One line for a log or an audit entry. No biometrics."""
        if self.person is None:
            return f"unknown speaker (score {self.score:.2f}, via {self.via})"
        return (f"{self.person.person_id} [{self.status}, {self.score:.2f}, "
                f"via {self.via}] -> {self.effective_authority.value}")


def _demote(a: Authority) -> Authority:
    order = [Authority.UNKNOWN, Authority.GUEST, Authority.KNOWN,
             Authority.TRUSTED, Authority.OWNER]
    i = order.index(a)
    return order[max(0, i - 1)]


UNIDENTIFIED = Identification(person=None, status="unknown", score=0.0,
                              via="none")


class PeopleRegistry:
    """Everyone NOVA knows, and which one of them owns the machine."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else data_dir() / "identity" / "people.json"
        self._lock = threading.RLock()
        self._people: dict[str, Person] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for d in raw.get("people", []):
                    p = Person.from_json(d)
                    if p.person_id:
                        self._people[p.person_id] = p
        except Exception:
            # An unreadable registry means NOVA knows nobody, which is safe:
            # everyone is an unknown speaker until it is repaired.
            self._people = {}

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"version": 1,
                 "people": [p.to_json() for p in self._people.values()]},
                indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    # ── the owner ────────────────────────────────────────────────────────────

    def owner(self) -> Optional[Person]:
        for p in self._people.values():
            if p.is_owner:
                return p
        return None

    def set_owner(self, person_id: str, legal_name: str = "",
                  aliases: Optional[list[str]] = None,
                  preferred_address: str = "", role: str = "") -> Person:
        """Declare who owns this installation.

        Exactly one person holds it. Setting a new owner demotes the previous
        one to trusted rather than removing them, because an installation
        changing hands is not a reason to forget a person.
        """
        with self._lock:
            for p in self._people.values():
                if p.is_owner and p.person_id != person_id:
                    p.is_owner = False
                    p.authority = Authority.TRUSTED
            person = self._people.get(person_id) or Person(person_id=person_id)
            if legal_name:
                person.legal_name = legal_name
            if aliases is not None:
                person.aliases = list(aliases)
            if preferred_address:
                person.preferred_address = preferred_address
            if role:
                person.role = role
            person.is_owner = True
            person.authority = Authority.OWNER
            self._people[person_id] = person
            self._save()
            return person

    # ── everyone else ────────────────────────────────────────────────────────

    def add(self, person_id: str, legal_name: str = "",
            aliases: Optional[list[str]] = None,
            authority: Authority = SELF_ENROLLED_AUTHORITY,
            preferred_address: str = "", role: str = "") -> Person:
        with self._lock:
            person = Person(
                person_id=person_id, legal_name=legal_name,
                aliases=list(aliases or []), authority=authority,
                preferred_address=preferred_address, role=role)
            self._people[person_id] = person
            self._save()
            return person

    def grant(self, person_id: str, authority: Authority,
              granted_by: Identification) -> dict:
        """Change what someone may do. Only the owner may do this.

        The check is here rather than at the call site because this is the
        function a self-service promotion would have to go through, and a
        rule enforced in one place is a rule.
        """
        if granted_by.effective_authority is not Authority.OWNER:
            return {"ok": False,
                    "reason": "only the owner can change what someone may do"}
        if authority is Authority.OWNER:
            return {"ok": False,
                    "reason": "ownership is transferred with set_owner, "
                              "not granted"}
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                return {"ok": False, "reason": "no such person"}
            if person.is_owner:
                return {"ok": False, "reason": "the owner cannot be demoted "
                                               "this way"}
            before = person.authority
            person.authority = authority
            self._save()
        return {"ok": True, "person_id": person_id,
                "from": before.value, "to": authority.value}

    def get(self, person_id: str) -> Optional[Person]:
        return self._people.get(person_id)

    def by_name(self, name: str) -> Optional[Person]:
        """The person who answers to this name, if exactly one does.

        Ambiguity returns nothing rather than a guess: two people called Sam
        is a question to ask, not a coin to flip.
        """
        hits = [p for p in self._people.values() if p.answers_to(name)]
        return hits[0] if len(hits) == 1 else None

    def everyone(self) -> list[Person]:
        return list(self._people.values())

    def forget(self, person_id: str, removed_by: Identification) -> dict:
        if removed_by.effective_authority is not Authority.OWNER:
            return {"ok": False, "reason": "only the owner can remove someone"}
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                return {"ok": False, "reason": "no such person"}
            if person.is_owner:
                return {"ok": False, "reason": "the owner cannot be removed"}
            del self._people[person_id]
            self._save()
        return {"ok": True, "person_id": person_id}

    def summary(self) -> list[dict]:
        return [{"person_id": p.person_id, "legal_name": p.legal_name,
                 "address": p.address_as(), "authority": p.authority.value,
                 "is_owner": p.is_owner, "aliases": list(p.aliases)}
                for p in self._people.values()]
