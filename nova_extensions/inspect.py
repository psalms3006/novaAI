"""Reading a package without running it, and saying what it could do.

The rule this module exists to enforce: **inspection never executes.** Not the
entrypoint, not a test, not an import. Importing a Python module runs its
top-level code, so "just import it and see" hands control to the thing being
judged, before any judgement has happened. Everything here is text and syntax
trees.

The second rule: a manifest is a claim. An extension declaring
`"permissions": []` while importing `socket` has told us something genuinely
useful -- that its author is careless or dishonest -- and that is worth more
than the declaration. Claims are checked against the code, and disagreement is
reported rather than resolved in the package's favour.

The output is a description and a verdict, and the verdict is allowed to be
"no". Nothing here installs, enables or trusts anything: a clean read produces
the state `inspected`, which is a long way from `verified`.
"""
from __future__ import annotations

import ast
import enum
import fnmatch
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("nova.extensions")

__all__ = ["Risk", "InspectionReport", "inspect_resource", "MANIFEST_NAMES"]

MANIFEST_NAMES = ("nova_extension.json", "nova-extension.json")

#: Never read. Reading a key in order to decide not to read it is still
#: reading it, and the contents would then be in a report, a log and a prompt.
_SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_dsa", "id_ecdsa",
    "id_ed25519", "*.p12", "*.pfx", "credentials.json", "secrets.json",
    "*.keystore", ".npmrc", ".netrc", ".pypirc",
)

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", "dist", "build", ".tox"}

#: Imports that mean the package can reach beyond itself. Not damning on their
#: own -- a weather connector needs the network -- but they must be declared.
_CAPABILITY_IMPORTS = {
    "network": {"socket", "requests", "httpx", "urllib", "urllib3", "http",
                "aiohttp", "websockets", "ftplib", "smtplib", "telnetlib"},
    "process": {"subprocess", "multiprocessing", "pty", "os.system"},
    "filesystem": {"shutil", "tempfile", "pathlib", "os"},
    "code": {"importlib", "ctypes", "marshal", "pickle"},
}

#: Calls that turn data into code. There is no benign reading of these in
#: something a user was handed and asked to trust.
_DANGEROUS_CALLS = {"eval", "exec", "compile", "__import__"}

_DANGEROUS_ATTRS = {("os", "system"), ("os", "popen"),
                    ("subprocess", "call"), ("subprocess", "run"),
                    ("subprocess", "Popen"), ("subprocess", "check_output")}


class Risk(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


@dataclass
class InspectionReport:
    path: str
    risk: Risk = Risk.UNKNOWN
    trust: str = "untrusted"
    installable: bool = False
    manifest: Optional[dict] = None
    capabilities_seen: set = field(default_factory=set)
    undeclared: set = field(default_factory=set)
    files_examined: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "risk": self.risk.value,
            "trust": self.trust,
            "installable": self.installable,
            "manifest": self.manifest,
            "capabilities_seen": sorted(self.capabilities_seen),
            "undeclared": sorted(self.undeclared),
            "files_examined": list(self.files_examined),
            "reasons": list(self.reasons),
        }

    def summary(self) -> str:
        """What NOVA says about it. Facts, and an honest verdict."""
        if self.risk is Risk.UNKNOWN and not self.files_examined:
            return f"I couldn't read anything at {self.path}."
        caps = ", ".join(sorted(self.capabilities_seen)) or "nothing beyond itself"
        lines = [
            f"I read {len(self.files_examined)} file(s) without running any of them.",
            f"It can reach: {caps}.",
            f"Risk looks {self.risk.value}.",
        ]
        if self.undeclared:
            lines.append("It does things it does not declare: "
                         + ", ".join(sorted(self.undeclared)) + ".")
        lines.append("I have not installed or trusted it.")
        return " ".join(lines)


def _is_secret(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_NAMES)


def _gitignore_patterns(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line.rstrip("/"))
    except OSError:
        pass
    return out


