# NOVA full system audit

Reachability computed from the import closure of every real entry point
(`nova.py`, `nova_desktop_app.py`, `desk/bridge.py`, `nova_cloud/app.py`,
`nova_cloud/manage.py`, the RAG API and the tools), with **relative imports
resolved** and **dynamic imports traced by hand**.

Both of those mattered. A first pass that ignored relative imports reported
`desk/store.py`, `desk/confirm.py` and every `nova_cloud/api_*.py` as dead —
they are live. A second pass still reported `actions/file_processor.py` and
`nova_wake.py` as dead; the first is loaded by
`importlib.import_module(f"actions.{tool_name}")` and the second is launched
as a **subprocess**, so no static analysis can ever see either. Nothing below
is classified on a search alone.

**201 modules · 95 runtime-reachable · 52 test-only · 54 orphaned (11,304
lines) — before correcting for dynamic loading. After correction, 30 modules
(~7,900 lines) are genuinely dead.**

---

## 1. Architecture discovered

```
DESKTOP                              CLOUD (separate process)
  nova_desktop_app.py                  nova_cloud/app.py
    └─ pywebview + WebView2               ├─ api_auth      accounts, sessions
    └─ ambient orb window                 ├─ api_devices   device registry
         │                                ├─ api_sync      preferences
  desk/bridge.py  (Flask + WS)            ├─ api_telemetry metadata only
    ├─ desk/chat.py      tool loop        ├─ api_admin     control plane
    ├─ desk/live_session Gemini Live      ├─ mailer        Resend / SMTP
    ├─ desk/store.py     SQLite convos    └─ static/admin  console UI
    ├─ desk/settings.py  preferences               │
    ├─ desk/creds.py     DPAPI keys           Supabase Postgres
    ├─ desk/confirm.py   confirmation UI
    └─ desk/account_api  account surface
         │
  nova.py  (2,208 lines — the integration point)
    ├─ _execute_tool_sync    if/elif dispatch, 14 branches
    ├─ nova_core/permissions AUTHORISATION  (new)
    ├─ nova_core/trust       trust context  (new)
    ├─ nova_safety           confirmation + injection detection
    ├─ nova_intelligence/    provider routing + failover
    ├─ nova_core/rag/        document library (new)
    ├─ memory_extra          semantic memory
    ├─ living_memory         structured memory
    ├─ offline_extra         ZIM knowledge
    ├─ actions/*             tools, DYNAMICALLY imported by name
    ├─ nova_patches          extra tools, DYNAMICALLY imported
    └─ nova_wake.py          wake word, SUBPROCESS (imports nova_ui)
```

---

## 2. Component status

### Working

| Component | Evidence |
|---|---|
| Voice pipeline | One `VoiceGate` shared by terminal and desktop; barge-in, mute, keepalive covered by tests |
| Provider routing | `rank_providers` + failover; local/remote by capability, not name |
| Authorisation | `nova_core/permissions`; 38 tests; enforced in the live tool path |
| Trust boundary | `nova_core/trust`; taint propagates through a turn; 18 tests |
| Confirmation gate | `nova_safety.safety_gate` called before consequential tools |
| Document library (RAG) | Ingest, chunk, embed, hybrid retrieval with citations; 45 tests |
| Embedding choice | onnx / cloud / lexical, honest about which is active |
| Accounts + cloud | Signup, login, devices, sync, telemetry, admin, MFA; 100+ tests |
| Admin control plane | Real data, RBAC, audit log; driven in a browser |
| Email | Resend and SMTP; verification, reset, new-device notices |
| Packaging | EXE, installer, ZIP; contents verified |

### Partial

| Component | What is missing |
|---|---|
| Memory | `memory_extra` + `living_memory` work. No `user.md` profile, no importance filtering, no conflict resolution |
| Vision | Per-request screenshot only. No continuous observer, no change detection, no screen state |
| Tasks | `task_manager.py` exists and is reachable, but nothing on the live path creates tasks |
| MCP | `mcp/` is wired; `nova_mcp/` and `nova_mcp_servers/` are not |
| Event bus | `core/event_bus.py` is sound and tested; **nothing publishes to it** |
| Capability registry | `capabilities/registry.py` (759 lines) registers **0 capabilities**; `nova.py` still uses if/elif |
| Verification engine | Exists; not called on the live path, so tool results are unverified |

### Broken / misrepresented

| Component | Reality |
|---|---|
| **Agents** | `_AGENT_ROSTER` maps tool calls to HUD labels. No queue, no worker, no delegation, no reviewer loop |
| **Browser control** | `actions/browser_control.py` is `webbrowser.open()`. Cannot read, click, type or fill |
| **NOVA network** | Identity + envelope built and tested this session. No relay, no directory, no connections yet |
| **Project collaboration** | Not started |
| **Bare `pytest`** | Fails to collect: root-level `test_tier56.py` imports `groq`, which is not installed |

---

## 3. Dead code — verified, not guessed

