"""What is installed, how far it has been trusted, and how to undo it.

The trust states exist to stop one question being answered by a different
one. "It imported without error" is not "it is safe". "It was safe last week"
is not "this version is safe". So an extension climbs:

    UNTRUSTED -> INSPECTED -> TESTING -> VERIFIED -> ENABLED

and each step has to be earned, with the reason recorded. Nothing may go
straight from INSPECTED to ENABLED, because that is precisely the shortcut a
plausible-looking package would benefit from, and it is the one a tired person
would take at midnight.

Quarantine is reachable from anywhere and leads back to UNTRUSTED, never to
where the extension was before. Something that misbehaved has not kept the
trust it had earned; it starts again.

Installing keeps the version it replaces. A bad upgrade is then a restore
rather than a hunt for wherever the user originally found the thing, and a
failed install leaves the working version untouched -- the failure mode worth
designing for is not a broken extension but a broken extension that took a
working one with it.

Nothing here upgrades anything. A newer version being available is
information, not an instruction: `note_available` records it, `upgradable`
answers the question, and a person decides.
"""
from __future__ import annotations

import enum
import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import nova_paths

log = logging.getLogger("nova.extensions.registry")

__all__ = ["ExtensionRegistry", "InstalledExtension", "TrustState",
           "IllegalTransition", "ExtensionError"]

REGISTER_FILENAME = "extensions.json"


class ExtensionError(Exception):
    """Something about this package is wrong."""


class IllegalTransition(ExtensionError):
    """A trust state that has not been earned."""


class TrustState(str, enum.Enum):
    UNTRUSTED = "untrusted"
    INSPECTED = "inspected"
    TESTING = "testing"
    VERIFIED = "verified"
    ENABLED = "enabled"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


#: One step at a time, in this order. Quarantine is handled separately
#: because it is reachable from anywhere.
_ALLOWED: dict[TrustState, set[TrustState]] = {
    TrustState.UNTRUSTED: {TrustState.INSPECTED},
    TrustState.INSPECTED: {TrustState.TESTING},
    TrustState.TESTING: {TrustState.VERIFIED, TrustState.UNTRUSTED},
    TrustState.VERIFIED: {TrustState.ENABLED, TrustState.DISABLED},
    TrustState.ENABLED: {TrustState.DISABLED},
    TrustState.DISABLED: {TrustState.ENABLED},
    # Deliberately empty: a quarantined extension has to be cleared, which
    # returns it to UNTRUSTED rather than to whatever it was trusted as.
    TrustState.QUARANTINED: set(),
}


@dataclass
class InstalledExtension:
    name: str
    version: str = ""
    state: TrustState = TrustState.UNTRUSTED
    source: str = ""
    installed_at: float = 0.0
    available: str = ""
    pinned: bool = False
    manifest: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["state"] = TrustState(self.state).value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "InstalledExtension":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["state"] = TrustState(known.get("state", TrustState.UNTRUSTED))
        return cls(**known)


