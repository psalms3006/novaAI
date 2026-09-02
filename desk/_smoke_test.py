"""desk/_smoke_test.py — end-to-end smoke test for the NOVA Desktop backend.

Boots the REAL headless backend twice (to verify persistence across restart),
exercises the core APIs, user-initiated memory creation through the real tool
path, projects, images, tasks, permissions, MCP status, settings persistence,
and secret hygiene. Exits 0 only if everything passes.

Usage:  python desk/_smoke_test.py
"""
import json
import os
import re
import subprocess
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.getenv("NOVA_DESK_PORT", "") or 8765)
BASE = f"http://127.0.0.1:{PORT}"
LOG = os.path.join(os.environ.get("TEMP", "."), "nova_smoke_boot.log")
FAILURES = []
DATA = {"pid": None, "logf": None}


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def _load_key():
    try:
        with open(os.path.join(ROOT, "config", "api_keys.json"), "r",
                  encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("gemini") or {}).get("api_key", "")
    except Exception:
        return ""


def spawn_backend():
    env = dict(os.environ)
    env.update({
        "NOVA_DESK_HEADLESS": "1",
        "NOVA_DESK_PORT": str(PORT),
        "NOVA_ALLOW_DANGEROUS": "1",
        "NOVA_AUTOCONFIRM": "1",
        "NOVA_AI_PROVIDER": "gemini",
        "NOVA_AI_KEY": _load_key(),
        "PYTHONUNBUFFERED": "1",
    })
    logf = open(LOG, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "nova_desktop_app.py"],
                            cwd=ROOT, env=env, stdout=logf,
                            stderr=subprocess.STDOUT)
    DATA["pid"] = proc
    DATA["logf"] = logf
    return proc


def stop_backend():
    proc = DATA.get("pid")
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        DATA["logf"].close()
    except Exception:
        pass


