"""
nova_apply_patches.py
─────────────────────
Run this once from the same folder as nova.py:

    python nova_apply_patches.py

It patches nova.py in-place (backup saved as nova.py.bak.<timestamp>).
Safe to re-run — each patch checks if it's already been applied.
"""

import shutil
import time
from pathlib import Path

NOVA_PATH = Path("nova.py")

if not NOVA_PATH.exists():
    print("❌ nova.py not found. Run from the same folder.")
    raise SystemExit(1)

# ── Backup ────────────────────────────────────────────────────────────────────
backup = NOVA_PATH.parent / f"nova.py.bak.{int(time.time())}"
shutil.copy(NOVA_PATH, backup)
print(f"✅ Backup: {backup.name}")

code = NOVA_PATH.read_text(encoding="utf-8")
applied = []
skipped = []


def patch(name: str, find: str, replace: str) -> None:
    global code
    if replace.strip() in code:
        skipped.append(name)
        return
    if find not in code:
        print(f"⚠️  [{name}] anchor not found — skipping (nova.py may differ)")
        skipped.append(name)
        return
    code = code.replace(find, replace, 1)
    applied.append(name)
    print(f"✅ [{name}] applied")


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 1 — Import nova_patch at the top of nova.py
#  (insert after load_dotenv() call)
# ══════════════════════════════════════════════════════════════════════════════

