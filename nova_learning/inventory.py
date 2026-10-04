"""nova_learning.inventory — what is in the folder, and what can be understood.

Every file gets a kind and a status. Nothing is silently dropped: a file that
cannot be used is kept in the inventory with the reason, so "sources skipped:
11" can always be answered with which, and why.

    document  text NOVA can read (the document library's parsers)
    image     a picture a vision model can look at
    media     audio / video: recorded, not processed in this version (said so)
    other     unsupported
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path

IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
MEDIA_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mov", ".webm", ".mkv", ".avi"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}
MAX_DOC_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FILES = 2000


@dataclass
class SourceFile:
    rel: str
    path: str
    kind: str                     # document | image | media | other
    doc_kind: str = ""            # pdf, markdown, ... for documents
    mime: str = ""
    size: int = 0
    mtime: float = 0.0
    checksum: str = ""
    status: str = "pending"       # pending | skipped | processed | failed
    reason: str = ""
    duplicate_of: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _checksum(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def scan(folder: str) -> list:
    """Every file under `folder`, classified. Raises FileNotFoundError."""
    from nova_core.rag import parsers
    root = Path(folder).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"There is no folder or file at {root}.")
    paths = [root] if root.is_file() else []
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                paths.append(Path(dirpath) / name)
            if len(paths) >= MAX_FILES:
                break
    base = root if root.is_dir() else root.parent
    out, seen = [], {}
    for p in sorted(paths)[:MAX_FILES]:
        rel = str(p.relative_to(base)).replace("\\", "/")
        try:
            st = p.stat()
        except OSError as e:
            out.append(SourceFile(rel, str(p), "other", status="skipped",
                                  reason=f"could not be opened ({type(e).__name__})"))
            continue
        sf = SourceFile(rel, str(p), "other", size=st.st_size, mtime=st.st_mtime)
        ext = p.suffix.lower()
        if p.name.startswith(".") or p.name.startswith("~$"):
            sf.status, sf.reason = "skipped", "hidden or temporary file"
        elif st.st_size == 0:
            sf.status, sf.reason = "skipped", "empty file"
        elif ext in IMAGE_EXT:
            sf.kind, sf.mime = "image", IMAGE_EXT[ext]
            if st.st_size > MAX_IMAGE_BYTES:
                sf.status, sf.reason = "skipped", "image larger than 8 MB"
        elif ext in MEDIA_EXT:
            sf.kind = "media"
            sf.status, sf.reason = "skipped", ("audio and video are not processed yet; "
                                               "add a transcript or notes to learn from them")
        else:
            kind = parsers.detect_kind(p)
            if kind in parsers.SUPPORTED_KINDS and kind != "image":
                sf.kind, sf.doc_kind = "document", kind
                if st.st_size > MAX_DOC_BYTES:
                    sf.status, sf.reason = "skipped", "document larger than 25 MB"
            else:
                sf.status, sf.reason = "skipped", f"unsupported file type ({ext or 'no extension'})"
        if sf.status != "skipped":
            try:
                sf.checksum = _checksum(p)
            except OSError as e:
                sf.status, sf.reason = "skipped", f"could not be read ({type(e).__name__})"
            else:
                if sf.checksum in seen:
                    sf.status, sf.reason = "skipped", "duplicate"
                    sf.duplicate_of = seen[sf.checksum]
                else:
                    seen[sf.checksum] = rel
        out.append(sf)
    return out


def summary(files: list) -> dict:
    by = {}
    for f in files:
        by[f.kind] = by.get(f.kind, 0) + 1
    return {"discovered": len(files), "by_kind": by,
            "usable": sum(1 for f in files if f.status != "skipped"),
            "skipped": sum(1 for f in files if f.status == "skipped")}


__all__ = ["scan", "summary", "SourceFile", "IMAGE_EXT", "MEDIA_EXT"]
