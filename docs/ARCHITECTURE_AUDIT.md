# NOVA architecture audit

Measured, not assumed. Reachability is computed from the import closure of the
three real entry points (`nova.py`, `nova_desktop_app.py`, `desk/bridge.py`);
capability claims are checked by reading the implementation, not the filename.

---

## 1. Current architecture

```
ENTRY POINTS
  nova.py                  terminal / --desk launcher, 2208 lines
  nova_desktop_app.py      pywebview shell + ambient orb window
  desk/bridge.py           Flask backend for the SPA, 2346 lines
  nova_cloud/              separate HTTP service (accounts, admin)

LIVE PATH (what actually runs)
  voice     nova_voice.VoiceGate ── shared by live_extra.NOVALive (terminal)
                                    and desk.live_session (desktop)
  model     nova_intelligence/router.py  rank_providers -> failover
  tools     nova._execute_tool_sync      if/elif dispatch, 14 branches
  safety    nova_safety.safety_gate      confirmation + injection detection
            desk/confirm.py              UI confirmation
  memory    memory_extra                 SentenceTransformers + numpy IndexFlatIP
  knowledge offline_extra                ZIM (Kiwix) offline Wikipedia
  accounts  nova_account -> nova_cloud   over HTTP
```

**Reachability: 155 local modules, 60 reachable, 95 unreachable (~20,500
lines).** Excluding `nova_cloud/` (a legitimately separate service reached over
HTTP) and dev scripts, roughly **13,000 lines of unreachable application code**
sit in the repository.

| Package | Modules | Reachable | Status |
|---|---:|---:|---|
| `desk` | 14 | 7 | partly wired |
| `actions` | 14 | 4 | partly wired |
| `nova_intelligence` | 10 | 7 | partly wired |
| `core` | 10 | 6 | partly wired |
| `orchestrator` | 9 | 4 | partly wired |
| `capabilities` | 6 | 4 | registry present, **never called** |
| `agent` | 12 | 2 | mostly dead |
| `memory` | 5 | 0 | **not wired in** |
| `nova_mcp` | 4 | 0 | **not wired in** |
| `nova_cloud` | 15 | 0 | separate service (expected) |

---

## 2. Existing capabilities — genuinely working

| Capability | Evidence |
|---|---|
| **Unified voice policy** | `nova_voice.VoiceGate` is imported by both terminal and desktop. Barge-in, mute-while-speaking, silence keepalive verified by tests. |
| **Provider routing + failover** | `rank_providers()` returns an ordered list; `complete()`/`stream()` walk it. Local/remote classified by `provider_is_local`, not by name. |
| **Confirmation gate** | `nova_safety.safety_gate` is called at the top of `_execute_tool_sync`, per-action, before consequential tools. Wired in both surfaces. |
| **Prompt-injection detection** | `nova_safety` scans tool results and user input. Present and reachable. |
| **Offline knowledge** | ZIM retrieval returns clean prose; web search falls back to `ddgs`. |
| **Semantic memory (dev only)** | SentenceTransformers + numpy inner-product index, with lexical fallback. |
| **Accounts / cloud / admin** | Built and tested this session: 406 tests, real email, Postgres-ready. |
| **Desktop shell + ambient orb** | WebGL orb, ambient window, HUD telemetry, settings. |

---

## 3. Broken or misrepresented capabilities

These are the findings that matter most, because each one currently *looks*
present.

### 3.1 "Agents" are display labels, not agents — **P0**

`desk/bridge.py:_AGENT_ROSTER` lists CODE, RESEARCH, BROWSER, VISION and so
on, and `_agent_key()` maps a *tool call* onto one of those names so the HUD
lights a row. There is no task queue, no worker, no delegation, no reviewer
loop and no agent state machine on the live path. `agent/` (12 modules) and
`orchestrator/` are largely unreachable.

**NOVA cannot currently delegate work or continue talking while work
proceeds.** The brief's §20–§24 are not partially built; they are absent from
the runtime.

### 3.2 Browser "control" is `webbrowser.open()` — **P0**

`actions/browser_control.py` is 100 lines of `webbrowser.open(url)` with
shortcuts for Google/YouTube/GitHub. It cannot read a page, click, scroll,
type, fill a form, upload, download, or verify anything. §12 and §13 are
entirely missing. Nothing named "browser agent" exists.

### 3.3 There is no RAG — **P0**

`memory_extra` embeds short memory *strings*. There is no document ingestion,
no file-type detection, no chunking, no metadata, no source citation and no
asset index. Every document parser the brief requires is **absent from the
environment**: `pypdf`, `python-docx`, `openpyxl`, `python-pptx`,
`pytesseract` are all uninstalled.

Worse: the embedder is deliberately **excluded from the packaged EXE** (spec
comment: torch + transformers would add gigabytes), so shipped NOVA falls back
to lexical search. Semantic memory works on a developer machine and silently
degrades in the product.

### 3.4 The capability registry is shelf-ware — **P1**