patch(
    "import_nova_patch",
    find="load_dotenv()\nGEMINI_API_KEY",
    replace=(
        "load_dotenv()\n"
        "\n"
        "# ── NOVA patch extensions ────────────────────────────────────────────\n"
        "try:\n"
        "    from nova_patch import (\n"
        "        _MIN_GAP_BETWEEN_CALLS as _MIN_GAP_BETWEEN_CALLS,  # override 2.5 → 6.0\n"
        "        _MEM_EXTRACT_EVERY_N,\n"
        "        offline_greeting,\n"
        "        check_network_recovery,\n"
        "        EXTRA_TOOL_DECLARATIONS,\n"
        "        execute_extra_tool,\n"
        "        ProactiveAgent,\n"
        "    )\n"
        "    _HAS_NOVA_PATCH = True\n"
        "except ImportError:\n"
        "    _HAS_NOVA_PATCH          = False\n"
        "    _MEM_EXTRACT_EVERY_N     = 5\n"
        "    EXTRA_TOOL_DECLARATIONS  = []\n"
        "    def execute_extra_tool(*a, **k): return None\n"
        "    def offline_greeting(m, s, g): return m.get('user_name', 'there')\n"
        "    def check_network_recovery(*a, **k): return False\n"
        "    class ProactiveAgent:\n"
        "        def __init__(self, *a, **k): pass\n"
        "        def start(self): pass\n"
        "        def update_speak(self, *a): pass\n"
        "        def update_meta(self, *a): pass\n"
        "\n"
        "GEMINI_API_KEY"
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 2 — [FIX-3] Raise _MIN_GAP_BETWEEN_CALLS constant in nova.py itself
#  (nova_patch.py overrides it on import, but set it here as fallback)
# ══════════════════════════════════════════════════════════════════════════════

patch(
    "min_gap_constant",
    find="_MIN_GAP_BETWEEN_CALLS: float = 2.5 # 1 second minimum between REST calls",
    replace="_MIN_GAP_BETWEEN_CALLS: float = 6.0  # 6 s = safe for 10 RPM free quota",
)


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 3 — [FIX-3] Gate memory extraction to every N turns
# ══════════════════════════════════════════════════════════════════════════════

patch(
    "memory_extraction_gate",
    find=(
        "# REST API rate-limit backoff (shared across memory extraction + vision)\n"
        "# When a 429 is received we record a timestamp; all REST calls skip until it expires.\n"
        "_rest_backoff_until: float = 0.0\n"
        "_rest_backoff_secs:  float = 0.0  # grows exponentially per burst"
    ),
    replace=(
        "# REST API rate-limit backoff (shared across memory extraction + vision)\n"
        "# When a 429 is received we record a timestamp; all REST calls skip until it expires.\n"
        "_rest_backoff_until: float = 0.0\n"
        "_rest_backoff_secs:  float = 0.0  # grows exponentially per burst\n"
        "_mem_extract_turn_counter: int = 0  # [FIX-3] count turns for extraction gate"
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 4 — [FIX-3] Throttle extract_memory_updates call site
#  In _receive_audio / run_offline_loop both call extract_memory_updates.
#  Add a turn counter gate around the threading.Thread(...) call.
# ══════════════════════════════════════════════════════════════════════════════

# Patch the Gemini Live receive loop (in NOVALive._receive_audio)
patch(
    "live_memory_gate",
    find=(
        "                            if full_in and len(full_in) > 5:\n"
        "                                threading.Thread(\n"
        "                                    target=lambda u=full_in, a=full_out: extract_memory_updates(u, a, self.meta),\n"
        "                                    daemon=True,\n"
        "                                ).start()"
    ),
    replace=(
        "                            if full_in and len(full_in) > 5:\n"
        "                                global _mem_extract_turn_counter\n"
        "                                _mem_extract_turn_counter += 1\n"
        "                                if _mem_extract_turn_counter % _MEM_EXTRACT_EVERY_N == 0:\n"
        "                                    threading.Thread(\n"
        "                                        target=lambda u=full_in, a=full_out: extract_memory_updates(u, a, self.meta),\n"
        "                                        daemon=True,\n"
        "                                    ).start()"
    ),
)

# Patch the offline loop call to extract_memory_updates
patch(
    "offline_memory_gate",
    find=(
        "        meta = extract_memory_updates(user_input, reply, meta)"
    ),
    replace=(
        "        global _mem_extract_turn_counter\n"
        "        _mem_extract_turn_counter += 1\n"
        "        if _mem_extract_turn_counter % _MEM_EXTRACT_EVERY_N == 0:\n"
        "            meta = extract_memory_updates(user_input, reply, meta)"
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 5 — Add EXTRA_TOOL_DECLARATIONS to TOOL_DECLARATIONS list
# ══════════════════════════════════════════════════════════════════════════════

patch(
    "extra_tool_declarations",
    find=(
        '    {\n'
        '        "name": "remember_fact",\n'
        '        "description": "Save ONE important personal fact'
    ),
    replace=(
        '    # ── Extra tools from nova_patch.py ──────────────────────────────\n'
        '    *EXTRA_TOOL_DECLARATIONS,\n'
        '\n'
        '    {\n'
        '        "name": "remember_fact",\n'
        '        "description": "Save ONE important personal fact'
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 6 — Wire extra tools into _execute_tool_sync
# ══════════════════════════════════════════════════════════════════════════════

patch(
    "extra_tool_dispatch",
    find=(
        '    if not _TOOL_AVAILABILITY.get(tool_name):\n'
        '        return f"Tool \'{tool_name}\' is unavailable (module missing)."'
    ),
    replace=(
        '    # [FIX-4] Try nova_patch extended tools first\n'
        '    _extra = execute_extra_tool(tool_name, args, meta, speak_fn=None)\n'
        '    if _extra is not None:\n'
        '        return _extra\n'
        '\n'
        '    if not _TOOL_AVAILABILITY.get(tool_name):\n'
        '        return f"Tool \'{tool_name}\' is unavailable (module missing)."'
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 7 — [FIX-1] Fix offline greeting
# ══════════════════════════════════════════════════════════════════════════════

patch(
    "offline_greeting_fix",
    find=(
        "    speak_offline(\"NOVA offline mode active. How can I help?\")\n"
        "    speak_offline(\"Where did we stop?\")\n"
        "    name_in = get_text_input() if TEXT_MODE else listen_offline()\n"
        "    if name_in:\n"
        "        user_name         = name_in.strip().split()[0]\n"
        "        meta[\"user_name\"] = user_name\n"
        "        speak_offline(f\"Good to meet you, {user_name}. I'm ready.\")\n"
        "    else:\n"
        "        user_name = meta.get(\"user_name\", \"there\")\n"
        "        speak_offline(\"Let's get started.\")"
    ),
    replace=(
        "    # [FIX-1] Smart greeting using stored memory\n"
        "    _input_fn  = get_text_input if TEXT_MODE else listen_offline\n"
        "    user_name  = offline_greeting(meta, speak_offline, _input_fn)"
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 8 — [FIX-2] Network recovery check inside offline loop
# ══════════════════════════════════════════════════════════════════════════════

patch(
    "network_recovery",
    find=(
        "        if not user_input:\n"
        "            speak_offline(\"I didn't catch that.\")\n"
        "            continue"
    ),
    replace=(
        "        # [FIX-2] Periodic network recovery check\n"
        "        if check_network_recovery(\n"
        "            meta, speak_offline,\n"
        "            get_text_input if TEXT_MODE else listen_offline,\n"
        "            GEMINI_API_KEY or \"\", FORCE_OFFLINE\n"
        "        ):\n"
        "            # User said yes — exit offline loop so caller can restart Live\n"
        "            return\n"
        "\n"
        "        if not user_input:\n"
        "            speak_offline(\"I didn't catch that.\")\n"
        "            continue"
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH 9 — [FIX-5] Proactive agent in main()
# ══════════════════════════════════════════════════════════════════════════════

patch(
    "proactive_agent_main",
    find=(
        "    # ── Start Gemini Live ─────────────────────────────────────────────\n"
        "    print(f\"🌐 Starting Gemini Live mode (voice: {NOVA_VOICE})...\")"
    ),
    replace=(
        "    # ── [FIX-5] Proactive agent ───────────────────────────────────────\n"
        "    _proactive = ProactiveAgent(\n"
        "        speak_fn=lambda t: print(f\"[PROACTIVE] {t}\"),  # placeholder until nova speaks\n"
        "        meta=meta,\n"
        "        planner=_planner,\n"
        "    )\n"
        "    _proactive.start()\n"
        "\n"
        "    # ── Start Gemini Live ─────────────────────────────────────────────\n"
        "    print(f\"🌐 Starting Gemini Live mode (voice: {NOVA_VOICE})...\")"
    ),
)

# Wire proactive agent to nova.speak once Live is up
patch(
    "proactive_live_wire",
    find=(
        "    # Give planner a reference to NOVA's speak function\n"
        "    _planner.set_speak(nova.speak)"
    ),
    replace=(
        "    # Give planner + proactive agent a reference to NOVA's speak function\n"
        "    _planner.set_speak(nova.speak)\n"
        "    _proactive.update_speak(nova.speak)"
    ),
)

# Wire proactive agent to speak_offline in offline fallback
patch(
    "proactive_offline_wire",
    find=(
        "    if status == \"offline\":\n"
        "        print(\"\\n[NOVA] 📴 Internet lost — switching to offline mode automatically.\")\n"
        "        _planner.set_speak(speak_offline)\n"
        "        run_offline_loop(meta)"
    ),
    replace=(
        "    if status == \"offline\":\n"
        "        print(\"\\n[NOVA] 📴 Internet lost — switching to offline mode automatically.\")\n"
        "        _planner.set_speak(speak_offline)\n"
        "        _proactive.update_speak(speak_offline)\n"
        "        run_offline_loop(meta)\n"
        "        # [FIX-2] After offline loop exits (network back, user said switch)\n"
        "        # re-enter the live loop:\n"
        "        _proactive.update_speak(nova.speak)\n"
        "        _planner.set_speak(nova.speak)\n"
        "        status = asyncio.run(nova.run())\n"
        "        if status == \"exit\":\n"
        "            print(\"\\n[NOVA] 🔴 Shutdown.\")"
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
#  Write patched file
# ══════════════════════════════════════════════════════════════════════════════

NOVA_PATH.write_text(code, encoding="utf-8")

print("\n" + "═" * 60)
print(f"✅ Applied  : {len(applied)} patches")
print(f"⏭️  Skipped  : {len(skipped)} (already applied or anchor not found)")
print(f"📄 Backup   : {backup.name}")
print("═" * 60)
if applied:
    print("Applied:", ", ".join(applied))
if skipped:
    print("Skipped:", ", ".join(skipped))
print("\nRun nova.py as normal. No other changes needed.")