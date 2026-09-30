# UI interaction audit

Status: 2026-09-30. App build `81abe0e` (packaged EXE) plus the revocation fix in the next commit.

## How it was done

1. **Static inventory.** Every screen and control in `desk/ui/src` was traced:
   control → handler → endpoint → the route that registers it → backend effect
   → whether the UI refreshes → why it might be disabled or unclickable. About 150
   controls across 24 screens and sections.
2. **Fix and regression-test** every broken or misleading control. The tests
   are listed per row below.
3. **Runtime checks against the packaged NOVA.exe.** They go through the same
   HTTP endpoints the controls call, including closing and relaunching the app.
   The results are in *Permission matrix (real app)*.
4. **Click-through in a real browser: not completed.** The script that checks
   every visible control for an attached handler and for anything covering it
   (`scratchpad/ui_inventory.js`) is ready. But this machine had about 0.3 GB of
   free RAM (antivirus engine ~1.5 GB), and the Chrome tab froze on the 3D
   Presence screen and then lost its window. It must be re-run with memory
   free, as listed under *Pending* below.

## Broken or misleading controls found, and what was done

| # | Control | Screen | Expected | Actual (before) | Cause | Result |
|---|---|---|---|---|---|---|
| 1 | Skills screen: every button; the whole window | Settings → Skills | list, test, roll back, remove, connect | the window went blank; no Skills route existed at runtime | the Skills API reused the bridge's endpoint name `api_capabilities`; Flask refused the module and the error was logged as a warning | **FIXED**: moved to `/api/skills`; failures log as errors; `test_desk_route_registry` rebuilds the real route table; screens sit in an error boundary. **Verified in EXE:** `/api/skills` answers |
| 2 | "Waiting for you" Yes / No | Settings → Permissions | answer the pending question | could never be clicked | sat under the confirmation dialog's full-screen backdrop | **FIXED**: removed; the row points to the dialog, whose buttons work |
| 3 | Microphone "Ask me" | Permissions, FirstRun | voice waits for the person | behaved exactly like Allow; FirstRun's "off" saved Ask (so still on) | only `deny` was ever checked | **FIXED**: Ask = voice starts only when the person presses the mic. **Verified in EXE** |
| 4 | Microphone "Never" while voice runs | Permissions | voice stops | kept running until next session | revocation not applied live | **FIXED** (revocation). **EXE run found** a session still *connecting* was missed; fixed again, with a regression test |
| 5 | Screen "Never" / "Ask me" | Permissions | no screen access | ambient mode streamed the screen; in-session vision and the camera skipped the gate | screen paths never read `screen_read` | **FIXED**: enforced at screen sharing, ambient auto-watch, in-session vision and camera. **Verified in EXE** |
| 6 | Voice buttons with microphone Never | Voice pill, Voice Session | say why | silent, or showed "Hearing you" with no session | 403 swallowed; state stuck at "connecting" | **FIXED**: the reason is shown; state settles |
| 7 | Space key on a focused switch or button | FirstRun, Permissions | toggles the control | started or muted the microphone | global shortcut swallowed Space | **FIXED**: controls and setup screens keep their keys |
| 8 | "Send things out" | Permissions | gates tools that send data | gated only a tool that does not exist | no tool mapped to `network` | **FIXED**: MCP tools and skill runs map to it |
| 9 | Change files / Computer control / Run programs | Permissions | gate their tools | `generate_document`, `app_control`, `learn_resource` ran unasked | unmapped tools | **FIXED**, tested per tool |
| 10 | FirstRun permission step | FirstRun | all scopes | 7 of 9 shown; "Anything you leave off, she asks" was false | — | **FIXED**: 9 scopes; text true now |
| 11 | Download / Test local model | Models & Offline | reports the result | "failed" after 20 s while it kept downloading | 20 s client timeout | **FIXED**: long timeout for these calls |
| 12 | "Use" (local model) | Models & Offline | "In use" moves | stale until remount | settings not reloaded | **FIXED** |
| 13 | Task progress | Tools & Tasks | 0–100 % | "5000%" | ×100 applied twice | **FIXED** |
| 14 | "Tools & permissions" link | Presence | open Permissions | opened whatever section was last open | section not set | **FIXED** |
| 15 | Left edge of Settings and Library panels | narrow windows | clickable | covered by the nav rail's full-height column | `pointer-events-auto` wrapper | **FIXED**: column click-through, rail clickable |
| 16 | Save (any setting) when the file cannot be written | Settings | NOT SAVED | said SAVED | write errors swallowed | **FIXED**: 500 and NOT SAVED; tested |
| 17 | Workspace folder | General | applies | only after restart | cached on first use | **FIXED**: read live |
| 18 | "Launch at startup" | General | a control | "Not connected yet" text; no control | only FirstRun could set it | **FIXED**: real "When Windows starts" select (`/api/startup`), re-applied at launch |
| 19 | Library → Projects | Library | talk inside a project | no way to create a project conversation | UI never sent `project_id` | **FIXED**: "Start a conversation in this project" |
| 20 | "Cancel and continue setup" | FirstRun | continues | dead if cancel failed | no catch | **FIXED** |
| 21 | Theme picker | anywhere | Escape closes | did not | — | **FIXED** |
| 22 | About / Stop hint text | About, Permissions | true statements | said NOVA never checks updates; said tasks cannot be cancelled | stale copy | **FIXED** |

