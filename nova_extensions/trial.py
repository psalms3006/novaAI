"""Running a candidate extension once, at arm's length.

Static inspection is safe and limited: it can see that a package imports
`socket`, never whether it works. Eventually something has to run, and the
only question worth arguing about is what it can reach when it does.

What this gives, on Windows, where there is no seccomp and no namespaces:

    a separate process      a crash, a hang or a memory bomb costs a
                            subprocess, not the assistant
    a scrubbed environment  the user's API keys are not sitting in
                            os.environ for anything that thinks to look
    a temporary directory   the obvious relative-path write lands somewhere
                            disposable
    a hard timeout          `while True` costs seconds, not a session

What it is **not** is containment. A determined package can still open a
socket, write an absolute path, or read files this user can read, because
Windows offers no cheap way to stop it from Python. `describe_isolation()`
says so in as many words, and a test asserts that it keeps saying so -- a
reassuring name would be worse than an accurate sentence, because someone
would rely on it.

So this is a way to find out whether a plausible-looking package does what it
claims, with the blast radius of a mistake reduced. It is not a reason to
trust the package, and nothing here marks anything trusted.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("nova.extensions.trial")

__all__ = ["TrialResult", "try_extension", "describe_isolation"]

DEFAULT_TIMEOUT_S = 30
DEFAULT_MAX_OUTPUT = 20_000

#: Names that obviously carry secrets. Matched case-insensitively as
#: substrings, because the interesting ones are never spelled the same twice:
#: GEMINI_API_KEY, AWS_SECRET_ACCESS_KEY, SOME_VENDOR_TOKEN.
_SECRET_NAME_MARKERS = (
    "key", "token", "secret", "password", "passwd", "credential", "auth",
    "session", "cookie", "private", "signature", "salt", "apikey",
)

#: Variables a Python process genuinely needs to start on Windows. Strip
#: everything and nothing runs, which teaches nothing.
_KEEP = (
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "TEMP", "TMP", "PATH",
    "LANG", "LC_ALL", "PYTHONIOENCODING",
)

#: A value shaped like a credential is dropped whatever it is called.
_SECRET_VALUE = re.compile(
    r"(ya29\.|AIza|sk-|ghp_|gho_|xox[baprs]-|AKIA|-----BEGIN)")


def describe_isolation() -> str:
    """What the trial does and does not protect against.

    Deliberately blunt. Somebody will decide how much to trust a result on
    the strength of this sentence.
    """
    return (
        "The candidate runs in a separate process, with credentials removed "
        "from its environment, in a temporary directory, under a timeout. "
        "This is isolation, not a sandbox: it is not contained, and it can "
        "still reach the network, write to absolute paths, and read anything "
        "this user account can read. Treat a clean trial as evidence that it "
        "runs, not that it is safe."
    )


@dataclass
class TrialResult:
    ok: bool = False
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""
    workdir: str = ""

    def summary(self) -> str:
        if self.error:
            return self.error
        if self.timed_out:
            return ("It didn't finish — I stopped it after the time limit, so "
                    "it either hangs or needs longer than a trial allows.")
        if self.ok:
            return "It ran and exited cleanly."
        first = (self.stderr or "").strip().splitlines()
        tail = first[-1] if first else f"exit code {self.returncode}"
        return f"It ran and failed: {tail[:200]}"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "returncode": self.returncode,
            "timed_out": self.timed_out, "error": self.error,
            "stdout": self.stdout, "stderr": self.stderr,
            "isolation": describe_isolation(),
        }


def _scrubbed_environment() -> dict:
    """A minimal environment with nothing credential-shaped in it."""
    out = {}
    for name in _KEEP:
        value = os.environ.get(name)
        if value is None:
            continue
        lowered = name.lower()
        if any(marker in lowered for marker in _SECRET_NAME_MARKERS):
            continue
        if _SECRET_VALUE.search(value):
            continue
        out[name] = value
    # Never inherit NOVA's own configuration either: a candidate has no
    # business knowing where the user's data lives.
    out.pop("NOVA_DATA_DIR", None)
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    out["PYTHONIOENCODING"] = "utf-8"
    return out


def try_extension(path, entrypoint: str,
                  timeout_seconds: int = DEFAULT_TIMEOUT_S,
                  max_output: int = DEFAULT_MAX_OUTPUT) -> TrialResult:
    """Run one entrypoint once, at arm's length. Never raises."""
    root = Path(path)
    target = root / entrypoint

    if not target.exists():
        return TrialResult(
            error=f"The manifest names an entrypoint, {entrypoint}, that is "
                  f"not in the package, so there is nothing to try.")

    workdir = tempfile.mkdtemp(prefix="nova-trial-")
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-B", str(target.resolve())],
            cwd=workdir,                      # relative writes land here
            env=_scrubbed_environment(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return TrialResult(
            timed_out=True, workdir=workdir,
            stdout=_clip(exc.stdout, max_output),
            stderr=_clip(exc.stderr, max_output))
    except Exception as exc:
        log.debug("[TRIAL] could not start the candidate", exc_info=True)
        return TrialResult(error=f"I couldn't run it: {exc}", workdir=workdir)

    return TrialResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=_clip(proc.stdout, max_output),
        stderr=_clip(proc.stderr, max_output),
        workdir=workdir,
    )


def _clip(text, limit: int) -> str:
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters]"
