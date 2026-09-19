"""The parts of NOVA that NOVA may not quietly rewrite.

Self-improvement that can edit its own safeguards is not self-improvement, it
is an unlocked door with a note on it. The failure does not need malice: a
model asked to make the tests pass, given the ability to edit the tests, will
sometimes edit the tests.

So some files are readable and analysable and proposable-against, and simply
not applicable without the owner saying so in that moment. The list is short
on purpose. A protected set large enough to need a search box is one nobody
audits, and everything in it is here for the same reason — it is machinery
that decides whether something is allowed, or machinery that lets a mistake
be undone.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

#: Patterns are matched against repository-relative POSIX paths.
PROTECTED_PATTERNS: tuple[str, ...] = (
    # Who may do what.
    "nova_core/permissions.py",
    "nova_safety.py",
    "trust/*",
    "nova_core/trust.py",

    # Who anyone is. Ownership, voice profiles, the authority mapping.
    "nova_identity/*",

    # The machinery of self-modification, including this file. A change that
    # can rewrite the rules about changes is only ever applied deliberately.
    "nova_self/*",

    # Credentials and the secure store.
    "nova_secure_store.py",
    "desk/creds.py",
    "desk/account_api.py",

    # The tests that hold all of the above honest. Without this line the
    # shortest path to a passing suite is deleting the assertion.
    "tests/test_identity_authority.py",
    "tests/test_speaker_profiles.py",
    "tests/test_permissions.py",
    "tests/test_trust_boundary.py",
    "tests/test_action_permissions.py",
    "tests/test_self_improvement.py",
    "tests/test_cloud_security.py",
    "tests/conftest.py",
)


def normalise(path: str | Path, root: str | Path | None = None) -> str:
    """Repository-relative POSIX form, for matching and for the audit log."""
    p = Path(path)
    if root is not None:
        try:
            p = p.resolve().relative_to(Path(root).resolve())
        except Exception:
            p = Path(path)
    return p.as_posix().lstrip("./")


def is_protected(path: str | Path, root: str | Path | None = None) -> bool:
    """Does this path need the owner's explicit say-so before it changes?"""
    rel = normalise(path, root)
    for pattern in PROTECTED_PATTERNS:
        if fnmatch.fnmatch(rel, pattern):
            return True
        # A directory pattern covers what is under it.
        if pattern.endswith("/*") and rel.startswith(pattern[:-1]):
            return True
    return False


def protected_among(paths) -> list[str]:
    """Which of these need the owner. Empty means the change is ordinary."""
    return sorted({normalise(p) for p in paths if is_protected(p)})
