"""Working out what to ask a new extension, and asking it.

The tempting reading of "generate tests" is to have a model write a test
file. That produces plausible code which may test nothing, and the only way to
find out is to run it -- which is the decision static inspection exists to
avoid making blindly.

So the probes are derived from what is actually in the package. The AST says
which functions it exposes, whether it reaches the network, whether it reads
credentials; each probe is a few lines answering one question, run through
`trial.try_extension` in a separate process with the environment scrubbed.

The questions worth asking are the unhappy ones. "Does it work" is what a
demo shows. "What does it do with no credentials" and "what does it do with
no network" are what a connector gets wrong, and what the user will hit on a
train.

None of this is verification, and `summary()` is careful not to claim it is.
A handful of smoke probes says a package loads and does not fall over when
something is missing. Whether it does what it claims needs credentials, a
real service, and a person paying attention.
"""
from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from nova_extensions.trial import try_extension

log = logging.getLogger("nova.extensions.probes")

__all__ = ["Probe", "ProbeReport", "generate_probes", "run_probes"]

DEFAULT_MAX_PROBES = 12

_CREDENTIAL_HINTS = ("api_key", "apikey", "token", "secret", "password",
                     "credential", "environ", "getenv")


@dataclass
class Probe:
    question: str
    script: str
    #: True when the probe can only ever show the shape of a failure, not
    #: that the real thing works.
    mocked: bool = False


@dataclass
class ProbeReport:
    results: list = field(default_factory=list)
    mocked_only: bool = False

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r["ok"])

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r["ok"])

    def summary(self) -> str:
        if not self.results:
            return ("I ran nothing — there was nothing in the package I could "
                    "form a question about.")
        lines = [f"{self.passed} of {len(self.results)} probes passed."]
        for result in self.results:
            if not result["ok"]:
                lines.append(f"  failed: {result['question']} — "
                             f"{result['detail'][:160]}")
        # §34: never let a clean run read as verification.
        lines.append(
            "This shows it loads and survives the failures I could simulate. "
            "It is not a real test: nothing here had credentials or a live "
            "service, so it does not show the extension does what it claims.")
        return "\n".join(lines)


def _entry_file(root: Path) -> Optional[Path]:
    for candidate in ("nova_extension.json", "nova-extension.json"):
        path = root / candidate
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entry = root / str(data.get("entrypoint", "main.py"))
                if entry.exists():
                    return entry
            except Exception:
                pass
    for fallback in ("main.py", "__init__.py"):
        if (root / fallback).exists():
            return root / fallback
    pys = sorted(root.glob("*.py"))
    return pys[0] if pys else None


def generate_probes(root, max_probes: int = DEFAULT_MAX_PROBES) -> list[Probe]:
    """Read the package and decide what is worth asking it. Never runs it."""
    root = Path(root)
    entry = _entry_file(root)
    if entry is None:
        return []

    module = entry.stem
    try:
        source = entry.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except Exception:
        # It cannot be parsed, so the only honest question is whether it loads.
        return [_load_probe(root, module)]

    functions = [
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    imports = _imported(tree)
    lowered = source.lower()

    probes: list[Probe] = [_load_probe(root, module)]

    if imports & {"requests", "httpx", "socket", "urllib", "urllib3",
                  "aiohttp", "http"}:
        probes.append(Probe(
            question="what does it do with no network",
            mocked=True,
            script=(
                "import socket\n"
                "def _refuse(*a, **k):\n"
                "    raise OSError('network disabled for this probe')\n"
                "socket.socket = _refuse\n"
                "socket.create_connection = _refuse\n"
                f"import {module}\n"
                "print('OK imported with the network refused')\n"
            )))

    if any(hint in lowered for hint in _CREDENTIAL_HINTS):
        probes.append(Probe(
            question="what does it do with missing credentials",
            mocked=True,
            script=(
                "import os\n"
                "os.environ.clear()\n"
                f"import {module}\n"
                "print('OK imported with no credentials present')\n"
            )))

    for name in functions:
        if len(probes) >= max_probes:
            break
        probes.append(Probe(
            question=f"is {name} callable as documented",
            mocked=True,
            script=(
                f"import {module}\n"
                f"fn = getattr({module}, {name!r}, None)\n"
                "assert fn is not None, 'missing from the module'\n"
                "assert callable(fn), 'present but not callable'\n"
                f"print('OK {name} is exposed and callable')\n"
            )))

    return probes[:max_probes]


def _load_probe(root: Path, module: str) -> Probe:
    return Probe(
        question="does it load",
        script=(f"import {module}\n"
                f"print('OK imported', {module}.__name__)\n"),
    )


def _imported(tree: ast.AST) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def run_probes(root, probes: list, timeout_seconds: int = 30) -> ProbeReport:
    """Run each probe in its own isolated process."""
    root = Path(root)
    report = ProbeReport(mocked_only=all(p.mocked for p in probes) if probes
                         else False)

    for index, probe in enumerate(probes):
        runner = root / f"_nova_probe_{index}.py"
        try:
            # sys.path so the probe can import the package under test; the
            # script itself is removed afterwards so nothing is left behind.
            runner.write_text(
                "import sys\n"
                f"sys.path.insert(0, r{str(root)!r})\n" + probe.script,
                encoding="utf-8")
            result = try_extension(root, runner.name,
                                   timeout_seconds=timeout_seconds)
            detail = (result.stderr or result.stdout or "").strip()
            report.results.append({
                "question": probe.question,
                "ok": bool(result.ok),
                "mocked": probe.mocked,
                "detail": detail.splitlines()[-1] if detail else result.summary(),
            })
        except Exception as exc:
            report.results.append({
                "question": probe.question, "ok": False, "mocked": probe.mocked,
                "detail": f"the probe could not be run: {exc}",
            })
        finally:
            try:
                runner.unlink(missing_ok=True)
            except Exception:
                pass

    return report
