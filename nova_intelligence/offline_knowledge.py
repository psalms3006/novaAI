"""nova_intelligence.offline_knowledge — Offline knowledge base management.

Wraps the existing OfflineWiki (ZIM files) and adds:
- Detection of installed vs available ZIM packages
- Download/install flow with progress
- Status reporting for UI
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# Known ZIM packages that NOVA can use
KNOWN_ZIM_PACKAGES = [
    {
        "id": "wikipedia_en_simple_all_mini",
        "name": "Wikipedia (Simple English, Mini)",
        "description": "Simple English Wikipedia — great for offline Q&A",
        "language": "en",
        "category": "encyclopedia",
        "url": "https://download.kiwix.org/zim/wikipedia_en_simple_all_mini_2026-05.zim",
        "filename": "wikipedia_en_simple_all_mini_2026-05.zim",
        "size_gb": 0.8,
        "recommended": True,
    },
    {
        "id": "wikipedia_en_geography_nopic",
        "name": "Wikipedia Geography (No Pictures)",
        "description": "Geography articles from English Wikipedia",
        "language": "en",
        "category": "geography",
        "url": "https://download.kiwix.org/zim/wikipedia_en_geography_nopic_2026-04.zim",
        "filename": "wikipedia_en_geography_nopic_2026-04.zim",
        "size_gb": 0.3,
        "recommended": False,
    },
]


class OfflineKnowledgeManager:
    """Manages offline knowledge bases (ZIM files).
    
    Detects installed packages, provides available packages for download,
    and handles installation with progress callbacks.
    """

    @staticmethod
    def default_data_path() -> Path:
        """Where downloaded ZIM archives live.

        When frozen this must NOT be the install directory. __file__ points
        inside the PyInstaller bundle, so archives downloaded through the UI
        landed inside the install directory — not writable under Program
        Files, and destroyed by the next uninstall or upgrade. Multi-gigabyte
        user downloads belong with the rest of the user's data.
        """
        import os
        import sys
        if getattr(sys, "frozen", False):
            base = os.getenv("APPDATA")
            root = Path(base) / "NOVA" if base else Path.home() / ".nova"
            return root / "data" / "zim"
        return Path(__file__).parent.parent / "data" / "zim"

    def __init__(self, data_path: str = ""):
        if not data_path:
            data_path = str(self.default_data_path())
        self._data_path = Path(data_path)
        self._data_path.mkdir(parents=True, exist_ok=True)
        self._wiki = None  # Lazy-loaded OfflineWiki
        self._downloading: Dict[str, bool] = {}
        self._progress: Dict[str, float] = {}
        self._progress_callbacks: Dict[str, List[Callable]] = {}

    @property
    def data_path(self) -> Path:
        return self._data_path

    def status(self) -> Dict[str, Any]:
        """Return full status of offline knowledge."""
        installed = self._detect_installed()
        available = self._get_available_packages()
        libzim_ok = self._check_libzim()

        return {
            "libzim_available": libzim_ok,
            "data_path": str(self._data_path),
            "installed_count": len(installed),
            "installed": installed,
            "available_count": len(available),
            "available": available,
            "downloading": dict(self._downloading),
            "progress": dict(self._progress),
        }

    def available_packages(self) -> List[Dict[str, Any]]:
        """List packages available for download (excluding already installed)."""
        installed_names = {p["filename"] for p in self._detect_installed()}
        return [
            {**p, "installed": False}
            for p in KNOWN_ZIM_PACKAGES
            if p["filename"] not in installed_names
        ]

    def installed_packages(self) -> List[Dict[str, Any]]:
        """List currently installed packages."""
        return self._detect_installed()

    def install(
        self,
        package_id: str,
        progress_callback: Optional[Callable[[str, float, str], None]] = None,
    ) -> bool:
        """Download and install a ZIM package.
        
        Args:
            package_id: The package ID (e.g., 'wikipedia_en_simple_all_mini')
            progress_callback: Called with (package_id, progress_pct, status_msg)
            
        Returns True on success, False on failure.
        """
        # Find the package
        pkg = None
        for p in KNOWN_ZIM_PACKAGES:
            if p["id"] == package_id:
                pkg = p
                break
        if pkg is None:
            log.error("[KNOWLEDGE] Unknown package: %s", package_id)
            return False

        # Already installed?
        if self._is_installed(pkg["filename"]):
            log.info("[KNOWLEDGE] Package already installed: %s", package_id)
            return True

        # Already downloading?
        if self._downloading.get(package_id):
            log.info("[KNOWLEDGE] Package already downloading: %s", package_id)
            return False

        self._downloading[package_id] = True
        self._progress[package_id] = 0.0

        def _do_download():
            try:
                self._notify_progress(package_id, 0.0, "Starting download...")
                target = self._data_path / pkg["filename"]

                import requests
                resp = requests.get(pkg["url"], stream=True, timeout=30)
                resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(target, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = (downloaded / total) * 100
                                self._progress[package_id] = pct
                                self._notify_progress(
                                    package_id, pct,
                                    f"Downloaded {downloaded / (1024**2):.0f}MB / {total / (1024**2):.0f}MB"
                                )

                self._progress[package_id] = 100.0
                self._notify_progress(package_id, 100.0, "Download complete")
                log.info("[KNOWLEDGE] Installed package: %s", package_id)

                # Invalidate wiki cache so it picks up the new file
                self._wiki = None

            except Exception as e:
                log.error("[KNOWLEDGE] Download failed for %s: %s", package_id, e)
                self._notify_progress(package_id, -1, f"Download failed: {e}")
                # Clean up partial file
                try:
                    target = self._data_path / pkg["filename"]
                    if target.exists():
                        target.unlink()
                except Exception:
                    pass
            finally:
                self._downloading.pop(package_id, None)

        t = threading.Thread(target=_do_download, daemon=True)
        t.start()
        return True

    def remove(self, package_id: str) -> bool:
        """Remove an installed package."""
        for p in KNOWN_ZIM_PACKAGES:
            if p["id"] == package_id:
                target = self._data_path / p["filename"]
                if target.exists():
                    target.unlink()
                    self._wiki = None
                    log.info("[KNOWLEDGE] Removed package: %s", package_id)
                    return True
        return False

    def initialize(self) -> bool:
        """Initialize the OfflineWiki reader (lazy)."""
        try:
            # Re-import to pick up newly installed files
            import sys
            offline_module = sys.modules.get("offline_extra")
            if offline_module:
                wiki_class = getattr(offline_module, "OfflineWiki", None)
                if wiki_class:
                    self._wiki = wiki_class(data_path=str(self._data_path))
                    return True
        except Exception as e:
            log.warning("[KNOWLEDGE] Failed to initialize OfflineWiki: %s", e)
        return False

    def search(self, query: str, limit: int = 3) -> List[Dict[str, str]]:
        """Search offline knowledge base."""
        if self._wiki is None:
            self.initialize()
        if self._wiki is None:
            return []
        try:
            return self._wiki.search(query, limit=limit)
        except Exception as e:
            log.debug("[KNOWLEDGE] Search error: %s", e)
            return []

    def on_progress(self, package_id: str, callback: Callable):
        """Register a progress callback for a download."""
        self._progress_callbacks.setdefault(package_id, []).append(callback)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _detect_installed(self) -> List[Dict[str, Any]]:
        """Detect ZIM files in the data directory."""
        installed = []
        for zim_file in self._data_path.glob("*.zim"):
            size = zim_file.stat().st_size
            # Match against known packages
            meta = None
            for p in KNOWN_ZIM_PACKAGES:
                if p["filename"] == zim_file.name:
                    meta = p
                    break
            installed.append({
                "id": meta["id"] if meta else zim_file.stem,
                "name": meta["name"] if meta else zim_file.stem,
                "filename": zim_file.name,
                "size_gb": round(size / (1024**3), 2),
                "path": str(zim_file),
                "installed": True,
            })
        return installed

    def _get_available_packages(self) -> List[Dict[str, Any]]:
        """Get all known packages with install status."""
        installed_names = {p["filename"] for p in self._detect_installed()}
        packages = []
        for p in KNOWN_ZIM_PACKAGES:
            packages.append({
                **p,
                "installed": p["filename"] in installed_names,
            })
        return packages

    def _is_installed(self, filename: str) -> bool:
        return (self._data_path / filename).exists()

    def _check_libzim(self) -> bool:
        try:
            import libzim
            return True
        except ImportError:
            return False

    def _notify_progress(self, package_id: str, pct: float, msg: str):
        for cb in self._progress_callbacks.get(package_id, []):
            try:
                cb(package_id, pct, msg)
            except Exception:
                pass
