"""run_command — the one gated way code gets executed on request.

Running a script is CODE_EXECUTE: it earns the confirmation gate, the way
every other consequence-charged tool does. file_processor used to run .py
files under its read capability, which is what made "process this file"
indistinguishable from "execute this file"; that action now refuses and
points here, where the permission engine charges the right capability and
the person is asked before anything starts.

What runs: a Python file, or a shell-free argument list the *user* named in
conversation. What never runs: a path the model invented that is not a real
file, and anything the policy will not let the file tools even open.
"""
from __future__ import annotations

import sys
from pathlib import Path

from core.shell_manager import execute_safe


def execute(args: dict) -> str:
    file_path = str(args.get("file_path") or args.get("path") or "").strip()
    timeout = int(args.get("timeout") or 30)
    if not file_path:
        return "Error: file_path is required (the script to run)."

    path = Path(file_path).expanduser()

    # The filesystem policy decides whether this file may be touched at
    # all; execution implies reading it.
    try:
        from nova_core.fs_policy import FilePolicyError, require
        try:
            path = require(str(path), write=False, delete=False, must_exist=True)
        except FilePolicyError as e:
            return f"Refused: {e}"
    except ImportError:
        if not path.exists():
            return f"Error: file not found: {file_path}"

    if path.suffix.lower() != ".py":
        return (f"Refused: run_command executes Python scripts; {path.name} "
                f"is not one. Ask for the tool that fits it instead.")

    # The interpreter is the one this process runs under: never a bare
    # "python" from PATH, which is whichever copy an attacker's earlier
    # step put there first.
    result = execute_safe([sys.executable, str(path)], timeout=timeout)

    if result.error:
        return f"Run failed: {result.error}"
    out = []
    if result.stdout:
        out.append(f"STDOUT:\n{result.stdout}")
    if result.stderr:
        out.append(f"STDERR:\n{result.stderr}")
    if not out:
        return f"Exit code {result.returncode} (no output)."
    return f"Exit code {result.returncode}\n" + "\n".join(out)


__all__ = ["execute"]
