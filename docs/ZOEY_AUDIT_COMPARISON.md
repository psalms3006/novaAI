# Zoey self-audit vs NOVA — what was done with each item

Source: `Zoey Complete Technical Self-Audit.md` (2026-09-30). Every section was
compared with NOVA's actual code before deciding. Verdicts: **KEEP NOVA**
(equal or better already), **IMPLEMENT** (gap filled), **IMPROVE** (NOVA had it,
weaker), **ADAPT** (the capability, built NOVA's way), **SKIP** (not right for
NOVA, reason given).

| # | Zoey item | NOVA before | Verdict | What changed / why not |
|---|---|---|---|---|
| 1 | Stateless per-turn runtime | Persistent desktop process with background tasks, scheduler, heartbeat | KEEP NOVA | NOVA does more (works between messages) |
| 2 | Gmail tools (fetch/send/labels/filters) | Read-only Gmail connector used only by a background sweep | ADAPT | `email_search` tool: search on request, read-only (`gmail.readonly`); sending stays out of scope by design |
| 2 | Knowledge entries (create/read/update/move/delete md docs) | `file_controller` + `generate_document` + `nova_learning` (verified, sourced) + document library | KEEP NOVA | Learning with provenance and verification is stronger than plain notes |
| 2 | `fetch_url`, `fetch_video_transcript` | Permission and trust entries existed, no tool | IMPLEMENT | `fetch_url`: one page (main content) or a YouTube transcript; refuses local/private addresses incl. via redirects |
| 2 | Browser page interaction (click/fill/JS) | `browser_control` (open/search) + `app_control` (UIA inside any window, browsers included) | KEEP NOVA | UIA works on the person's real browser; no new DOM driver |
| 2 | Mac: describe_screen / look_at_screen / files / apps | `app_control inspect`, `vision`, `file_controller`, `open_app`, `list_apps` | KEEP NOVA | Equivalent on Windows |
| 2 | start_watching / watch_report (app-usage time) | Screen awareness (vision) in ambient mode | SKIP | Usage-time logging is surveillance-grade data; not requested; NOVA's awareness is opt-in per session |
| 2 | Canva, Notion | none | SKIP | Account-bound; reachable via `nova_capability` (acquire) or MCP when the person wants them |
| 2 | Integrations, custom REST APIs | `nova_capability` acquisition (research → approve once → test → reuse), MCP bridge | KEEP NOVA | Already richer (tested and health-tracked) |
| 2 | Companions / workers / dispatch | Agent roster, `nova_task` background tasks with orchestration | KEEP NOVA | Equivalent |
| 2 | `create_recurring_task` | `nova_scheduler` existed (persisted, grace/stale windows) but no tool could create a workflow; one-shot reminders only | ADAPT | `planner` gains `every` (daily, weekdays, hourly, weekly, every N min) and `run` (do a task on a schedule), plus pause/resume/cancel/list, on nova_scheduler |
| 2 | update_orchestrator (voice/colour/name) | Theme in Settings; name in identity | SKIP | Settings already covers it; a chat tool to change identity was not asked for |
| 3/9 | Reversibility rule: act on reversible, ask on irreversible | Scoped permissions; shutdown once ran on Allow | IMPROVE (earlier today) | Irreversible actions always ask, even on Allow (`desk/confirm.IRREVERSIBLE`) |
| 5 | Permissions opt-in, revocable, card on first use | Nine scopes, allow/ask/deny, confirmation dialog, revocation stops voice/screen | KEEP NOVA | Equal |
| 5/13 | Terminal/System Settings off-limits | Allowed behind permission scopes and confirmation | KEEP NOVA | The user explicitly wanted Settings control; irreversible steps always ask |
| 6/11 | remember_fact | Living memory with supersession, transient filter, research separated | KEEP NOVA | Improved today (pollution fixes) |
| 6/11 | update_working_memory / pick up where I left off | Session archive written, never read | IMPLEMENT | `desk/continuity.py`: last conversations + unfinished tasks at session start (chat and voice) |
| 11 | recall_past_conversations | Conversations stored (typed + voice), no tool | IMPLEMENT | `recall_conversations` by topic and date, both stores |
| 9/18 | Credit awareness | Free-tier quota, no remaining-quota API | ADAPT | Not countable; instead `gemini-flash-lite-latest` added to the cloud fallback chain and an honesty note when a tool-less fallback claims an action |
| 12 | Webhook triggers (inbound) | none | SKIP | A desktop app must not open inbound endpoints |
| 15 | SMS | none | SKIP | No carrier integration; not requested |
| 15 | Push notifications | none — due reminders went to `print()` | IMPLEMENT | Windows notification + spoken/shown + Activity entry (`desk/notify.py`) |
| 17/22 | Honest limits, unknowns | Prompted honesty; claims checked against tool results | KEEP NOVA + IMPROVE | `_unbacked_claim_note` in chat |
| 19 | Marketplace | Skills via `nova_capability` | SKIP | No marketplace planned |
| 20 | Voice/colour/name personalisation | Settings | KEEP NOVA | |
| 21 | Failure modes: report plainly, retry differently, never fabricate | Many honesty paths | KEEP NOVA + IMPROVE | research_report reports what failed; fallback claim note |

Verification of each implemented item is in the commit messages and
`tests/test_zoey_gaps.py`, `tests/test_planner_automations.py`,
`tests/test_fallback_honesty.py`; live EXE checks are recorded in the session report.
