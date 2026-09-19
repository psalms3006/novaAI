"""NOVA changing NOVA, and the conditions that make it safe to allow.

What this replaced was one function: read a block of text from the model,
find it in nova.py, write the replacement into the running source, save a
.bak beside it, say "restart NOVA to apply changes". No tests, no isolation,
no check on what was being edited, and an undo that depended on somebody
noticing which .bak was the right one. Two are still in the repository root
from previous attempts.

The properties worth holding, each tested below against a real throwaway git
repository rather than a mock:

    a change is rehearsed somewhere that does not matter before it is applied
    a change that fails the suite never reaches the working tree
    a change that fails after being applied is put back automatically
    the safeguards are not editable without the owner saying so
    nobody but the owner applies anything
    every attempt is written down, including the refusals
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nova_identity.people import Authority, Identification, Person
from nova_self.improve import Edit, Proposal, SelfImprover
from nova_self.journal import Journal
from nova_self.protected import is_protected, protected_among


# ── a small real repository to operate on ────────────────────────────────────

@pytest.fixture()
def repo(tmp_path):
    """A git repo with one module and one test, so the suite means something."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)

    (root / "calc.py").write_text(textwrap.dedent('''
        def add(a, b):
            return a + b


        def double(x):
            return x * 2
    ''').strip() + "\n", encoding="utf-8")

    (root / "tests" / "test_calc.py").write_text(textwrap.dedent('''
        from calc import add, double

        def test_add():
            assert add(2, 3) == 5

        def test_double():
            assert double(4) == 8
    ''').strip() + "\n", encoding="utf-8")

    (root / "conftest.py").write_text("import sys, os\n"
                                      "sys.path.insert(0, os.path.dirname(__file__))\n",
                                      encoding="utf-8")

    def git(*a):
        return subprocess.run(["git", *a], cwd=str(root),
                              capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return root


@pytest.fixture()
def improver(repo, tmp_path):
    return SelfImprover(
        root=repo,
        journal=Journal(path=tmp_path / "journal.jsonl"),
        test_command=(sys.executable, "-m", "pytest", "tests", "-q"))


def owner_id():
    return Identification(
        person=Person(person_id="samuel", legal_name="Samuel Chibuzor Asagwara",
                      authority=Authority.OWNER, is_owner=True),
        status="recognised", score=0.95, via="voice")


def guest_id():
    return Identification(
        person=Person(person_id="visitor", authority=Authority.GUEST),
        status="recognised", score=0.9, via="voice")


def probable_owner():
    return Identification(
        person=Person(person_id="samuel", authority=Authority.OWNER,
                      is_owner=True),
        status="probable", score=0.5, via="voice")


GOOD = Edit("calc.py", "return x * 2", "return x + x")
BREAKS = Edit("calc.py", "return a + b", "return a - b")


# ── the protected set ────────────────────────────────────────────────────────

def test_the_safeguards_are_protected():
    for p in ("nova_core/permissions.py", "nova_safety.py",
              "nova_identity/people.py", "nova_identity/authority.py"):
        assert is_protected(p), p


def test_the_self_improvement_machinery_protects_itself():
    """A change that can rewrite the rules about changes is only ever applied
    deliberately."""
    for p in ("nova_self/improve.py", "nova_self/protected.py",
              "nova_self/journal.py"):
        assert is_protected(p), p


def test_the_tests_that_hold_the_safeguards_honest_are_protected():
    """Without this the shortest route to a passing suite is deleting the
    assertion that fails."""
    assert is_protected("tests/test_identity_authority.py")
    assert is_protected("tests/test_self_improvement.py")
    assert is_protected("tests/conftest.py")


def test_ordinary_code_is_not_protected():
    """A protected set large enough to need a search box is one nobody
    audits, and it would make self-improvement useless."""
    for p in ("desk/live_session.py", "nova.py", "terminal_voice.py",
              "living_memory.py"):
        assert not is_protected(p), p


# ── rehearsal ────────────────────────────────────────────────────────────────

def test_a_good_change_passes_rehearsal(improver):
    out = improver.rehearse(Proposal("double is unclear", [GOOD]))
    assert out.ok and out.tests_ok
    assert out.tests_passed == 2 and out.tests_failed == 0


def test_rehearsal_does_not_touch_the_working_tree(improver, repo):
    before = (repo / "calc.py").read_text(encoding="utf-8")
    improver.rehearse(Proposal("double is unclear", [GOOD]))
    assert (repo / "calc.py").read_text(encoding="utf-8") == before, (
        "the rehearsal edited the real file")


def test_a_breaking_change_fails_rehearsal_and_changes_nothing(improver, repo):
    before = (repo / "calc.py").read_text(encoding="utf-8")
    out = improver.rehearse(Proposal("make add subtract", [BREAKS]))
    assert not out.ok and out.tests_ok is False
    assert out.tests_failed == 1
    assert (repo / "calc.py").read_text(encoding="utf-8") == before


def test_an_edit_whose_anchor_is_gone_fails_cleanly(improver):
    out = improver.rehearse(Proposal(
        "stale", [Edit("calc.py", "return x * 99", "return x")]))
    assert not out.ok
    assert "not present" in out.message


def test_an_ambiguous_edit_is_refused(improver, repo):
    """Two matches means the change would land somewhere nobody chose."""
    (repo / "calc.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return 1\n",
        encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "two"], cwd=str(repo),
                   capture_output=True)
    out = improver.rehearse(Proposal("x", [Edit("calc.py", "return 1",
                                                "return 2")]))
    assert not out.ok and "ambiguous" in out.message


