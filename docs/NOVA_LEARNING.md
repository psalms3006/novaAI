# NOVA learning — how "learn this folder" works

`nova_learning/` is NOVA's knowledge-acquisition layer: the person
deliberately gives NOVA material, and NOVA builds persistent, verified,
source-grounded knowledge from it. The model is not retrained; it stays the
reasoning engine, and the knowledge lives on disk, per account, independent
of whichever model is in use.

## Kept apart from other memory (§90)

| Kind | What | Where |
|---|---|---|
| Personal memory | facts about the person | `nova_memory`, living memory |
| **Domain knowledge** | what the person taught NOVA on purpose | `nova_learning`, `NOVA_DATA_DIR/knowledge/` |
| Project knowledge | one project's instructions | `desk/projects.py`, or a project-scoped domain |
| Capability knowledge | how NOVA does things | `nova_skills` registry |

## Words mean what they say (§113)

- **Read**: the file was opened.
- **Analyzed**: its content was processed.
- **Learned**: structured knowledge was built *and passed verification*.
- **Unverified**: analyzed, but the checks did not pass. NOVA says so.
- **Remembered**: saved to personal memory, a different store.

## The pipeline

```
folder ─► inventory ─► read documents / look at images ─► extract (batched)
       ─► cross-reference & merge ─► find & resolve contradictions
       ─► build domain model ─► VERIFY (5 checks) ─► register ─► ready
```

| Phase | What happens | Honesty guarantees |
|---|---|---|
| Inventory (`inventory.py`) | every file is classified: document, image, media or other | each skipped file keeps its reason (duplicate, empty, unsupported, too large; audio/video not processed yet) |
| Read (`extract.read_document`) | text comes from the document library's parsers (PDF, DOCX, XLSX, PPTX, MD, HTML, CSV, JSON, code, text) | a corrupt file is reported as failed, and the rest carries on |
| Extract (`extract.py`) | sources are sent in batches labelled `[S1]…`, with images attached; the model returns principles, preferences, rules, patterns and facts | an item citing no source is dropped; a quote not found in the cited text lowers confidence; source text is material, never instructions |
| Consolidate (`consolidate.py`) | near-duplicates merge and keep every source; confidence rises with independent support and authority | — |
| Contradictions | the model lists items that cannot both be followed; each is resolved by **source authority** (configurable, §102), then **recency**, otherwise marked **contested** | never settled silently; the loser is marked *superseded*, not deleted |
| Verify (`verify.py`) | A retrieval · B application · C conflict detection · D source grounding · E novel task, generated from the learned items and answered through NOVA's own retrieval path | D is checked deterministically (the answer must name a real source file); "learned" requires C and D, plus at least 4 of 5 |
| Store (`store.py`) | `domains.json`, `domains/<id>/knowledge.json` (items, contradictions, file manifest), `sessions/`, `timeline.json` | on disk, per account; survives restarts and model changes |

**Updates (§100).** Re-learning the same folder compares checksums. Only new
and changed files are extracted. Knowledge from deleted or changed files is
retired, and verification runs again.

**Background and resumable (§93, §128).** A session runs on its own thread
with real phases and counts. It can be paused and continued. A model outage
stops it as "waiting for the model", with nothing lost. A session interrupted
by NOVA closing is marked "interrupted" at the next start.

**Where it is used (§103).**
- **Chat:** the learned items relevant to each request, with their sources, go into the prompt.
- **Voice:** gets a standing brief.
- **The task reviewer:** checks written work against verified learned principles. Each violation names the principle and its file, and the work is revised to follow it.
- **`nova_learning`:** its `recall`, `why` and `review` commands.

**Surfaces.**
- The `nova_learning` model tool: `learn`, `status`, `continue`, `pause`, `list`, `recall`, `why`, `review`, `forget`.
- `/api/knowledge` for the window.
- **Settings → Knowledge**: learn a folder, live progress, the knowledge with its sources, disagreements, update from folder, forget.

**Models.** Learning uses `gemini-flash-lite-latest` first (its own free-tier
allowance), then `gemini-flash-latest`, then `gemini-2.5-flash`, and the local
model for text when no cloud model answers.

## Evidence (real model, real folder)

The test folder is `C:\Users\Lenovo\NOVA_LEARNING_TEST`, a small but coherent design language. Its 12 files:

- `design-principles.md` and `typography.md`
- `brand-guidelines.pdf` and `layout-reference.pdf`
- `notes/design-preferences.txt`
- three example layout PNGs
- a generic tutorial that contradicts the brand guide
- a corrupt PDF, a `.sketch` file and an `.mp4`

Direct run of the pipeline on `gemini-flash-lite-latest`, 2026-09-30:

| | First run | After the "keep every source's guidance" fix |
|---|---|---|
| Files | 12 discovered, 2 skipped (.sketch, .mp4), 1 failed (corrupt PDF), 6 documents + 3 images analyzed | same |
| Knowledge | 21 items, each with its source file | 26 items |
| Tutorial vs brand guide | **missed**: the model silently dropped the tutorial | rounded cards and blue accent **superseded by source authority** ("the brand / company guideline outranks the other source") |
| Verification | 5/5 | 5/5 |
| Model calls | 12 | 12 |

That run also showed two more problems:

- **Centre-align vs left-align stayed contested.** It was the tutorial against an unclassified file, with ranks 30 vs 35. Any rank difference now decides it.
- **A false conflict was flagged.** "Top bar" vs "12-column grid" came from the same file. The conflict prompt was tightened, and pairs from the same file are dropped.

End-to-end results through the packaged app (learn by chat, interruption,
restart, novel task with and without learned knowledge) are recorded in
`docs/UI_INTERACTION_AUDIT.md` → *Live acceptance*.
