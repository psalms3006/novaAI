"""nova_core.rag.parsers — turn a file into text that still knows where it came from.

The design constraint that shapes this module: **a chunk must be able to cite
its source location**. "According to your proposal, the deadline is October 17"
is only useful if NOVA can say *page 4*. So parsers do not return a string;
they return `Segment`s carrying a human-meaningful locator -- page 4, sheet
"Q3", slide 7, line 120.

Every parser is optional. A missing library degrades that one format to
"cannot read" with an honest reason, rather than taking the ingestion pipeline
down or silently indexing an empty document.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("nova.rag.parsers")

#: Hard ceiling per file. A 400 MB log is not a document, and letting one in
#: would block the ingest worker for minutes.
MAX_BYTES = 64 * 1024 * 1024
MAX_CHARS = 4_000_000


class ParseError(Exception):
    """Could not read the file. The message is shown to the user, so it says
    what to do rather than which exception fired."""


@dataclass
class Segment:
    """A piece of a document plus where in the document it was."""

    text: str
    locator: str = ""          # "page 4", "slide 7", "sheet Q3", "lines 1-40"
    ordinal: int = 0           # position within the document, for ordering
    kind: str = "text"         # text | table | heading | code | caption


@dataclass
class ParsedDocument:
    segments: list[Segment] = field(default_factory=list)
    title: str = ""
    page_count: int = 0
    parser: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.segments if s.text.strip())

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)


# -- file type detection -----------------------------------------------------

#: Extension -> logical kind. Content sniffing supplements this, because
#: extensions lie and a mislabelled file should not be parsed as the wrong
#: type.
_EXT_KIND = {
    ".pdf": "pdf",
    ".docx": "docx", ".doc": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".pptx": "pptx",
    ".csv": "csv", ".tsv": "csv",
    ".json": "json", ".jsonl": "json",
    ".md": "markdown", ".markdown": "markdown",
    ".txt": "text", ".log": "text", ".rst": "text",
    ".html": "html", ".htm": "html",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".bmp": "image", ".webp": "image", ".tif": "image", ".tiff": "image",
    ".py": "code", ".js": "code", ".ts": "code", ".java": "code",
    ".c": "code", ".h": "code", ".cpp": "code", ".cs": "code",
    ".go": "code", ".rs": "code", ".rb": "code", ".php": "code",
    ".sh": "code", ".sql": "code", ".yaml": "code", ".yml": "code",
    ".toml": "code", ".ini": "code", ".xml": "code",
}

_MAGIC = [
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n", "image"),
    (b"\xff\xd8\xff", "image"),
    (b"GIF87a", "image"), (b"GIF89a", "image"),
    (b"BM", "image"),
]


def detect_kind(path: str | Path, data: bytes | None = None) -> str:
    """Logical kind of a file, preferring content over extension."""
    p = Path(path)
    head = data[:16] if data else b""
    if not head:
        try:
            with open(p, "rb") as fh:
                head = fh.read(16)
        except OSError:
            head = b""

    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    # Office formats and ZIPs share PK; the extension disambiguates.
    if head.startswith(b"PK\x03\x04"):
        return _EXT_KIND.get(p.suffix.lower(), "archive")

    ext = _EXT_KIND.get(p.suffix.lower())
    if ext:
        return ext

    guessed, _ = mimetypes.guess_type(str(p))
    if guessed:
        if guessed.startswith("image/"):
            return "image"
        if guessed.startswith("text/"):
            return "text"
        if guessed.startswith("audio/"):
            return "audio"
        if guessed.startswith("video/"):
            return "video"
    return "unknown"


# -- individual parsers ------------------------------------------------------

def _require(module: str, install: str, fmt: str):
    try:
        return __import__(module)
    except ImportError:
        raise ParseError(
            f"NOVA cannot read {fmt} files on this machine yet. "
            f"Install it with:  pip install {install}")


def parse_pdf(path: Path) -> ParsedDocument:
    _require("pypdf", "pypdf", "PDF")
    from pypdf import PdfReader

    doc = ParsedDocument(parser="pypdf")
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise ParseError(f"This PDF could not be opened ({type(e).__name__}). "
                         "It may be corrupt or password protected.")

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            raise ParseError("This PDF is password protected, so NOVA cannot "
                             "read it.")

    meta = getattr(reader, "metadata", None)
    if meta and getattr(meta, "title", None):
        doc.title = _useful_title(str(meta.title))
    doc.page_count = len(reader.pages)

    empty = 0
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if not text.strip():
            empty += 1
            continue
        doc.segments.append(Segment(text=_tidy(text), locator=f"page {i}",
                                    ordinal=i))
    if empty and empty == doc.page_count:
        # Almost always a scan. Say so, because "0 results" would otherwise
        # look like NOVA forgot the document.
        doc.warnings.append(
            "No selectable text found; this looks like a scanned PDF. "
            "OCR is needed to read it.")
    elif empty:
        doc.warnings.append(f"{empty} of {doc.page_count} pages had no text.")
    return doc


def parse_docx(path: Path) -> ParsedDocument:
    _require("docx", "python-docx", "Word")
    import docx

    doc = ParsedDocument(parser="python-docx")
    try:
        d = docx.Document(str(path))
    except Exception as e:
        raise ParseError(f"This Word document could not be opened "
                         f"({type(e).__name__}).")

    try:
        core = d.core_properties
        if core.title:
            doc.title = str(core.title).strip()
    except Exception:
        pass

    ordinal = 0
    buf: list[str] = []
    section = "start"
    for para in d.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style = (para.style.name if para.style is not None else "") or ""
        if style.lower().startswith("heading"):
            if buf:
                ordinal += 1
                doc.segments.append(Segment("\n".join(buf), f"section: {section}",
                                            ordinal))
                buf = []
            section = text[:80]
            ordinal += 1
            doc.segments.append(Segment(text, f"heading: {text[:60]}", ordinal,
                                        kind="heading"))
            continue
        buf.append(text)
    if buf:
        ordinal += 1
        doc.segments.append(Segment("\n".join(buf), f"section: {section}", ordinal))

    for n, table in enumerate(d.tables, start=1):
        rows = []
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            ordinal += 1
            doc.segments.append(Segment("\n".join(rows), f"table {n}", ordinal,
                                        kind="table"))
    return doc


def parse_xlsx(path: Path) -> ParsedDocument:
    _require("openpyxl", "openpyxl", "Excel")
    import openpyxl

    doc = ParsedDocument(parser="openpyxl")
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        raise ParseError(f"This spreadsheet could not be opened "
                         f"({type(e).__name__}).")

    ordinal = 0
    for sheet in wb.worksheets:
        rows: list[str] = []
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                rows.append(" | ".join(cells).rstrip(" |"))
            if len(rows) >= 5000:        # a sheet, not a database dump
                rows.append("... (truncated)")
                break
        if rows:
            ordinal += 1
            doc.segments.append(Segment("\n".join(rows), f"sheet: {sheet.title}",
                                        ordinal, kind="table"))
    try:
        wb.close()
    except Exception:
        pass
    return doc


def parse_pptx(path: Path) -> ParsedDocument:
    _require("pptx", "python-pptx", "PowerPoint")
    from pptx import Presentation

    doc = ParsedDocument(parser="python-pptx")
    try:
        pres = Presentation(str(path))
    except Exception as e:
        raise ParseError(f"This presentation could not be opened "
                         f"({type(e).__name__}).")

    for i, slide in enumerate(pres.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                t = (shape.text_frame.text or "").strip()
                if t:
                    parts.append(t)
        try:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"[speaker notes] {notes}")
        except Exception:
            pass
        if parts:
            doc.segments.append(Segment("\n".join(parts), f"slide {i}", i))
    doc.page_count = len(pres.slides)
    return doc


def parse_csv(path: Path) -> ParsedDocument:
    doc = ParsedDocument(parser="csv")
    raw = _read_text(path)
    try:
        dialect = csv.Sniffer().sniff(raw[:4096])
    except Exception:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(raw), dialect)
    rows = []
    for n, row in enumerate(reader, start=1):
        if any((c or "").strip() for c in row):
            rows.append(" | ".join(c.strip() for c in row))
        if n > 20000:
            rows.append("... (truncated)")
            break
    # Keep the header with every block, or a row 400 lines down loses meaning.
    header = rows[0] if rows else ""
    block, ordinal = [], 0
    for i, row in enumerate(rows):
        block.append(row)
        if len(block) >= 200:
            ordinal += 1
            body = "\n".join(block)
            if ordinal > 1 and header:
                body = header + "\n" + body
            doc.segments.append(Segment(body, f"rows {i - len(block) + 2}-{i + 1}",
                                        ordinal, kind="table"))
            block = []
    if block:
        ordinal += 1
        body = "\n".join(block)
        if ordinal > 1 and header:
            body = header + "\n" + body
        doc.segments.append(Segment(body, f"rows {len(rows) - len(block) + 1}-"
                                    f"{len(rows)}", ordinal, kind="table"))
    return doc


def parse_json(path: Path) -> ParsedDocument:
    doc = ParsedDocument(parser="json")
    raw = _read_text(path)
    try:
        obj = json.loads(raw)
        pretty = json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        pretty = raw            # JSONL or malformed: index it as text anyway
        doc.warnings.append("Not valid JSON; indexed as plain text.")
    for ordinal, block in enumerate(_split_lines(pretty, 200), start=1):
        doc.segments.append(Segment(block[0], block[1], ordinal, kind="code"))
    return doc


def parse_markdown(path: Path) -> ParsedDocument:
    """Split on headings, because that is what a Markdown author meant."""
    doc = ParsedDocument(parser="markdown")
    raw = _read_text(path)
    lines = raw.splitlines()

    first_h1 = next((l for l in lines if l.startswith("# ")), "")
    if first_h1:
        doc.title = first_h1[2:].strip()

    current, heading, ordinal, start = [], "(top)", 0, 1
    for i, line in enumerate(lines, start=1):
        if re.match(r"^#{1,6}\s+\S", line):
            if any(l.strip() for l in current):
                ordinal += 1
                doc.segments.append(Segment("\n".join(current).strip(),
                                            f"section: {heading}", ordinal))
            heading = line.lstrip("#").strip()[:80]
            current, start = [line], i
            continue
        current.append(line)
    if any(l.strip() for l in current):
        ordinal += 1
        doc.segments.append(Segment("\n".join(current).strip(),
                                    f"section: {heading}", ordinal))
    return doc


def parse_html(path: Path) -> ParsedDocument:
    doc = ParsedDocument(parser="html")
    raw = _read_text(path)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        if soup.title and soup.title.string:
            doc.title = soup.title.string.strip()
        text = soup.get_text("\n")
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", raw)
        doc.warnings.append("Parsed without BeautifulSoup; markup may remain.")
    for ordinal, (block, locator) in enumerate(_split_lines(_tidy(text), 120),
                                               start=1):
        doc.segments.append(Segment(block, locator, ordinal))
    return doc


def parse_text(path: Path, kind: str = "text") -> ParsedDocument:
    doc = ParsedDocument(parser=kind)
    raw = _read_text(path)
    per = 120 if kind == "text" else 80      # code blocks want to stay smaller
    for ordinal, (block, locator) in enumerate(_split_lines(raw, per), start=1):
        doc.segments.append(Segment(block, locator, ordinal,
                                    kind="code" if kind == "code" else "text"))
    return doc


def parse_image(path: Path) -> ParsedDocument:
    """Images carry no text until something reads them.

    OCR and captioning are the vision pipeline's job, not the parser's -- they
    need a model. This returns an empty document with a clear reason so the
    ingest layer can route it to vision instead of indexing nothing and
    calling it success.
    """
    doc = ParsedDocument(parser="image")
    doc.warnings.append("needs_vision")
    return doc


_PARSERS = {
    "pdf": parse_pdf,
    "docx": parse_docx,
    "xlsx": parse_xlsx,
    "pptx": parse_pptx,
    "csv": parse_csv,
    "json": parse_json,
    "markdown": parse_markdown,
    "html": parse_html,
    "text": parse_text,
    "code": lambda p: parse_text(p, "code"),
    "image": parse_image,
}

SUPPORTED_KINDS = frozenset(_PARSERS)


def parse(path: str | Path) -> ParsedDocument:
    """Parse a file into located segments, or raise ParseError with a reason
    the user can act on."""
    p = Path(path)
    if not p.exists():
        raise ParseError(f"There is no file at {p}.")
    if not p.is_file():
        raise ParseError(f"{p} is not a file.")

    size = p.stat().st_size
    if size == 0:
        raise ParseError(f"{p.name} is empty.")
    if size > MAX_BYTES:
        raise ParseError(
            f"{p.name} is {size / 1048576:.0f} MB, over NOVA's "
            f"{MAX_BYTES // 1048576} MB limit for a single document.")

    kind = detect_kind(p)
    parser = _PARSERS.get(kind)
    if parser is None:
        raise ParseError(
            f"NOVA does not know how to read {p.suffix or 'this kind of'} "
            f"files yet ({kind}).")

    doc = parser(p)
    if not doc.title:
        doc.title = p.stem.replace("_", " ").replace("-", " ").strip()
    if doc.char_count > MAX_CHARS:
        kept, total = [], 0
        for seg in doc.segments:
            if total + len(seg.text) > MAX_CHARS:
                break
            kept.append(seg)
            total += len(seg.text)
        doc.segments = kept
        doc.warnings.append(
            f"Only the first {total:,} characters were indexed.")
    return doc


# -- helpers -----------------------------------------------------------------

def _read_text(path: Path) -> str:
    """Decode without guessing wrongly and without throwing."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


#: Titles writers leave in PDF metadata by default. Trusting them gives every
#: document from the same tool the same useless name.
_JUNK_TITLES = frozenset({
    "untitled", "unnamed", "document", "microsoft word", "document1",
    "presentation1", "workbook1", "slide 1", "new document", "-", "",
})


def _useful_title(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned.lower() in _JUNK_TITLES or len(cleaned) < 3:
        return ""
    return cleaned


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_lines(text: str, per_block: int) -> list[tuple[str, str]]:
    """Line-numbered blocks, so a plain text file can still cite a location."""
    lines = text.splitlines() or [text]
    out = []
    for start in range(0, len(lines), per_block):
        block = lines[start:start + per_block]
        if not any(l.strip() for l in block):
            continue
        out.append(("\n".join(block),
                    f"lines {start + 1}-{start + len(block)}"))
    return out


__all__ = ["parse", "detect_kind", "ParsedDocument", "Segment", "ParseError",
           "SUPPORTED_KINDS", "MAX_BYTES", "MAX_CHARS"]