Each entry was checked against dynamic imports, subprocess launches, the
PyInstaller spec and string references.

| Module | Lines | Why it is dead |
|---|---:|---|
| `nova_3d.py` | 1,520 | Superseded by `desk/static/orb3d.js`. Zero references anywhere |
| `nova_apply_patches.py` | 341 | One-shot: "Run this once from the same folder as nova.py". Rewrites nova.py |
| `nova_patches.py` | — | **NOT DEAD.** Dynamically imported by `execute_extra_tool`. Keep |
| `nova_fixes_final.py` | 334 | One-shot patcher, already applied |
| `nova_critical_fixes.py` | 262 | One-shot patcher, already applied |
| `validate_fixes.py` | 229 | Validates those patches; superseded by `tests/` |
| `gemini_compat.py` | 335 | Superseded by `nova_intelligence/gemini_provider.py`. Only referenced by `validate_fixes.py` |
| `nova_reliability.py` | 351 | Superseded by `core/reliability.py`, itself unused |
| `core/reliability.py` | 259 | Never imported |
| `core/execution_engine.py` | 199 | Orchestrator path, unreachable |
| `desk/_smoke_test.py` | 296 | Ad-hoc script, superseded by `tests/` |
| `actions/deepgram_voice.py` | 268 | No tool named `deepgram_voice` exists, so the dynamic loader can never reach it |
| `voice.py` | 193 | Superseded by `nova_voice.py` |
| `nova_desktop.py` | 79 | Superseded by `nova_desktop_app.py` |
| `demo_living_systems.py` | 120 | Demo script |
| `orchestrator/cli.py` | 99 | CLI for an unreachable subsystem |
| Root `test_*.py` (11 files) | ~1,580 | Ad-hoc scripts predating `tests/`. One breaks `pytest` collection |

**Kept despite appearing orphaned** — and why:

* `nova_wake.py` (630) — launched as a **subprocess** by `nova.py:1106`
* `nova_ui.py` (403) — imported by `nova_wake.py`
* `nova_patches.py` (334) — **dynamically imported** by `execute_extra_tool`
* `actions/file_processor.py` (465), `actions/vision.py` (392) — reachable via
  `importlib.import_module(f"actions.{tool_name}")`
* `nova_intelligence/voice_provider.py` (92) — listed in the PyInstaller spec
* `cleanup.py` (106) — already implements the `archive/` convention used below

---

## 4. Duplicates

| Concern | Implementations | Live one | Action |
|---|---|---|---|
| Memory | `memory_extra`, `living_memory`, `nova_memory`, `memory/layers`, `memory/knowledge_graph`, `memory/faiss_store`, `nova_knowledge_graph` | `memory_extra` + `living_memory` | Archive the whole `memory/` package and `nova_knowledge_graph.py` (1,434 lines) |
| MCP | `mcp/`, `nova_mcp/`, `nova_mcp_servers/` | `mcp/` | Archive `nova_mcp/` (459) and `nova_mcp_servers/` (232) |
| Desktop shell | `nova_desktop_app`, `nova_desktop`, `nova_ui`, `nova_3d` | `nova_desktop_app` | Archive `nova_desktop.py`, `nova_3d.py`. **Keep `nova_ui.py`** (wake subprocess) |
| Reliability | `nova_reliability`, `core/reliability` | neither | Archive both |
| Voice module | `voice.py`, `nova_voice.py` | `nova_voice.py` | Archive `voice.py` |
| Gemini SDK layer | `gemini_compat`, `nova_intelligence/gemini_provider` | `gemini_provider` | Archive `gemini_compat.py` |
| Patch scripts | 4 one-shot patchers + validator | none | Archive all |
| Test scripts | 11 root-level, plus `tests/` | `tests/` | Archive the root ones |

---

## 5. Risks

1. **Archiving `memory/` and `nova_mcp/` is the riskiest step.** Both are
   packages; a stray dynamic import would break at runtime rather than at
   import time. Mitigation: full suite plus a real EXE launch after the move.
2. **`nova_patches.py` looks exactly like the dead patchers** and is not.
   Removing it would silently disable a set of extra tools.
3. **The `archive/` folder must be excluded from PyInstaller**, or the
   installer grows and dead code ships.
4. **Root `test_*.py` removal changes `pytest` behaviour** — for the better,
   but anyone relying on `pytest <file>` at the root will notice.

---

## 6. Implementation order

1. Fix `pytest` collection (add config restricting it to `tests/`).
2. Archive the verified-dead modules into `archive/`, with a manifest.
3. Run the full suite; launch the packaged EXE.
4. Wire the NOVA identity into the account system (NOVA ID per installation).
5. Build the project collaboration foundation: projects, membership, roles,
   real-time chat, project-scoped documents. Personal scope stays separate —
   already enforced in the RAG store by SQL scope filtering.
6. Screen sharing and voice/video rooms need `aiortc` (WebRTC), which is not
   installed. Deliberately last, and a new dependency to approve.
