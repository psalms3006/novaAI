# actions/web_search.py
#
# Fixes applied vs original:
#   [FIX-1] Dead-code second except removed; DDG errors now properly caught
#   [FIX-2] compare mode (_compare / _gemini_search) now actually executes
#   [FIX-3] Nested try/except hierarchy so every failure path is handled
#   [FIX-4] OpenRouter → Gemini grounded search → DDG cascade (3 levels)

import json
import sys
from pathlib import Path



def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


# ─── Backend: Gemini grounded search ─────────────────────────────────────────

def _gemini_search(query: str) -> str:
    from google import genai

    client   = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise ValueError("Gemini returned no candidates.")

    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    for part in parts:
        if getattr(part, "text", None):
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


# ─── Backend: DuckDuckGo ──────────────────────────────────────────────────────

def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    import importlib

    try:
        ddgs_module = importlib.import_module("ddgs")
    except ImportError:
        ddgs_module = importlib.import_module("duckduckgo_search")

    DDGS = getattr(ddgs_module, "DDGS")

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):
            lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ─── Compare mode ─────────────────────────────────────────────────────────────

def _compare(items: list[str], aspect: str) -> str:
    """
    [FIX-2] compare mode now reachable and properly cascades backends.
    """
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )

    # Try Gemini first
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini compare failed: {e} — falling back to DDG")

    # DDG per-item fallback
    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        try:
            results = _ddg_search(f"{item} {aspect}", max_results=3)
            for r in results[:2]:
                if r.get("snippet"):
                    lines.append(f"  • {r['snippet']}")
        except Exception as e:
            lines.append(f"  (search failed: {e})")

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query, sir."

    # [FIX-2] compare mode actually branches now
    if items and mode != "compare":
        mode = "compare"

    if player:
        player.write_log(f"[Search] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 Query: {query!r}  Mode: {mode}")

    # ── Compare mode ──────────────────────────────────────────────────────────
    if mode == "compare":
        try:
            return _compare(items or [query], aspect)
        except Exception as e:
            print(f"[WebSearch] ❌ Compare failed entirely: {e}")
            return f"Comparison failed, sir: {e}"

    # ── Search mode: 3-level cascade ─────────────────────────────────────────
    # Level 1: OpenRouter
    # [FIX-1] Restructured so DDG errors are caught separately, not silently
    try:
        import importlib

        or_client_mod = importlib.import_module("or_client")
        client = getattr(or_client_mod, "client")
        result = client.chat(
            query,
            system="You are a web search assistant. Answer factually and concisely.",
        )
        print("[WebSearch] ✅ OpenRouter OK")
        return result
    except ImportError:
        print("[WebSearch] ℹ️ or_client not installed — skipping OpenRouter")
    except Exception as e:
        print(f"[WebSearch] ⚠️ OpenRouter failed: {e}")

    # Level 2: Gemini grounded search
    try:
        result = _gemini_search(query)
        print("[WebSearch] ✅ Gemini grounded search OK")
        return result
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini search failed: {e}")

    # Level 3: DuckDuckGo
    # [FIX-1] This try/except is now its own block — errors here are caught
    try:
        results = _ddg_search(query)
        result  = _format_ddg(query, results)
        print(f"[WebSearch] ✅ DDG: {len(results)} result(s)")
        return result
    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed, sir: {e}"