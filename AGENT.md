# Nova — Voice-First AI Assistant Spec
Single source of truth. Updated after Tier 5 & 6 completion audit (June 2026).

## Identity

- **Name:** NOVA
- **One line:** A personal voice-first assistant that helps you get things done on your computer — search, act, and remember — without feeling like a demo chatbot.
- **Audience:** Just you (single-user; per-user state is fine for now).
- **Personality:** Warm, plain-spoken, and brief. Speaks like a capable helper, not a lecturer.

## First three capabilities (Tier 2 targets)

1. **Answer questions and look things up** — web search and factual queries.
2. **Open apps and handle computer tasks** — launch programs, manage files.
3. **Remember facts about you** — preferences and durable notes.

## Stack

- **Language:** Python 3 (existing project-nova codebase).
- **Brain provider:** Gemini 2.5 Flash Live (native audio) online; Gemini REST → Ollama/TinyLlama offline.
- **Runtime:** Laptop-first. Heartbeat (Tier 5) designed to relocate to an always-on host without rewrite.

## Voice (Tier 3+)

- **Input path:** Gemini Live native audio (online) | faster-whisper (offline) | keyboard (--text).
- **TTS:** Gemini native (online) | pyttsx3 → Piper (offline).
- **STT:** Gemini native (online) | faster-whisper (offline).
- **Wake word:** nova_wake.py (optional background listener).

## Safety and proactivity (Tier 6 — NOW COMPLETE)

- **Never without explicit yes:** send_message, self_editor, autostart, game_updater, destructive file ops.
- **Confirmation gate:** Hard code gate in `_execute_tool_sync()` via `nova_safety.safety_gate()`.
- **Prompt injection detection:** Checked on every transcribed turn and tool result via `nova_safety.check_injection()`.
- **Audit log:** `nova_audit.log` + accessible via `/audit` REPL command.
- **Config:** All tuneable values in `nova_config.toml`. No magic numbers in source.
- **Proactive:** Yes — quiet by default. Earns interruptions; does not assume them.
- **Kill switch:** `/pause` stops heartbeat; `/resume` restarts it.

## Build tiers (verification order)

| Tier | What | Status |
|------|------|--------|
| 1 | Text brain, in-session memory, streaming | ✅ Complete |
| 2 | Tool registry (21 tools) | ✅ Complete |
| 3 | Native audio voice wrapper | ✅ Complete |
| 4 | Durable FAISS memory store | ✅ Complete |
| 5 | Heartbeat / proactive loop | ✅ Complete (nova_heartbeat.py) |
| 6 | Confirmation gate, config, audit log | ✅ Complete (nova_safety.py + nova_config.toml) |

## File structure (post-cleanup)

```
project-nova/
├── nova.py                    # Main entrypoint (~3,300 lines)
├── nova_agents.py             # BrowserAgent, MeetingAgent, SurveillanceAgent, SpawnAgent
├── nova_memory.py             # NovaMemory session/long-term memory class
├── nova_patch.py              # ProactiveAgent, extra tools, offline greeting
├── nova_heartbeat.py          # ✨ NEW — Tier 5 full: held notices, quiet hours, inbox
├── nova_safety.py             # ✨ NEW — Tier 6: confirmation gate, injection detect, audit
├── nova_config.toml           # ✨ NEW — single config file for all tuneable values
├── nova_patches.py            # ✨ NEW — applies Tier 5 & 6 integration to nova.py
├── nova_wake.py               # Wake word detector
├── nova_ui.py                 # 3D Web UI components
├── nova_3d.py                 # Three.js JARVIS-style interface
├── nova_apply_patches.py      # Self-editor patch applier utility
├── voice.py                   # ElevenLabs voice
├── memory.json                # User memory facts
├── memory_meta.json           # User name / gender meta
├── memory.index               # FAISS index
├── heartbeat_state.json       # ✨ NEW — persists check timers across restarts
├── heartbeat_inbox.json       # ✨ NEW — held notices inbox
├── nova_audit.log             # ✨ NEW — audit trail of tool runs + confirmations
├── nova_tasks.json            # Planner reminders
├── nova.log                   # Runtime log (rotated by cleanup.py)
├── .env                       # API keys (git-ignored)
├── .gitignore
├── AGENT.md                   # This file
├── cleanup.py                 # Safe project cleanup script
├── test_nova.py               # Integration test
├── test_whisper.py
├── test_tts.py
├── test_deepgram.py
├── test_groq.py
├── test_audio.py
├── wake.wav                   # Wake word audio sample
├── actions/                   # Tool module directory
│   ├── web_search.py
│   ├── open_app.py
│   ├── file_controller.py
│   ├── computer_settings.py
│   ├── browser_control.py
│   └── file_processor.py
├── nova_embedder/             # Local sentence-transformer model
├── nova_memories/             # (legacy session memory)
└── archive/                   # Archived backups & duplicates
    ├── nova.py.bak.*
    ├── nova_agents (1).py
    ├── nova_apply_patches (1).py
    └── nova_*.log (rotated logs)
```

## Run modes

```bash
python nova.py              # → Gemini Live (voice in, voice out)
python nova.py --offline    # → Force offline brain (Gemini REST/Ollama + pyttsx3)
python nova.py --text       # → Offline brain, keyboard input
python nova.py --phone      # → Phone/browser server (WiFi access from phone)
python nova.py --ui         # → Launch 3D Web UI in browser
python nova.py --setup      # → Register NOVA in Windows startup
```

## REPL commands (offline + online text mode)

| Command | What it does |
|---------|-------------|
| `/inbox` | Show unread proactive notices |
| `/dismiss` | Clear all inbox notices |
| `/dismiss <id>` | Clear a specific notice by ID |
| `/pause` | Pause heartbeat (no unsolicited messages) |
| `/resume` | Resume heartbeat |
| `/heartbeat` | Show heartbeat status |
| `/audit` | Show last 10 audit log entries |
| `/cost` | Show session token usage estimate |
| `/facts` | List remembered facts |
| `/clear` | Clear conversation history |
| `/quit` | Exit |

## Provider selection

```bash
AGENT_PROVIDER=claude|gemini|mock  # auto-picks Gemini if GEMINI_API_KEY is set
AGENT_MOCK_FAIL_WEB_SEARCH=1       # test failure handling (mock provider)
NOVA_VISION_MODEL=gemini-2.0-flash # override vision model
```

Requires `GEMINI_API_KEY` in `.env` (git-ignored).

## Assumptions (from Tier 0 interview)

All defaults above were applied because only **B (evolve from Nova)** was answered. Override anything in this file anytime — it's the contract for future sessions.