class ExtensionRegistry:
    """The register of what is installed and how far it is trusted."""

    def __init__(self, root: Optional[str] = None) -> None:
        self.root = Path(root) if root else nova_paths.data_file("extensions")
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: dict[str, InstalledExtension] = {}
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    @property
    def _register(self) -> Path:
        return self.root / REGISTER_FILENAME

    def _load(self) -> None:
        try:
            if self._register.exists():
                raw = json.loads(self._register.read_text(encoding="utf-8"))
                for item in raw or []:
                    try:
                        ext = InstalledExtension.from_dict(item)
                        self._items[ext.name] = ext
                    except Exception:
                        log.warning("[EXT] dropping unreadable entry",
                                    exc_info=True)
        except Exception:
            log.warning("[EXT] could not read the register", exc_info=True)

    def _save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self._register.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([e.to_dict() for e in self._items.values()], indent=2),
                encoding="utf-8")
            tmp.replace(self._register)
        except Exception:
            log.warning("[EXT] could not write the register", exc_info=True)

    # ── paths ───────────────────────────────────────────────────────────────
    def path_for(self, name: str) -> Path:
        return self.root / name / "current"

    def _version_dir(self, name: str, version: str) -> Path:
        return self.root / name / "versions" / (version or "unversioned")

    def versions(self, name: str) -> list[str]:
        base = self.root / name / "versions"
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    # ── the register ────────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[InstalledExtension]:
        with self._lock:
            return self._items.get(name)

    def all(self) -> list[InstalledExtension]:
        with self._lock:
            return list(self._items.values())

    def add(self, source) -> InstalledExtension:
        """Record a candidate without installing it."""
        manifest = _read_manifest(Path(source))
        name = str(manifest.get("name") or Path(source).name)
        with self._lock:
            ext = InstalledExtension(
                name=name,
                version=str(manifest.get("version", "")),
                source=str(source),
                manifest=manifest,
            )
            self._note(ext, TrustState.UNTRUSTED, "added as a candidate")
            self._items[name] = ext
            self._save()
        return ext

    # ── trust ───────────────────────────────────────────────────────────────
    def advance(self, name: str, state: TrustState,
                reason: str) -> InstalledExtension:
        state = TrustState(state)
        with self._lock:
            ext = self._items.get(name)
            if ext is None:
                raise ExtensionError(f"{name} is not registered")
            allowed = _ALLOWED.get(ext.state, set())
            if state not in allowed:
                raise IllegalTransition(
                    f"{name} is {ext.state.value}; it cannot become "
                    f"{state.value} without passing through "
                    f"{', '.join(sorted(s.value for s in allowed)) or 'nothing'}"
                )
            ext.state = state
            self._note(ext, state, reason)
            self._save()
            return ext

    def quarantine(self, name: str, reason: str) -> bool:
        """Reachable from any state. Misbehaviour does not need permission."""
        with self._lock:
            ext = self._items.get(name)
            if ext is None:
                return False
            ext.state = TrustState.QUARANTINED
            self._note(ext, TrustState.QUARANTINED, reason)
            self._save()
            return True

    def clear_quarantine(self, name: str, reason: str) -> bool:
        """Back to the bottom, not back to where it was."""
        with self._lock:
            ext = self._items.get(name)
            if ext is None or ext.state is not TrustState.QUARANTINED:
                return False
            ext.state = TrustState.UNTRUSTED
            self._note(ext, TrustState.UNTRUSTED,
                       f"quarantine cleared: {reason}; trust starts again")
            self._save()
            return True

    @staticmethod
    def _note(ext: InstalledExtension, state: TrustState, reason: str) -> None:
        ext.history.append({
            "at": time.time(),
            "state": TrustState(state).value,
            "reason": reason,
        })
        del ext.history[:-40]

    # ── installing ──────────────────────────────────────────────────────────
    def install(self, source) -> InstalledExtension:
        """Copy a package in, keeping whatever it replaces.

        The manifest is read *before* anything is moved, so a package that is
        malformed cannot take the working version down with it.
        """
        src = Path(source)
        manifest = _read_manifest(src)          # raises on a bad package
        name = str(manifest.get("name") or src.name)
        version = str(manifest.get("version", "")) or "unversioned"

        with self._lock:
            target = self.path_for(name)
            store = self._version_dir(name, version)

            # Keep what is already there before touching it.
            existing = self._items.get(name)
            if existing is not None and target.exists():
                kept = self._version_dir(name, existing.version or "previous")
                if not kept.exists():
                    shutil.copytree(target, kept)

            store.parent.mkdir(parents=True, exist_ok=True)
            if store.exists():
                shutil.rmtree(store, ignore_errors=True)
            shutil.copytree(src, store)

            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(store, target)

            ext = existing or InstalledExtension(name=name)
            previous = ext.version
            ext.version = version
            ext.source = str(src)
            ext.manifest = manifest
            ext.installed_at = time.time()
            if ext.state in (TrustState.ENABLED, TrustState.VERIFIED):
                # A new version has not inherited the last one's testing.
                ext.state = TrustState.UNTRUSTED
                self._note(ext, TrustState.UNTRUSTED,
                           f"version changed to {version}; trust starts again")
            else:
                self._note(ext, ext.state,
                           f"installed {version}"
                           + (f", replacing {previous}" if previous else ""))
            self._items[name] = ext
            self._save()
            return ext

    def rollback(self, name: str) -> bool:
        """Restore the most recent version that is not the current one."""
        with self._lock:
            ext = self._items.get(name)
            if ext is None:
                return False
            others = [v for v in self.versions(name) if v != ext.version]
            if not others:
                return False
            previous = others[-1]
            source = self._version_dir(name, previous)
            target = self.path_for(name)
            try:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(source, target)
            except Exception as exc:
                log.warning("[EXT] rollback of %s failed: %s", name, exc)
                return False
            ext.version = previous
            ext.state = TrustState.UNTRUSTED
            self._note(ext, TrustState.UNTRUSTED,
                       f"rolled back to {previous}; trust starts again")
            self._save()
            return True

    def uninstall(self, name: str) -> bool:
        with self._lock:
            ext = self._items.pop(name, None)
            self._save()
        if ext is None:
            return False
        shutil.rmtree(self.root / name, ignore_errors=True)
        return True

    # ── versions ────────────────────────────────────────────────────────────
    def note_available(self, name: str, version: str) -> bool:
        """Record that a newer version exists. Does not install it."""
        with self._lock:
            ext = self._items.get(name)
            if ext is None:
                return False
            ext.available = str(version)
            self._save()
            return True

    def upgradable(self, name: str) -> bool:
        ext = self.get(name)
        if ext is None or ext.pinned or not ext.available:
            return False
        return ext.available != ext.version

    def pin(self, name: str) -> bool:
        return self._set_pin(name, True)

    def unpin(self, name: str) -> bool:
        return self._set_pin(name, False)

    def _set_pin(self, name: str, value: bool) -> bool:
        with self._lock:
            ext = self._items.get(name)
            if ext is None:
                return False
            ext.pinned = value
            self._save()
            return True

    # ── narration ───────────────────────────────────────────────────────────
    def describe(self, name: str) -> str:
        ext = self.get(name)
        if ext is None:
            return f"I have nothing installed called {name}."
        bits = [f"{ext.name} {ext.version or '(no version)'}",
                f"state {ext.state.value}"]
        if ext.pinned:
            bits.append("pinned")
        if self.upgradable(name):
            bits.append(f"{ext.available} is available, not installed")
        if ext.state is not TrustState.ENABLED:
            bits.append("not in use")
        return ", ".join(bits) + "."


def _read_manifest(source: Path) -> dict:
    for candidate in ("nova_extension.json", "nova-extension.json"):
        path = source / candidate
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ExtensionError(f"{candidate} is not readable JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ExtensionError(f"{candidate} is not an object")
        return data
    raise ExtensionError(
        "there is no nova_extension.json, so the package makes no claims to "
        "check")