`capabilities/registry.py` (759 lines) documents itself as consolidating
`core/capability_bus.py`, `agent/tools/registry.py`, `nova.py:TOOL_DECLARATIONS`
and `tools/__init__.py`, and it even accepts a `fallback_fn` so it can wrap the
existing runtime non-destructively. It registers **0 capabilities** and is
called by nothing. `nova.py` still dispatches with an if/elif chain.

### 3.5 The event bus is wired to nothing — **P1**

`core/event_bus.py` is a sound thread-safe pub/sub with glob patterns and
priority, covered by two test files. Nothing on the live path publishes to it.
Every §35 event (`SCREEN_CHANGED`, `AGENT_STARTED`, `REMINDER_TRIGGERED`…)
would have to come from somewhere that does not exist.

### 3.6 Verification never runs — **P1**

`core/verification_engine.py` exists and is referenced only by
`desk/chat.py`, `orchestrator/` and its own tests. Tool results on the live
path are returned to the model unverified. §40 ("never assume a tool
succeeded") is not enforced.

### 3.7 Screen perception is per-request screenshots — **P1**

`actions/screen_processor.py` grabs a full screenshot via `mss` when a tool is
called. There is no continuous observer, no change detection, no region of
interest, no cached screen state. §10's "do not repeatedly take manual
screenshots" describes exactly the current behaviour.

---

## 4. Duplicate implementations

| Concern | Implementations | Live one |
|---|---|---|
| Memory | `memory_extra`, `living_memory`, `nova_memory`, `memory/layers`, `nova_knowledge_graph`, `memory/knowledge_graph` | `memory_extra` only |
| MCP | `mcp/`, `nova_mcp/`, `nova_mcp_servers/` | `mcp/` partly |
| Agents/orchestration | `agents_extra`, `agent/`, `orchestrator/`, `core/goal_engine`, `core/execution_engine` | `agents_extra` only |
| Tool registry | `capabilities/registry`, `core/capability_bus`, `agent/tools/registry`, `tools/__init__`, `nova.py` if/elif | the if/elif chain |
| Desktop shell | `nova_desktop_app`, `nova_desktop`, `nova_ui`, `nova_3d` (1520 lines) | `nova_desktop_app` |
| One-shot patch scripts | `nova_patches`, `nova_apply_patches`, `nova_critical_fixes`, `nova_fixes_final` (~1250 lines) | none |

Six memory systems and five tool registries is the single clearest signal that
features were added rather than integrated.

---

## 5. Architectural risks

1. **No runtime spine.** Subsystems talk to each other by direct import and
   `if/elif`. Adding proactivity, agents or ambient perception means threading
   new calls through `nova.py` and `desk/bridge.py` by hand each time.
2. **`nova.py` (2208) and `desk/bridge.py` (2346) are the integration
   points.** Every capability lands in one of two files. This is why duplicates
   accumulate on the shelf instead of being adopted.
3. **Capability drift between dev and packaged builds.** Semantic memory is the
   proven case; it works in development and silently degrades in the EXE.
4. **No permission engine.** `desk/confirm.py:_permission_for()` maps a tool to
   a category — that is the whole authorisation model. §25's capability-based
   agent permissions have nothing to build on.
5. **Dead code outweighs live code in several packages**, so "does NOVA have
   X?" cannot be answered by looking for a file named X.

---

## 6. Missing infrastructure

| Missing | Brief | Consequence |
|---|---|---|
| Permission engine (capability-based) | §25, §29 | No agent can be safely given autonomy |
| Task manager on the live path | §21, §36 | No long-running or resumable work |
| Agent orchestrator + queue + reviewer | §20–§24 | No delegation |
| RAG ingestion pipeline | §6, §7 | Cannot use the user's own documents |
| Document/OCR parsers | §6, §8 | Cannot read PDFs, DOCX, images |
| Asset index | §7 | "that image I showed you yesterday" impossible |
| Browser automation | §12, §13 | Cannot act on the web |
| Screen observer + state | §10, §11 | No ambient awareness |
| Address/intent detection | §4 | NOVA answers speech not aimed at it |
| Priority + interruption engine | §2, §3 | Cannot decide when to speak |
| Streaming TTS with continuation | §5 | No long-form speech, no resume |
| Context engine with budgets | §37, §38 | Context assembled ad hoc per call |

---

## 7. Proposed target architecture

Reuse what exists; connect it; add only what is genuinely absent.

```
                     ┌──────────────── NOVA CORE ────────────────┐
   Desktop UI ──►    │  EventBus      (reuse core/event_bus)     │
   Ambient UI ──►    │  Permissions   (NEW)                      │
   CLI        ──►    │  ToolRegistry  (adopt capabilities/)      │
                     │  TaskManager   (adopt task_manager.py)    │
                     │  ContextEngine (NEW, thin)                │
                     │  Verification  (reuse core/verification)  │
                     └───────────────────┬───────────────────────┘
                                         │
        ┌──────────┬──────────┬──────────┼──────────┬──────────┐
        ▼          ▼          ▼          ▼          ▼          ▼
      Voice      Memory      RAG      Vision     Browser     Agents
   (nova_voice) (memory_   (NEW)    (screen    (NEW,       (NEW,
                 extra)              observer)  Playwright)  on TaskManager)
```

Principles for this work:

* **Adopt, do not rewrite.** `core/event_bus.py`, `capabilities/registry.py`
  and `core/verification_engine.py` are sound. They need callers.
* **The spine is additive.** Publishing an event or checking a permission must
  not change existing behaviour until something subscribes.
* **One retirement per adoption.** When a duplicate is superseded, delete it in
  the same change, so the count goes down rather than up.

---

## 8. Dependency changes

| Add | For | Note |
|---|---|---|
| `pypdf` | PDF text | pure Python, small |
| `python-docx`, `openpyxl`, `python-pptx` | Office formats | pure Python |
| `playwright` | browser automation | large; desktop-only, not bundled by default |
| `rapidocr-onnxruntime` *or* `pytesseract` | OCR | ONNX avoids a system Tesseract install |
| `mss` | already present | screen capture |

Deliberately **not** adding: a second vector database, a message broker, or a
container runtime. §54.

Open decision: semantic embeddings in the packaged build. Options are a small
ONNX embedding model (~90 MB, bundleable), embeddings via the cloud provider,
or accepting lexical-only in the EXE. This changes install size materially and
is worth deciding explicitly.

---

## 9. Storage changes

Today: JSON files in the repository root (`living_memory.json`, `nova_tasks.json`,
`goals.json`, `memory.json`, `memory.index`), plus `%APPDATA%/NOVA/nova_desktop.db`
(SQLite, conversations) and the cloud Postgres.

Proposed, per user, under the existing app-data directory:

```
%APPDATA%/NOVA/users/<user_id>/
    profile/user.md              learned profile (§15)
    memory/                      extracted memories + index
    rag/index.sqlite             documents, chunks, metadata, citations
    rag/vectors.npy              embeddings
    assets/                      original files, content-addressed
    tasks.sqlite                 task + agent-run state (§36)
    events.log                   append-only audit (§34)
```

SQLite for anything relational, files for blobs, numpy for vectors. No new
database engine. Note this also fixes a live bug: runtime state currently
writes into the repository/install directory rather than per-user app data.

---

## 10. Security changes

| Change | Why |
|---|---|
| Capability-based permission engine | §25; nothing to build agent autonomy on today |
| Trust labelling on all ingested content | §31; web pages, PDFs and emails must never become instructions |
| Per-agent capability grants | §25; agents must not inherit full access |
| Audit log for every tool execution | §34; `nova_safety` logs some, not all |
| Per-user data isolation at the storage layer | §32; today paths are global, not per user |
| Security maturity self-report | §30; NOVA should state its actual level, not claim excellence |

`nova_safety` already provides the confirmation gate and injection detection —
that is a real Level 1–2 foundation, and the work is to formalise and extend
it rather than replace it.

---

## 11. Implementation phases

| Phase | Content | Depends on |
|---|---|---|
| **A. Spine** | Permission engine (new); event bus wired into the live path; tool dispatch through the registry with verification | — |
| **B. Storage + memory** | Per-user storage layout; `user.md` profile; memory extraction with importance and conflict handling | A |
| **C. RAG** | Ingestion, parsers, chunking, metadata, citations, asset index | B |
| **D. Tasks + agents** | Task manager on the live path; agent registry with permissions; async workers; reviewer loop | A |
| **E. Perception** | Screen observer with change detection; screen state; screen-aware answers | A |
| **F. Proactive** | Priority engine; address detection; interruption policy | A, E |
| **G. Browser** | Playwright session manager; semantic interaction; verification | A, D |
| **H. Voice long-form** | Streaming TTS, chunked generation, continuation | A |

---

## 12. Priorities

**P0 — the product misrepresents itself or cannot do the headline thing**

1. Permission engine — every autonomy feature is blocked on it.
2. RAG: ingestion, parsers, citations. The Definition of Done leads with it.
3. Agents that actually delegate, rather than HUD labels named after tools.
4. Browser automation, or stop describing `webbrowser.open()` as browser control.
5. Packaged-build embedding gap: semantic memory silently degrades in the EXE.

**P1 — structural, blocks everything after it**

6. Wire the event bus into the live runtime.
7. Adopt `capabilities/registry.py`; retire the if/elif chain.
8. Run verification after tool execution.
9. Per-user storage; move runtime state out of the install directory.
10. Screen observer with change detection.

**P2 — important, not blocking**

11. Address/intent detection and the priority engine.
12. Streaming long-form TTS with interruption and resume.
13. Context engine with explicit budgets.
14. Retire the six memory systems down to one; delete the patch scripts.
15. Security maturity self-report.
