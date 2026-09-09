"""nova_core.rag.chunking — split documents where meaning splits.

Fixed-size chunking is the default everywhere and it is the reason so many RAG
systems retrieve the right document and the wrong paragraph. A chunk cut
mid-sentence loses the subject; a chunk that merges two sections answers
neither question well.

So this splits on the strongest boundary available and only falls back:

    segment (page, slide, section)  ->  paragraph  ->  sentence  ->  words

Every chunk keeps the locator its segment came from, because a chunk that
cannot say "page 4" cannot be cited, and an answer that cannot be traced back
to a source is the thing NOVA is supposed to avoid.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parsers import Segment

#: Target chunk size in characters. Roughly 200-250 tokens -- large enough to
#: carry an argument, small enough that retrieval is precise.
TARGET_CHARS = 900
MAX_CHARS = 1400
MIN_CHARS = 120

#: Carried from the end of one chunk into the start of the next, so a fact
#: stated at a boundary is retrievable from both sides.
OVERLAP_CHARS = 140

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")
_PARAGRAPH = re.compile(r"\n\s*\n")


@dataclass
class Chunk:
    text: str
    locator: str = ""
    ordinal: int = 0
    kind: str = "text"
    heading: str = ""          # nearest heading above this chunk, if any
    token_estimate: int = 0

    def __post_init__(self):
        if not self.token_estimate:
            # Good enough for budgeting; exact counts need the model's own
            # tokenizer and are not worth a dependency here.
            self.token_estimate = max(1, len(self.text) // 4)

    @property
    def citation(self) -> str:
        if self.heading and self.locator:
            return f"{self.locator} - {self.heading}"
        return self.locator or self.heading


def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_END.split(text)
    return [p.strip() for p in parts if p.strip()]


def _hard_split(text: str, limit: int) -> list[str]:
    """Last resort: a single run of text with no sentence boundaries at all
    (minified JSON, a table dump). Split on whitespace so words survive."""
    words, out, cur, size = text.split(), [], [], 0
    for w in words:
        if size + len(w) + 1 > limit and cur:
            out.append(" ".join(cur))
            cur, size = [], 0
        cur.append(w)
        size += len(w) + 1
    if cur:
        out.append(" ".join(cur))
    return out or [text[:limit]]


def _pack(pieces: list[str], target: int, maximum: int) -> list[str]:
    """Greedily fill chunks up to target, never exceeding maximum."""
    chunks, cur, size = [], [], 0
    for piece in pieces:
        plen = len(piece)
        if plen > maximum:
            if cur:
                chunks.append("\n\n".join(cur))
                cur, size = [], 0
            chunks.extend(_hard_split(piece, target))
            continue
        if size and size + plen + 2 > target:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(piece)
        size += plen + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _with_overlap(chunks: list[str], overlap: int) -> list[str]:
    if overlap <= 0 or len(chunks) < 2:
        return chunks
    out = [chunks[0]]
    for prev, cur in zip(chunks, chunks[1:]):
        tail = prev[-overlap:]
        # Start the carried context at a word boundary, so the overlap does
        # not begin mid-word and pollute the tokens.
        space = tail.find(" ")
        if space > 0:
            tail = tail[space + 1:]
        out.append(f"{tail}\n{cur}" if tail.strip() else cur)
    return out


def _heading_of(segment: Segment) -> str:
    loc = segment.locator or ""
    for prefix in ("section: ", "heading: "):
        if loc.startswith(prefix):
            return loc[len(prefix):]
    return ""


def chunk_segments(segments: list[Segment], *, target: int = TARGET_CHARS,
                   maximum: int = MAX_CHARS, minimum: int = MIN_CHARS,
                   overlap: int = OVERLAP_CHARS) -> list[Chunk]:
    """Chunk a parsed document, preserving locators and headings."""
    chunks: list[Chunk] = []
    ordinal = 0
    pending: Chunk | None = None

    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue

        heading = _heading_of(segment)

        # A heading on its own is context for what follows, not a chunk.
        if segment.kind == "heading" and len(text) < minimum:
            continue

        # Tables are split on rows, never mid-row: half a row is noise.
        if segment.kind == "table":
            pieces = [ln for ln in text.split("\n") if ln.strip()]
            packed = _pack(pieces, target, maximum) if pieces else []
        else:
            paragraphs = [p.strip() for p in _PARAGRAPH.split(text) if p.strip()]
            pieces = []
            for para in paragraphs:
                if len(para) <= maximum:
                    pieces.append(para)
                else:
                    pieces.extend(_split_sentences(para) or [para])
            packed = _pack(pieces, target, maximum)

        # Overlap carries a tail from the previous chunk into the next, which
        # is right for prose and wrong for a table: the tail lands mid-row and
        # turns a clean record into noise. Rows are already self-contained.
        if segment.kind != "table":
            packed = _with_overlap(packed, overlap)

        for body in packed:
            body = body.strip()
            if not body:
                continue
            # Merge a runt into the previous chunk from the same place rather
            # than indexing a fragment that can never win a retrieval.
            if (len(body) < minimum and pending is not None
                    and pending.locator == segment.locator
                    and len(pending.text) + len(body) <= maximum):
                pending.text = f"{pending.text}\n{body}"
                pending.token_estimate = max(1, len(pending.text) // 4)
                continue
            ordinal += 1
            pending = Chunk(text=body, locator=segment.locator,
                            ordinal=ordinal, kind=segment.kind,
                            heading=heading)
            chunks.append(pending)

    # A final orphan fragment with nothing to merge into is still worth
    # keeping if it is the whole document.
    return [c for c in chunks if c.text.strip()]


def chunk_text(text: str, locator: str = "", **kw) -> list[Chunk]:
    return chunk_segments([Segment(text=text, locator=locator)], **kw)


__all__ = ["Chunk", "chunk_segments", "chunk_text", "TARGET_CHARS",
           "MAX_CHARS", "MIN_CHARS", "OVERLAP_CHARS"]