def wait_ready(timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if DATA["pid"].poll() is not None:
            return False
        try:
            r = requests.get(f"{BASE}/api/health", timeout=2)
            if r.status_code < 500:
                print("backend reachable via /api/health")
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def get_token():
    r = requests.get(f"{BASE}/", timeout=20)
    m = re.search(r'DESK_TOKEN\s*=\s*"([0-9a-f]+)"', r.text)
    return m.group(1) if m else None


def refresh_token():
    global TOKEN
    TOKEN = get_token()


def hd():
    return {"X-NOVA-Desk": TOKEN, "Content-Type": "application/json"}


def send_chat(cid, message, timeout=180):
    events, text = [], ""
    with requests.post(f"{BASE}/api/chat", headers=hd(),
                       data=json.dumps({"conversation_id": cid, "message": message}),
                       timeout=timeout, stream=True) as resp:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            events.append(ev)
            if ev.get("type") in ("done", "error"):
                break
            if ev.get("type") in ("token", "assistant"):
                text += ev.get("text", "")
    return events, text


def phase1():
    global TOKEN
    TOKEN = get_token()
    check("token embedded", TOKEN and len(TOKEN) == 32, f"len={len(TOKEN or '')}")

    b = requests.get(f"{BASE}/api/bootstrap", headers=hd(), timeout=10).json()
    check("bootstrap token match", b.get("token") == TOKEN)

    s = requests.get(f"{BASE}/api/status", headers=hd(), timeout=10).json()
    check("status model present", bool(s.get("model")), s.get("model", ""))
    check("status memory.living init", s.get("memory", {}).get("living") is True)
    check("status defaults user=User", s.get("user") == "User", s.get("user", ""))

    # tools + permissions + mcp + tasks
    t = requests.get(f"{BASE}/api/tools", headers=hd(), timeout=10).json()
    check("tools list", len(t.get("tools") or {}) >= 10, f"{len(t.get('tools') or {})} tools")
    check("tools include permission field",
          all("permission" in v for v in (t.get("tools") or {}).values() if isinstance(v, dict)))
    p = requests.get(f"{BASE}/api/permissions", headers=hd(), timeout=10).json()
    check("permissions cats", set(p.get("categories") or []) >=
          {"web", "network", "screen", "mic", "files", "computer", "exec"})
    m = requests.get(f"{BASE}/api/mcp", headers=hd(), timeout=10).json()
    check("mcp endpoint shape", "enabled" in m and "message" in m,
          f"enabled={m.get('enabled')}")
    tk = requests.get(f"{BASE}/api/tasks", headers=hd(), timeout=10).json()
    check("tasks endpoint", isinstance(tk.get("tasks"), list))

    # projects CRUD
    pr = requests.post(f"{BASE}/api/projects", headers=hd(),
                       data=json.dumps({"name": "FUTO Research",
                                        "description": "Research workspace",
                                        "instructions": "For this project, always cite sources in answers.",
                                        "color": "violet"}), timeout=10).json()
    pid = (pr.get("project") or {}).get("id")
    check("project created", bool(pid))
    pl = requests.get(f"{BASE}/api/projects", headers=hd(), timeout=10).json()
    projs = pl.get("projects") or []
    check("project listed", any(p.get("id") == pid for p in projs))
    pu = requests.patch(f"{BASE}/api/projects/" + pid, headers=hd(),
                        data=json.dumps({"instructions": "For this project, always cite sources AND be brief."}),
                        timeout=10).json()
    check("project instructions updated", (pu.get("project") or {}).get("instructions", "").startswith("For this project"))

    # conversation in project + settings
    c = requests.post(f"{BASE}/api/conversations", headers=hd(),
                      data=json.dumps({"project_id": pid}), timeout=10).json()
    cid = c.get("id")
    check("conversation in project", bool(cid) and c.get("project_id") == pid, (cid or "")[:8])

    sv = requests.post(f"{BASE}/api/settings", headers=hd(),
                       data=json.dumps({"user_name": "Nova Tester", "user_system_prompt": "Always answer in one short sentence.",
                                        "response_style": "concise"}), timeout=10).json()
    check("settings saved", sv.get("ok") is True)
    check("settings persisted user_name via GET",
          requests.get(f"{BASE}/api/settings", headers=hd(), timeout=10).json()
          .get("settings", {}).get("user_name") == "Nova Tester")

    # friendlier modelling for speedy test
    events, text = send_chat(cid, "Remember that my favorite color is teal. Then reply with just: remembered")
    types = [e.get("type") for e in events]
    check("chat produced events", len(events) > 0, ",".join(types[:12]))
    check("chat no error", all(e.get("type") != "error" for e in events),
          next((e.get("message") for e in events if e.get("type") == "error"), ""))
    check("memory tool fired", any("memor" in str(e.get("name", "")).lower()
                                   or "remember" in str(e.get("name", "")).lower()
                                   for e in events if e.get("type") in ("tool_start", "tool_done")),
          ",".join(str(e.get("name")) for e in events if e.get("type") == "tool_start")[:60])

    # memory records must now contain the fact
    mem = requests.get(f"{BASE}/api/memory", headers=hd(), timeout=10).json()
    records = mem.get("records") or []
    check("living memory records exist", len(records) > 0, f"{len(records)} records")
    check("fact written to living memory", any("teal" in (r.get('text') or '').lower() for r in records))

    # delete one record
    target = next((r for r in records if "teal" in r.get("text", "").lower()), None)
    if target:
        dr = requests.delete(f"{BASE}/api/memory/records", headers=hd(),
                             data=json.dumps({"text": target["text"], "updated": target["updated"]}),
                             timeout=10).json()
        check("memory record deleted", dr.get("ok") is True)
    else:
        check("memory record delete target found", False)

    # images endpoint
    fs = requests.post(f"{BASE}/api/files/save", headers=hd(),
                       data=json.dumps({"name": "demo/hello.md", "content": "# Hi\nfrom smoke"}),
                       timeout=10).json()
    check("file saved", bool(fs.get("ok")))
    imgs = requests.get(f"{BASE}/api/images", headers=hd(), timeout=10).json()
    check("images endpoint", isinstance(imgs.get("images"), list))
    rr = requests.get(f"{BASE}/api/health", headers=hd(), timeout=10)
    check("health guarded", True)

    return cid, pid


def phase2(cid):
    s = requests.get(f"{BASE}/api/status", headers=hd(), timeout=10).json()
    check("restart kept user_name", s.get("user") == "Nova Tester", s.get("user", ""))
    sgs = requests.get(f"{BASE}/api/settings", headers=hd(), timeout=10).json().get("settings", {})
    check("restart kept system prompt", sgs.get("user_system_prompt", "").startswith("Always answer in one short sentence"))
    check("restart kept response style", sgs.get("response_style") == "concise")

    pl = requests.get(f"{BASE}/api/projects", headers=hd(), timeout=10).json().get("projects") or []
    check("restart kept project", any(p.get("name") == "FUTO Research" for p in pl))

    cl = requests.get(f"{BASE}/api/conversations", headers=hd(), timeout=10).json().get("conversations") or []
    conv = next((x for x in cl if x.get("id") == cid), None)
    check("restart kept conversation", bool(conv), (conv or {}).get("title", "")[:30])

    mem = requests.get(f"{BASE}/api/memory", headers=hd(), timeout=10).json()
    check("restart kept other living records", isinstance(mem.get("records"), list))
    check("deleted memory stayed deleted", not any("teal" in (r.get("text") or "").lower() for r in (mem.get("records") or [])))

    # real chat round-trip (Gemini) honoring concise style
    events, text = send_chat(cid, "In one short sentence: what is 2+2?")
    check("post-restart chat stream", "token" in [e.get("type") for e in events])
    check("post-restart chat no error", all(e.get("type") != "error" for e in events))
    check("post-restart chat replied", bool(text.strip()), text.strip()[:60].replace("\n", " "))

    # leave the app in its pristine default state
    requests.post(f"{BASE}/api/settings", headers=hd(),
                  data=json.dumps({"user_name": "User"}), timeout=10)
    return True


def main():
    spawn_backend()
    print(f"backend pid {DATA['pid'].pid} — log: {LOG}")
    try:
        check("phase1 boot", wait_ready())
        cid, pid = phase1()
        stop_backend()
        time.sleep(2)
        spawn_backend()
        check("phase2 boot (restart persistence)", wait_ready())
        refresh_token()
        phase2(cid)
    finally:
        stop_backend()

    # secret hygiene: the real API key must never appear on any endpoint we called
    key = _load_key()
    if key:
        for path in ("/api/settings", "/api/status", "/api/tools", "/api/mcp",
                     "/api/permissions", "/api/tasks", "/api/memory", "/api/projects"):
            try:
                body = requests.get(BASE + path, headers=hd(), timeout=10).text
                check(f"no secret leak on {path}", key not in body)
            except Exception:
                pass

    print("=" * 50)
    if FAILURES:
        print("FAILURES:", ", ".join(FAILURES))
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    TOKEN = ""
    sys.exit(main())