def _ignored(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(parts[0], pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts[:-1]):
            return True
    return False


def inspect_resource(path: Any, max_files: int = 200) -> InspectionReport:
    """Read a folder and describe what it could do. Never runs it."""
    root = Path(path)
    report = InspectionReport(path=str(root))

    if not root.exists() or not root.is_dir():
        report.reasons.append("there is nothing readable at that path")
        return report

    patterns = _gitignore_patterns(root)
    found_secret = False
    candidates: list[Path] = []

    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        if any(part in _SKIP_DIRS for part in item.relative_to(root).parts):
            continue
        rel = item.relative_to(root).as_posix()
        if _is_secret(item.name):
            found_secret = True
            continue                      # never opened
        if _ignored(rel, patterns):
            continue
        candidates.append(item)

    truncated = len(candidates) > max_files
    if truncated:
        candidates = candidates[:max_files]
        report.reasons.append(
            f"only the first {max_files} files were examined; the rest were "
            f"not read, so this verdict does not cover them")

    if found_secret:
        report.reasons.append(
            "it contains credential files, which were skipped rather than "
            "read")

    manifest = _read_manifest(root, report)
    dangerous = False

    for item in candidates:
        rel = item.relative_to(root).as_posix()
        report.files_examined.append(rel)
        if item.suffix != ".py":
            continue
        try:
            source = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            report.reasons.append(
                f"{rel} could not be parsed ({exc.msg}); a file NOVA cannot "
                f"read is a file NOVA cannot vouch for")
            dangerous = True
            continue
        if _walk(tree, report, rel):
            dangerous = True

    _check_declarations(manifest, report)

    if dangerous:
        report.risk = Risk.HIGH
    elif report.undeclared:
        report.risk = Risk.MEDIUM
    elif report.capabilities_seen - {"filesystem"}:
        report.risk = Risk.MEDIUM
    else:
        report.risk = Risk.LOW

    report.trust = "inspected"
    # Reading something is not grounds for running it. Installation requires
    # a sandboxed trial and a person saying yes, neither of which happens here.
    report.installable = False
    if not report.reasons:
        report.reasons.append("nothing alarming, but it has only been read")
    return report


def _read_manifest(root: Path, report: InspectionReport) -> Optional[dict]:
    for name in MANIFEST_NAMES:
        path = root / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            report.reasons.append(f"{name} is not readable JSON ({exc})")
            return None
        if not isinstance(data, dict):
            report.reasons.append(f"{name} is not an object")
            return None
        report.manifest = data
        entry = data.get("entrypoint")
        if entry and not (root / str(entry)).exists():
            report.reasons.append(
                f"the manifest names an entrypoint, {entry}, that is not in "
                f"the package")
        return data
    report.reasons.append("no manifest, so it makes no claims to check")
    return None


def _walk(tree: ast.AST, report: InspectionReport, rel: str) -> bool:
    dangerous = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in _imported_names(node):
                for capability, modules in _CAPABILITY_IMPORTS.items():
                    if name in modules or name.split(".")[0] in modules:
                        report.capabilities_seen.add(capability)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _DANGEROUS_CALLS:
                report.reasons.append(
                    f"{rel} calls {func.id}(), which turns data into running "
                    f"code")
                report.capabilities_seen.add("code")
                dangerous = True
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair in _DANGEROUS_ATTRS:
                    report.reasons.append(
                        f"{rel} runs other programs via "
                        f"{pair[0]}.{pair[1]}()")
                    report.capabilities_seen.add("process")
            elif isinstance(func, ast.Name) and func.id == "open":
                for arg in list(node.args[1:]) + [
                        kw.value for kw in node.keywords if kw.arg == "mode"]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and any(m in arg.value for m in ("w", "a", "x", "+")):
                        report.capabilities_seen.add("filesystem")
    return dangerous


def _imported_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def _check_declarations(manifest: Optional[dict],
                        report: InspectionReport) -> None:
    """Compare what it says it needs against what the code reaches for."""
    if manifest is None:
        return
    declared = {str(p).strip().lower()
                for p in (manifest.get("permissions") or [])}
    # filesystem is excluded: almost every package touches pathlib, and
    # treating that as a hidden capability would make the signal useless.
    actual = {c for c in report.capabilities_seen if c != "filesystem"}
    undeclared = actual - declared
    if undeclared:
        report.undeclared = undeclared
        report.reasons.append(
            "the manifest does not declare " + ", ".join(sorted(undeclared))
            + ", but the code uses it — treat the manifest as unreliable")