Checked and working (no change needed): Settings navigation and search;
General/Appearance/Identity/Personality/Conversation controls (persist via
`/api/settings`); Memory search, forget and clear; Account key, cloud,
offline, sign-out and devices; Connections Gmail connect/disconnect;
Diagnostics self-test; Presence task Stop/Try again; Library conversations,
documents and files; mind-map node/refresh/forget; confirmation dialog
Yes/No/Esc; command palette; toasts.

Intentionally informational: Tools & Tasks is read-only; its permission
label reads "Change in Permissions".

## Permission matrix (real app, 2026-09-30)

Driven against the packaged NOVA.exe through the endpoints the Permissions
screen calls, including a full quit and relaunch. The person's own settings
were restored afterwards (verified).

| Permission | Set to | Action | Expected | Actual | Result |
|---|---|---|---|---|---|
| Microphone | Never | press mic | refused | 403 `microphone_denied` | PASS |
| Microphone | Never | push-to-talk | refused | 403 | PASS |
| Microphone | Ask me | window opens (auto start) | waits | `microphone_ask`, not started | PASS |
| Microphone | Ask me | press mic | starts | started | PASS |
| Microphone | Never, revoked mid-session | session connecting | stops | kept going to streaming | **FAIL → fixed** (next build) |
| Screen | Never | switch watching on | refused | 403 `screen_deny` | PASS |
| Screen | Ask me | ambient mode turns it on | refused | `screen_ask` | PASS |
| All 9 scopes | mixed | save and read back | identical | identical | PASS |
| All 9 scopes | mixed | after quit + relaunch (42 s) | identical | identical | PASS |
| Screen | Never, after restart | switch on | refused | 403 | PASS |
| Microphone | Ask me, after restart | auto start | waits | `microphone_ask` | PASS |

Tool scopes (file read/write, browser, computer control, run programs, send
things out) are enforced by the gate every tool call passes. They are proven
per tool in `tests/test_permission_enforcement.py` and
`tests/test_trust_boundary.py`. Proving them through the real app needs the
model to call the tools, and is part of the live acceptance run below.

## Pending

- **Click-through in a real browser**: `ui_inventory.js` on every screen, then
  clicking the safe controls while recording network calls. Blocked by memory:
  close heavy apps (Chrome, ChatGPT) first.
- **Live acceptance of learning and capability acquisition through the app's
  chat**: blocked by the Gemini free-tier daily quota (it resets at midnight
  Pacific, 08:00 Lagos). The learning pipeline itself was proven on the real
  model; see `docs/NOVA_LEARNING.md`.