def test_a_missing_file_is_refused(improver):
    out = improver.rehearse(Proposal("x", [Edit("nope.py", "a", "b")]))
    assert not out.ok and "no such file" in out.message


# ── who may apply ────────────────────────────────────────────────────────────

def test_a_guest_cannot_change_nova(improver, repo):
    before = (repo / "calc.py").read_text(encoding="utf-8")
    out = improver.apply(Proposal("x", [GOOD]), approved_by=guest_id())
    assert not out.ok and out.status == "rejected"
    assert (repo / "calc.py").read_text(encoding="utf-8") == before


def test_nobody_at_all_cannot_change_nova(improver):
    out = improver.apply(Proposal("x", [GOOD]), approved_by=None)
    assert not out.ok and out.status == "rejected"


def test_a_protected_file_needs_a_certain_owner(improver):
    """A probable owner is the impersonator's best case, and editing the
    rules is what they would want most."""
    p = Proposal("x", [Edit("nova_identity/people.py", "a", "b")])
    out = improver.apply(p, approved_by=probable_owner())
    assert not out.ok and out.status == "rejected"
    assert "approval" in out.message or "owner" in out.message


def test_the_owner_can_apply_an_ordinary_change(improver, repo):
    out = improver.apply(Proposal("double is unclear", [GOOD]),
                         approved_by=owner_id())
    assert out.ok and out.status == "applied"
    assert "return x + x" in (repo / "calc.py").read_text(encoding="utf-8")


# ── undo ─────────────────────────────────────────────────────────────────────

def test_a_change_that_breaks_the_suite_is_put_back_automatically(improver, repo):
    """The rehearsal runs against the last commit; the working tree can
    differ. When it does, the change is undone rather than left standing."""
    before = (repo / "calc.py").read_text(encoding="utf-8")
    out = improver.apply(Proposal("break it", [BREAKS]),
                         approved_by=owner_id(), rehearsed=False)
    assert not out.ok
    assert out.status == "rolled_back"
    assert "automatically" in out.rollback_status
    assert (repo / "calc.py").read_text(encoding="utf-8") == before, (
        "NOVA was left in a state nobody chose")


def test_an_applied_change_can_be_undone_on_request(improver, repo):
    before = (repo / "calc.py").read_text(encoding="utf-8")
    applied = improver.apply(Proposal("double is unclear", [GOOD]),
                             approved_by=owner_id())
    assert applied.ok
    assert (repo / "calc.py").read_text(encoding="utf-8") != before

    undone = improver.rollback(applied.attempt_id)
    assert undone.ok
    assert (repo / "calc.py").read_text(encoding="utf-8") == before


def test_rolling_back_something_unknown_says_so(improver):
    out = improver.rollback("si_doesnotexist")
    assert not out.ok and "no such attempt" in out.message


# ── the record ───────────────────────────────────────────────────────────────

def test_every_attempt_is_written_down(improver):
    improver.rehearse(Proposal("a good one", [GOOD]))
    rows = improver.journal.summary()
    assert rows and rows[0]["problem"] == "a good one"
    assert rows[0]["tests_ok"] is True


def test_refusals_are_written_down_too(improver):
    """The refused attempts are the interesting ones afterwards."""
    improver.apply(Proposal("sneaky", [GOOD]), approved_by=guest_id())
    rows = improver.journal.summary()
    assert rows[0]["status"] == "rejected"
    assert rows[0]["authorised_by"]


def test_the_history_of_one_attempt_is_kept_not_overwritten(improver, repo):
    """"Tested, applied, rolled back forty seconds later" is the shape worth
    seeing, and a single mutable row erases it."""
    out = improver.apply(Proposal("double is unclear", [GOOD]),
                         approved_by=owner_id())
    improver.rollback(out.attempt_id)
    states = [r["status"] for r in improver.journal.history(out.attempt_id)]
    assert "applied" in states and "rolled_back" in states
    assert len(states) >= 3, states


def test_the_record_names_the_protected_files_it_refused(improver):
    p = Proposal("x", [Edit("nova_core/permissions.py", "a", "b")])
    improver.apply(p, approved_by=guest_id())
    row = improver.journal.latest(p.attempt_id)
    assert row["protected"] == ["nova_core/permissions.py"]


# ── reading ──────────────────────────────────────────────────────────────────

def test_nova_can_read_her_own_source(improver):
    text = improver.read("calc.py")
    assert "def add" in text


def test_reading_a_protected_file_is_allowed_and_says_it_is_protected(improver):
    """Understanding the safeguards is how a proposal about them gets
    written. Only applying is restricted."""
    out = SelfImprover(root=Path(__file__).resolve().parent.parent).read(
        "nova_core/permissions.py")
    assert "Capability" in out
    assert "protected" in out.splitlines()[0]


def test_reading_outside_the_repository_is_refused(improver):
    assert "outside the repository" in improver.read("../../../etc/passwd")
