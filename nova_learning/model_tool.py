"""nova_learning.model_tool — the `nova_learning` tool NOVA's model calls."""
from __future__ import annotations

import json
import logging

from . import retrieve
from .service import service

log = logging.getLogger("nova.learning")

NAME = "nova_learning"

DECLARATION = {
    "name": NAME,
    "description": (
        "Knowledge the user deliberately teaches you. Use 'learn' ONLY when the user explicitly "
        "asks you to learn or study material (\"learn this folder\", \"study these and learn how I "
        "approach design\"); \"read/open/summarise this file\" is a one-off task for file_processor, "
        "not learning. Learning runs in the background with real progress; keep talking meanwhile. "
        "Commands: 'learn' {path, domain, scope: personal|project|reference, project_id}, "
        "'status' {session_id?}, 'continue' {session_id?}, 'pause' {session_id}, 'list', "
        "'recall' {query, domain?} (what you learned that bears on something, with sources), "
        "'why' {statement, domain?} (where you learned it), 'review' {text, domain} (check work "
        "against learned principles), 'forget' {domain}. If the scope is unclear, ask whether this "
        "is lasting personal knowledge, just for one project, or reference material. Say "
        "\"learned\" only when status says verified; otherwise say what state it is in."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "cmd": {"type": "STRING",
                    "description": "learn | status | continue | pause | list | recall | why | review | forget"},
            "args": {"type": "OBJECT",
                     "description": "learn: {path, domain, scope, project_id}; status/continue/pause: "
                                    "{session_id}; recall: {query, domain}; why: {statement, domain}; "
                                    "review: {text, domain}; forget: {domain}"},
        },
        "required": ["cmd"],
    },
}


def _fmt(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)[:6000]


def _session_view(s: dict) -> dict:
    if not s:
        return {"status": "none", "say": "No learning session yet."}
    v = s.get("verification") or {}
    return {"session_id": s["id"], "domain": s["domain"], "source": s["source"], "scope": s.get("scope"),
            "status": s["status"], "phase": s["phase"], "counts": s.get("counts", {}),
            "failed_files": s.get("failed", {}), "skipped_files": len(s.get("skipped", {})),
            "verification": {"verdict": v.get("verdict"), "passed": v.get("passed"),
                             "total": v.get("total")} if v else None,
            "message": s.get("message", "")}


def _domain_id(svc, name: str) -> str:
    if not name:
        doms = [d for d in svc.store.domains().values() if d.get("status") in ("learned", "unverified")]
        return doms[0]["id"] if len(doms) == 1 else ""
    d = svc.store.find_domain(name)
    return d["id"] if d else ""


def execute(cmd: str, args: dict | None = None, svc=None) -> str:
    a = dict(args or {})
    cmd = (cmd or "list").strip().lower()
    svc = svc or service()
    try:
        if cmd == "learn":
            path = str(a.get("path") or a.get("folder") or "").strip().strip('"')
            domain = str(a.get("domain") or "").strip()
            if not path or not domain:
                return "Error: learn needs {path, domain} — and a scope if the user said which."
            s = svc.learn(path, domain=domain, scope=str(a.get("scope") or "personal"),
                          project_id=str(a.get("project_id") or ""))
            return _fmt({**_session_view(s), "say": (
                "Learning has started in the background. It is NOT learned yet — it is learned only "
                "after the verification step passes. The user can keep talking; report progress with "
                "'status' if asked.")})
        if cmd == "status":
            return _fmt(_session_view(svc.status(str(a.get("session_id") or ""))))
        if cmd in ("continue", "resume"):
            sid = str(a.get("session_id") or "")
            if not sid:
                s = next((x for x in svc.store.sessions(20)
                          if x["status"] in ("paused", "interrupted", "waiting_for_model")), None)
                if not s:
                    return "There is no paused learning session to continue."
                sid = s["id"]
            return _fmt(_session_view(svc.resume(sid)))
        if cmd == "pause":
            return _fmt(_session_view(svc.pause(str(a.get("session_id") or ""))))
        if cmd == "list":
            doms = list(svc.store.domains().values())
            if not doms:
                return "Nothing has been learned yet."
            return _fmt([{"domain": d["name"], "status": d["status"], "scope": d.get("scope"),
                          "items": d.get("item_count", 0), "sources": d.get("sources", []),
                          "verification": {k: (d.get("verification") or {}).get(k)
                                           for k in ("verdict", "passed", "total")}} for d in doms])
        if cmd == "recall":
            hits = retrieve.relevant(str(a.get("query") or ""), store=svc.store,
                                     domain_id=_domain_id(svc, str(a.get("domain") or "")), limit=10)
            if not hits:
                return "Nothing I have learned bears on that."
            return _fmt([{"domain": h["domain"], "verified": h["verified"], "kind": h["item"]["kind"],
                          "statement": h["item"]["statement"], "confidence": h["item"].get("confidence"),
                          "contested": bool(h["item"].get("contested")),
                          "sources": sorted({x["rel"] for x in h["item"]["support"]})} for h in hits])
        if cmd == "why":
            stmt = str(a.get("statement") or a.get("query") or "")
            hits = retrieve.relevant(stmt, store=svc.store,
                                     domain_id=_domain_id(svc, str(a.get("domain") or "")), limit=3)
            if not hits:
                return ("I can't trace that to anything I learned, so I shouldn't claim it came "
                        "from your material.")
            out = []
            for h in hits:
                it = h["item"]
                out.append({"statement": it["statement"], "domain": h["domain"],
                            "evidence": [{"file": x["rel"], "quote": x.get("quote", ""),
                                          "authority": x.get("authority", ""),
                                          "quote_checked": x.get("quote_verified")} for x in it["support"]],
                            "source_count": it.get("source_count", len(it["support"]))})
            return _fmt(out)
        if cmd == "review":
            did = _domain_id(svc, str(a.get("domain") or ""))
            if not did:
                return "Error: say which learned domain to review against."
            text = str(a.get("text") or "")
            ans, _ = retrieve.answer(did, "Review the following work strictly against the learned "
                                          "principles. List each principle it follows, each it breaks "
                                          "(with the source), and what to change.\n\nWORK:\n" + text,
                                     store=svc.store)
            return ans
        if cmd == "forget":
            return ("Forgotten." if svc.forget(str(a.get("domain") or ""))
                    else "There is no learned domain by that name.")
        return f"Error: unknown command {cmd!r}."
    except FileNotFoundError as e:
        return f"Error: {e}"
    except (KeyError, ValueError) as e:
        return f"Error: {e}"
    except Exception as e:
        log.exception("nova_learning %s failed", cmd)
        return f"Error: {type(e).__name__}: {e}"


__all__ = ["DECLARATION", "NAME", "execute"]
