"""actions.fetch_url — read one web page, or a video's transcript.

web_search returns snippets; browser_control opens a page for the person to
look at. Neither let NOVA read a specific page the user names ("summarise this
article", "what does this page say about pricing") -- the permission tables
already had a `fetch_url` entry and the trust layer already treated its output
as untrusted, but there was no tool behind them.

Safety:
  * http(s) only, and every hop -- redirects included -- must resolve to a
    public address. NOVA's own bridge listens on 127.0.0.1; a page that
    redirects to http://127.0.0.1:8765/... must not become a way in.
  * a size cap on what is downloaded and on what is returned;
  * the text is data: the caller (chat's trust layer) runs everything after it
    as untrusted.

YouTube links return the transcript (youtube-transcript-api), which is what
"summarise this video" needs.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qs, urljoin, urlparse

MAX_BYTES = 2_000_000
DEFAULT_CHARS = 12_000
MAX_REDIRECTS = 5


class Refused(Exception):
    pass


def _public_host(host: str) -> None:
    if not host:
        raise Refused("the address has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise Refused(f"{host} could not be found")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise Refused(f"{host} points at a local or private address ({ip}), which NOVA does not fetch")


def _youtube_id(url: str) -> str:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host.endswith("youtu.be"):
        return u.path.strip("/").split("/")[0]
    if host.endswith("youtube.com"):
        if u.path == "/watch":
            return (parse_qs(u.query).get("v") or [""])[0]
        m = re.match(r"^/(?:shorts|embed|live)/([\w-]{6,})", u.path)
        if m:
            return m.group(1)
    return ""


def _transcript(video_id: str, max_chars: int) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi
    fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-US", "en-GB"])
    parts, total = [], 0
    for snip in fetched:
        mins, secs = divmod(int(getattr(snip, "start", 0)), 60)
        line = f"[{mins}:{secs:02d}] {getattr(snip, 'text', '').strip()}"
        total += len(line) + 1
        if total > max_chars:
            parts.append("... (transcript truncated)")
            break
        parts.append(line)
    return "\n".join(parts)


def _readable(text: str) -> str:
    """Paragraphs, and the short lines that head them; not menus and language
    lists (runs of short lines with no prose after them)."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    long_ = [len(l) >= 60 for l in lines]
    keep = []
    for i, l in enumerate(lines):
        if long_[i] or any(long_[i + 1:i + 3]):
            keep.append(l)
    out = "\n".join(keep)
    return out if len(out) > 200 else "\n".join(lines)


def fetch(url: str, max_chars: int = DEFAULT_CHARS) -> dict:
    """{'url', 'title', 'text', 'truncated', 'kind'} or raises Refused/IOError."""
    import requests
    from nova_skills.research import html_to_text
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
            url = "https://" + url
        else:
            raise Refused("only web addresses (http or https) can be fetched")
    vid = _youtube_id(url)
    if vid:
        text = _transcript(vid, max_chars)
        return {"url": url, "title": f"YouTube video {vid}", "text": text,
                "truncated": text.endswith("(transcript truncated)"), "kind": "transcript"}
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _public_host(urlparse(current).hostname or "")
        r = requests.get(current, timeout=15, stream=True, allow_redirects=False,
                         headers={"User-Agent": "Mozilla/5.0 (NOVA reader)"})
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            current = urljoin(current, r.headers["Location"])
            continue
        break
    else:
        raise IOError("too many redirects")
    if r.status_code != 200:
        raise IOError(f"the page answered HTTP {r.status_code}")
    raw = r.raw.read(MAX_BYTES, decode_content=True)
    ctype = (r.headers.get("Content-Type") or "").lower()
    body = raw.decode(r.encoding or "utf-8", errors="replace")
    if "html" in ctype or body.lstrip().startswith("<"):
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        # The page's own content, not its menus: "Jump to content / Main menu
        # / Navigation ..." used up the budget on the first real fetch.
        main = (re.search(r"(?is)<main\b[^>]*>(.*)</main>", body)
                or re.search(r"(?is)<article\b[^>]*>(.*)</article>", body))
        text = html_to_text(main.group(1) if main and len(main.group(1)) > 500 else body)
        text = _readable(re.sub(r"[ \t]*\n[ \t\n]*", "\n", text))
    elif "text" in ctype or "json" in ctype:
        title, text = "", body
    else:
        raise IOError(f"that address is not a page or text ({ctype or 'unknown type'})")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return {"url": current, "title": title, "text": text[:max_chars],
            "truncated": len(text) > max_chars, "kind": "page"}


def execute(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "Give me the address of the page or video."
    try:
        max_chars = max(1000, min(40_000, int(args.get("max_chars") or DEFAULT_CHARS)))
    except (TypeError, ValueError):
        max_chars = DEFAULT_CHARS
    try:
        page = fetch(url, max_chars)
    except Refused as e:
        return f"I won't fetch that: {e}."
    except Exception as e:
        return f"I couldn't read {url}: {type(e).__name__}: {str(e)[:200]}"
    head = f"{page['title']} -- {page['url']}" if page["title"] else page["url"]
    note = " (cut short; ask for more if needed)" if page["truncated"] else ""
    label = "Transcript of" if page["kind"] == "transcript" else "Text of"
    return f"{label} {head}{note}:\n\n{page['text']}"


__all__ = ["execute", "fetch", "Refused"]
