"""Block parser for context/self/nova.md.

The doc mixes hand-written prose with AUTO blocks the generators refresh.
An AUTO block is delimited by HTML-comment markers naming it:

    <!-- AUTO-START: capabilities -->
    (regenerated content)
    <!-- AUTO-END: capabilities -->

parse() splits the document into an ordered list of Block objects.
render() reassembles them. The load-bearing property is round-trip
stability: render(parse(text)) == text for any text, whether or not any
AUTO block's content is replaced first -- editing hand-written prose
between two AUTO blocks must never touch the blocks themselves, and
regenerating an AUTO block must never touch a single character outside
its own markers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_START_RE = re.compile(r"^<!-- AUTO-START: (?P<name>[a-zA-Z0-9_-]+) -->$")
_END_RE = re.compile(r"^<!-- AUTO-END: (?P<name>[a-zA-Z0-9_-]+) -->$")


class MalformedDocError(ValueError):
    """The doc's AUTO markers don't nest/close the way they must."""


@dataclass
class Block:
    kind: str  # "prose" or "auto"
    text: str  # for "prose": the literal text. for "auto": the CURRENT
               # inner content (between the markers, no markers, no
               # leading/trailing newline of its own -- see render()).
    name: str | None = None  # set only for "auto"


def _split_lines_keep_ending(text: str) -> tuple[list[str], str]:
    """Split into lines without the line terminator, and report which
    terminator the file actually uses (CRLF or LF), so render() can put
    the same one back. Mixed endings collapse to the first one seen --
    a doc mixing both is already inconsistent, and a parser's job is not
    to guess which parts of that inconsistency were intentional."""
    if "\r\n" in text:
        ending = "\r\n"
    elif "\n" in text:
        ending = "\n"
    else:
        ending = "\n"  # no newline at all yet; doesn't matter, nothing to join
    normalized = text.replace("\r\n", "\n")
    trailing_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if trailing_newline:
        lines = lines[:-1]
    return lines, ending


def parse(text: str) -> list[Block]:
    """Parse *text* into an ordered list of prose and auto blocks."""
    lines, _ending = _split_lines_keep_ending(text)
    blocks: list[Block] = []
    prose_buf: list[str] = []
    i = 0
    n = len(lines)
    open_stack: list[str] = []

    def flush_prose():
        if prose_buf:
            blocks.append(Block(kind="prose", text="\n".join(prose_buf)))
            prose_buf.clear()

    while i < n:
        line = lines[i]
        m_start = _START_RE.match(line.strip())
        m_end = _END_RE.match(line.strip())
        if m_start:
            name = m_start.group("name")
            if open_stack:
                raise MalformedDocError(
                    f"AUTO-START: {name} opened while AUTO-START: "
                    f"{open_stack[-1]} is still open -- blocks cannot nest")
            open_stack.append(name)
            flush_prose()
            inner: list[str] = []
            i += 1
            closed = False
            while i < n:
                inner_line = lines[i]
                m_inner_end = _END_RE.match(inner_line.strip())
                if m_inner_end:
                    if m_inner_end.group("name") != name:
                        raise MalformedDocError(
                            f"AUTO-END: {m_inner_end.group('name')} does not "
                            f"match the open AUTO-START: {name}")
                    closed = True
                    open_stack.pop()
                    break
                inner.append(inner_line)
                i += 1
            if not closed:
                raise MalformedDocError(
                    f"AUTO-START: {name} was never closed with a matching "
                    f"AUTO-END")
            blocks.append(Block(kind="auto", name=name, text="\n".join(inner)))
            i += 1
            continue
        if m_end:
            raise MalformedDocError(
                f"AUTO-END: {m_end.group('name')} with no matching "
                f"AUTO-START")
        prose_buf.append(line)
        i += 1

    flush_prose()
    return blocks


def render(blocks: list[Block], overrides: dict[str, str] | None = None,
           line_ending: str = "\n") -> str:
    """Reassemble *blocks* into text.

    *overrides* maps an AUTO block's name to new inner content; a block
    whose name is not in overrides keeps its existing content untouched.
    Hand-written prose blocks are never affected by overrides.
    """
    overrides = overrides or {}
    parts: list[str] = []
    for block in blocks:
        if block.kind == "prose":
            parts.append(block.text)
        else:
            content = overrides.get(block.name, block.text)
            # "" means zero inner lines (START immediately followed by
            # END), not one empty line -- content.split("\n") on "" gives
            # [""], which would insert a blank line that was not there.
            inner_lines = content.split("\n") if content else []
            parts.append("\n".join(
                [f"<!-- AUTO-START: {block.name} -->"]
                + inner_lines
                + [f"<!-- AUTO-END: {block.name} -->"]
            ))
    joined = "\n".join(parts)
    if not joined.endswith("\n"):
        joined += "\n"
    if line_ending != "\n":
        joined = joined.replace("\n", line_ending)
    return joined


def detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def block_names(blocks: list[Block]) -> list[str]:
    return [b.name for b in blocks if b.kind == "auto" and b.name]


def get_auto_content(blocks: list[Block], name: str) -> str | None:
    for b in blocks:
        if b.kind == "auto" and b.name == name:
            return b.text
    return None
