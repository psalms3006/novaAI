"""Controlled self-improvement: propose, test in isolation, apply, undo.

What this replaces was one function. It read a block of text from the model,
found it in nova.py, wrote the replacement straight into the running source,
saved a .bak beside it and said "restart NOVA to apply changes". No tests, no
isolation, no check on what was being edited, and an undo that depended on
somebody noticing which of the .bak files was the right one. Two of them are
still sitting in the repository root from previous attempts.

The shape here is the one the situation actually needs:

    propose   a problem, an edit, a rationale, a risk
      |
    rehearse  a clean checkout of the last commit, in a git worktree; the
      |       edit applied there; the test suite run there. Nothing in the
      |       working tree has been touched yet, so a proposal that breaks
      |       everything breaks a directory under Temp.
      |
    apply     only if the rehearsal passed, only with the owner's word when
      |       the change reaches anything protected, and only after the
      |       current contents of every file are copied somewhere safe.
      |
    verify    the suite again, against the real working tree.
      |
    rollback  automatic the moment that fails. Never left half-applied.

Every step is written to the journal, including the ones that refused.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .journal import Attempt, Journal
from .protected import is_protected, normalise, protected_among

#: How long the rehearsal may take before it is called a failure. A change
#: that hangs the suite is a change that fails it.
TEST_TIMEOUT_S = 900

#: The suite is the evidence. Narrowing it to "the tests near the change" is
#: how a fix for one thing ships a break in another.
#:
#: sys.executable rather than "python": the interpreter running NOVA is the
#: one with her dependencies installed, and whatever "python" happens to mean
#: on PATH generally is not. It reported "No module named pytest" and that
#: read as a failing change rather than as a broken harness.
#:
#: And no -q here. pytest.ini already sets it in addopts, so passing it again
#: made -qq, which suppresses the summary line -- leaving a rehearsal that
#: said "None passed, None failed" about a run where everything passed. A
#: rehearsal that cannot say what happened is not evidence of anything.
DEFAULT_TEST_COMMAND = (sys.executable, "-m", "pytest", "tests")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def unavailable_reason(root: Optional[Path] = None) -> str:
    """Why self-editing cannot run here, or '' if it can.

    The installed app is compiled: it carries no editable source, no test
    suite and no git history, and its interpreter *is* NOVA.exe -- so the old
    test command, "<interpreter> -m pytest tests", would have started a second
    copy of NOVA instead of running a test. Say so plainly instead of
    rehearsing something that cannot be rehearsed.
    """
    if getattr(sys, "frozen", False):
        return ("I'm running as the installed app, which doesn't include my source code or "
                "test suite, so I can't safely change myself from here. Improvements to NOVA "
                "come through updates. I can write the change up as a proposal for the "
                "developer instead.")
    root = Path(root) if root else repo_root()
    if not (root / "tests").is_dir() or not (root / ".git").exists():
        return ("My source code here isn't a development checkout with its tests, so I can't "
                "rehearse a change to myself safely.")
    return ""


@dataclass
class Edit:
    """One exact replacement in one file.

    Anchored on the old text rather than on line numbers, because the file
    the model read and the file on disk are not always the same file, and a
    line number silently applies the change to the wrong place while an
    anchor simply fails.
    """

    path: str
    old: str
    new: str

    def apply_to(self, text: str) -> str:
        if self.old not in text:
            raise ValueError(
                f"{self.path}: the text to replace is not present. It may "
                f"have changed since it was read.")
        if text.count(self.old) > 1:
            raise ValueError(
                f"{self.path}: the text to replace appears "
                f"{text.count(self.old)} times, so the edit is ambiguous.")
        return text.replace(self.old, self.new, 1)


@dataclass
class Proposal:
    problem: str
    edits: list[Edit]
    rationale: str = ""
    risk: str = "medium"
    attempt_id: str = field(default_factory=lambda: f"si_{uuid.uuid4().hex[:8]}")

    @property
    def files(self) -> list[str]:
        return sorted({normalise(e.path) for e in self.edits})

    @property
    def protected_files(self) -> list[str]:
        return protected_among(self.files)

    @property
    def touches_protected(self) -> bool:
        return bool(self.protected_files)


@dataclass
class Outcome:
    ok: bool
    status: str
    message: str
    attempt_id: str = ""
    tests_ok: Optional[bool] = None
    tests_passed: Optional[int] = None
    tests_failed: Optional[int] = None
    output: str = ""
    files: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    rollback_status: str = ""

    def to_json(self) -> dict:
        return {
            "ok": self.ok, "status": self.status, "message": self.message,
            "attempt_id": self.attempt_id, "tests_ok": self.tests_ok,
            "tests_passed": self.tests_passed, "tests_failed": self.tests_failed,
            "files": self.files, "protected": self.protected,
            "rollback_status": self.rollback_status,
            "output": self.output[-4000:],
        }


_COUNTS = re.compile(r"(?:(\d+) failed)?[,\s]*(?:(\d+) passed)?")


def _parse_pytest(tail: str) -> tuple[Optional[int], Optional[int]]:
    passed = failed = None
    m = re.search(r"(\d+) passed", tail)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", tail)
    if m:
        failed = int(m.group(1))
    if failed is None and passed is not None:
        failed = 0
    return passed, failed


class SelfImprover:
    """Runs the loop above. One instance per repository."""

    def __init__(self, root: Optional[Path] = None,
                 journal: Optional[Journal] = None,
                 test_command: tuple[str, ...] = DEFAULT_TEST_COMMAND):
        self.root = Path(root) if root else repo_root()
        self.journal = journal or Journal()
        self.test_command = tuple(test_command)
        self.snapshot_dir = self._snapshot_root()

    def _snapshot_root(self) -> Path:
        env = os.getenv("NOVA_DATA_DIR", "").strip()
        base = Path(env) if env else (
            Path(os.getenv("APPDATA")) / "NOVA" if os.getenv("APPDATA")
            else self.root)
        d = base / "self" / "snapshots"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── git ──────────────────────────────────────────────────────────────────

    def _git(self, *args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(cwd or self.root),
                              capture_output=True, text=True, timeout=120)

    def head(self) -> str:
        r = self._git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else ""

    # ── rehearsal ────────────────────────────────────────────────────────────

    def rehearse(self, proposal: Proposal) -> Outcome:
        """Apply and test the change somewhere that does not matter.

        The worktree is a clean checkout of the last commit, so the rehearsal
        measures the proposal and not whatever else is half-finished in the
        working tree. That is the trade: it will not see a change that
        depends on uncommitted work, and in exchange a result means something.
        """
        why_not = unavailable_reason(self.root)
        if why_not:
            return Outcome(False, "unavailable", why_not, proposal.attempt_id)
        attempt = Attempt(
            attempt_id=proposal.attempt_id, problem=proposal.problem,
            rationale=proposal.rationale, risk=proposal.risk,
            files=proposal.files, protected=proposal.protected_files,
            status="proposed", baseline_commit=self.head(),
            tests_command=" ".join(self.test_command))
        self.journal.record(attempt)

        if not proposal.edits:
            attempt.status = "failed"
            attempt.notes = "a proposal with no edits"
            self.journal.record(attempt)
            return Outcome(False, "failed", "nothing to change",
                           proposal.attempt_id)

        work = Path(tempfile.mkdtemp(prefix="nova-rehearsal-"))
        tree = work / "tree"
        try:
            r = self._git("worktree", "add", "--detach", str(tree), "HEAD")
            if r.returncode != 0:
                attempt.status = "failed"
                attempt.notes = f"could not create a workspace: {r.stderr[:300]}"
                self.journal.record(attempt)
                return Outcome(False, "failed",
                               f"could not create an isolated workspace: "
                               f"{r.stderr[:200]}", proposal.attempt_id)

            try:
                self._apply_edits(proposal, tree)
            except ValueError as e:
                attempt.status = "failed"
                attempt.notes = str(e)
                self.journal.record(attempt)
                return Outcome(False, "failed", str(e), proposal.attempt_id)

            ok, passed, failed, output = self._run_tests(tree)
            attempt.status = "tested"
            attempt.tests_ok, attempt.tests_passed = ok, passed
            attempt.tests_failed, attempt.tests_output = failed, output[-4000:]
            if ok and proposal.touches_protected:
                attempt.status = "awaiting_approval"
            self.journal.record(attempt)

            if not ok:
                return Outcome(
                    False, "tested",
                    f"the change does not pass the suite "
                    f"({failed} failed, {passed} passed)",
                    proposal.attempt_id, ok, passed, failed, output,
                    proposal.files, proposal.protected_files)

            note = ("ready to apply" if not proposal.touches_protected else
                    "ready, but it touches protected files and needs your "
                    "explicit approval")
            return Outcome(True, attempt.status, note, proposal.attempt_id,
                           ok, passed, failed, output, proposal.files,
                           proposal.protected_files)
        finally:
            self._git("worktree", "remove", "--force", str(tree))
            shutil.rmtree(work, ignore_errors=True)

    def _apply_edits(self, proposal: Proposal, tree: Path) -> None:
        for edit in proposal.edits:
            target = tree / edit.path
            if not target.is_file():
                raise ValueError(
                    f"{edit.path}: no such file in the workspace. Self-edits change "
                    f"existing files only; read the file you mean first (action=read) "
                    f"and propose an edit to it.")
            text = target.read_text(encoding="utf-8")
            target.write_text(edit.apply_to(text), encoding="utf-8",
                              newline="")

    def _run_tests(self, cwd: Path):
        env = dict(os.environ)
        # The rehearsal gets its own data directory, so a change under test
        # cannot write into the real memory store on its way past.
        env["NOVA_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="nova-rehearsal-data-")))
        try:
            r = subprocess.run(self.test_command, cwd=str(cwd), env=env,
                               capture_output=True, text=True,
                               timeout=TEST_TIMEOUT_S)
            out = (r.stdout or "") + (r.stderr or "")
            # The whole output, not the tail. stderr is appended after stdout,
            # and pytest's "N passed" line lives in stdout -- so a run with a
            # long warnings block pushed the counts out of the window and the
            # rehearsal reported "None passed, None failed" on a change that
            # had in fact passed everything.
            passed, failed = _parse_pytest(out)
            return r.returncode == 0, passed, failed, out
        except subprocess.TimeoutExpired:
            return False, None, None, (
                f"the suite did not finish within {TEST_TIMEOUT_S}s; a change "
                f"that hangs the tests has failed them")

    # ── applying, and undoing ────────────────────────────────────────────────

    def apply(self, proposal: Proposal, approved_by=None,
              rehearsed: bool = True) -> Outcome:
        """Put the change into the working tree, and take it out again if the
        suite then fails.

        `approved_by` is an Identification. Anything reaching a protected file
        needs a certain owner — not a probable one, because the promotion an
        impersonator wants most is the one that edits the rules.
        """
        from nova_identity.people import Authority

        attempt = Attempt(
            attempt_id=proposal.attempt_id, problem=proposal.problem,
            rationale=proposal.rationale, risk=proposal.risk,
            files=proposal.files, protected=proposal.protected_files,
            baseline_commit=self.head(),
            tests_command=" ".join(self.test_command),
            authorised_by=(approved_by.describe() if approved_by else "nobody"))

        authority = (approved_by.effective_authority if approved_by
                     else Authority.UNKNOWN)
        if proposal.touches_protected and authority is not Authority.OWNER:
            attempt.status = "rejected"
            attempt.notes = ("touches protected files without the owner's "
                             "approval")
            self.journal.record(attempt)
            return Outcome(
                False, "rejected",
                "this changes " + ", ".join(proposal.protected_files) +
                ", which needs your explicit approval",
                proposal.attempt_id, protected=proposal.protected_files)
        if authority is not Authority.OWNER:
            attempt.status = "rejected"
            attempt.notes = "only the owner may apply a change to NOVA"
            self.journal.record(attempt)
            return Outcome(False, "rejected",
                           "only the owner may change NOVA's own code",
                           proposal.attempt_id)

        if rehearsed:
            trial = self.rehearse(proposal)
            if not trial.ok:
                return trial

        snapshot = self._snapshot(proposal)
        attempt.snapshot = str(snapshot)
        try:
            self._apply_edits_to_root(proposal)
        except ValueError as e:
            self._restore(snapshot, proposal)
            attempt.status = "failed"
            attempt.rollback_status = "restored"
            attempt.notes = str(e)
            self.journal.record(attempt)
            return Outcome(False, "failed", str(e), proposal.attempt_id,
                           rollback_status="restored")

        ok, passed, failed, output = self._run_tests(self.root)
        attempt.tests_ok, attempt.tests_passed = ok, passed
        attempt.tests_failed, attempt.tests_output = failed, output[-4000:]

        if not ok:
            # The rehearsal passed and the real thing did not, which means the
            # working tree differs from the commit it was rehearsed against.
            # Undo now rather than leaving NOVA in a state nobody chose.
            self._restore(snapshot, proposal)
            attempt.status = "rolled_back"
            attempt.rollback_status = "restored automatically"
            self.journal.record(attempt)
            return Outcome(
                False, "rolled_back",
                f"applied, failed the suite ({failed} failed), and was put "
                f"back as it was", proposal.attempt_id, ok, passed, failed,
                output, proposal.files, proposal.protected_files,
                "restored automatically")

        attempt.status = "applied"
        self.journal.record(attempt)
        return Outcome(True, "applied", "applied and the suite passes",
                       proposal.attempt_id, ok, passed, failed, output,
                       proposal.files, proposal.protected_files)

    def _apply_edits_to_root(self, proposal: Proposal) -> None:
        for edit in proposal.edits:
            target = self.root / edit.path
            if not target.is_file():
                raise ValueError(f"{edit.path}: no such file")
            text = target.read_text(encoding="utf-8")
            target.write_text(edit.apply_to(text), encoding="utf-8",
                              newline="")

    def _snapshot(self, proposal: Proposal) -> Path:
        """Copy the current contents of every file the change will touch.

        File contents rather than a git operation, so this works on files git
        does not track and cannot disturb anyone's staged or stashed work.
        The commit is recorded alongside for context, not for the undo.
        """
        stamp = f"{proposal.attempt_id}_{int(time.time())}"
        target = self.snapshot_dir / stamp
        for edit in proposal.edits:
            src = self.root / edit.path
            if not src.is_file():
                continue
            dst = target / edit.path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        (target / "_meta.txt").parent.mkdir(parents=True, exist_ok=True)
        (target / "_meta.txt").write_text(
            f"attempt: {proposal.attempt_id}\ncommit: {self.head()}\n"
            f"problem: {proposal.problem}\n", encoding="utf-8")
        return target

    def _restore(self, snapshot: Path, proposal: Proposal) -> bool:
        ok = True
        for edit in proposal.edits:
            src = snapshot / edit.path
            dst = self.root / edit.path
            if src.is_file():
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    ok = False
        return ok

    def rollback(self, attempt_id: str) -> Outcome:
        """Undo an applied change, on request rather than on failure."""
        row = self.journal.latest(attempt_id)
        if row is None:
            return Outcome(False, "failed", "no such attempt", attempt_id)
        snapshot = Path(row.get("snapshot", ""))
        if not row.get("snapshot") or not snapshot.is_dir():
            return Outcome(False, "failed",
                           "that attempt has no snapshot to restore",
                           attempt_id)
        restored = []
        for rel in row.get("files", []):
            src, dst = snapshot / rel, self.root / rel
            if src.is_file():
                shutil.copy2(src, dst)
                restored.append(rel)

        attempt = Attempt(
            attempt_id=attempt_id, problem=row.get("problem", ""),
            files=row.get("files", []), protected=row.get("protected", []),
            status="rolled_back", snapshot=str(snapshot),
            rollback_status=f"restored {len(restored)} file(s) on request")
        self.journal.record(attempt)
        return Outcome(True, "rolled_back",
                       f"restored {len(restored)} file(s)", attempt_id,
                       files=restored,
                       rollback_status="restored on request")

    # ── reading, which needs no ceremony ─────────────────────────────────────

    def read(self, path: str, max_chars: int = 20000) -> str:
        """NOVA reading her own source. Always allowed, including for the
        protected files — understanding them is how a proposal gets written."""
        target = (self.root / path).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError:
            return f"{path}: outside the repository"
        if not target.is_file():
            return f"{path}: no such file"
        text = target.read_text(encoding="utf-8", errors="replace")
        note = " [protected — changes need your approval]" if is_protected(
            path) else ""
        head = f"[{path}, {len(text.splitlines())} lines{note}]\n\n"
        return head + (text[:max_chars] +
                       ("\n...[truncated]" if len(text) > max_chars else ""))
