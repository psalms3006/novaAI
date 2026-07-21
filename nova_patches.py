"""
nova_patches.py
═══════════════
Exact patches for nova.py to integrate Tier 5 (nova_heartbeat.py)
and Tier 6 (nova_safety.py).

Apply via NOVA's self_editor tool:
    nova.speak("apply patch 1")
    ...

OR run this file directly:
    python nova_patches.py

Each PATCH_* dict has 'old_code' (exact text to find) and 'new_code'
(replacement). Applied in order. All strings are exact — including
indentation and line endings.
"""

from pathlib import Path

NOVA_PY = Path("nova.py")

PATCHES = [

    # ──────────────────────────────────────────────────────────────────────────
    # PATCH 1 — Load nova_config.toml at startup (after load_dotenv())
    # ──────────────────────────────────────────────────────────────────────────
    {
        "id": "P1 — load nova_config.toml",
        "old_code": "load_dotenv()",
        "new_code": """load_dotenv()

# ── Load nova_config.toml (Tier 6 config) ────────────────────────────────────
try:
    import tomllib as _tomllib
    _cfg_path = Path("nova_config.toml")
    if _cfg_path.exists():
        _NOVA_CFG = _tomllib.loads(_cfg_path.read_text())
    else:
        _NOVA_CFG = {}
except Exception:
    _NOVA_CFG = {}

def _cfg(section: str, key: str, default):
    \"\"\"Read a value from nova_config.toml with fallback.\"\"\"
    return _NOVA_CFG.get(section, {}).get(key, default)

# Override constants from config (only if config exists)
if _NOVA_CFG:
    NOVA_VOICE              = _cfg("nova",       "voice",           NOVA_VOICE)
    MAX_GEMINI_RETRIES      = _cfg("nova",       "max_retries",     MAX_GEMINI_RETRIES)
    MAX_HISTORY_TURNS       = _cfg("nova",       "history_turns",   MAX_HISTORY_TURNS)
    VISION_MODEL            = _cfg("model",      "vision_model",    VISION_MODEL)
    WHISPER_MODEL_SIZE      = _cfg("model",      "whisper_size",    WHISPER_MODEL_SIZE)
    TTS_RATE                = _cfg("tts",        "rate",            TTS_RATE)
    TTS_VOLUME              = _cfg("tts",        "volume",          TTS_VOLUME)
    PHONE_PORT              = _cfg("server",     "phone_port",      PHONE_PORT)
    UI_PORT                 = _cfg("server",     "ui_port",         UI_PORT)
    _MEM_EXTRACT_EVERY_N    = _cfg("memory",     "extract_every_n", 5)
    _MIN_GAP_BETWEEN_CALLS  = _cfg("rate_limit", "min_gap_secs",    _MIN_GAP_BETWEEN_CALLS)""",
    },

    # ──────────────────────────────────────────────────────────────────────────
    # PATCH 2 — Safety gate + audit logger inside _execute_tool_sync()
    # Target: the first line of _execute_tool_sync body
    # ──────────────────────────────────────────────────────────────────────────
    {
        "id": "P2 — safety gate in _execute_tool_sync",
        "old_code": """def _execute_tool_sync(tool_name: str, args: dict, meta: dict) -> str:
    print(f"🔧 Tool: {tool_name}({json.dumps(args, ensure_ascii=False)[:120]})")
    if tool_name == "vision":""",
        "new_code": """def _execute_tool_sync(tool_name: str, args: dict, meta: dict) -> str:
    print(f"🔧 Tool: {tool_name}({json.dumps(args, ensure_ascii=False)[:120]})")

    # ── Tier 6: hard confirmation gate ───────────────────────────────────────
    try:
        from nova_safety import safety_gate, log_tool_run
        _gate = safety_gate(
            tool_name, args,
            get_confirmation=lambda: input("NOVA awaiting your yes/no: ").strip(),
        )
        if _gate is not None:
            return _gate   # user declined — return reason string to model
    except ImportError:
        pass

    if tool_name == "vision":""",
    },

    # ──────────────────────────────────────────────────────────────────────────
    # PATCH 3 — Log every tool result to audit trail
    # Target: the last return in _execute_tool_sync
    # ──────────────────────────────────────────────────────────────────────────
    {
        "id": "P3 — audit log in _execute_tool_sync",
        "old_code": """    try:
        module  = importlib.import_module(f"actions.{tool_name}")
        execute = getattr(module, "execute", None)
        if execute is None:
            return f"Tool '{tool_name}' has no execute() function."
        return str(execute(args))
    except Exception as e:
        log.exception(f"Tool execution failed: {tool_name}")
        return f"Tool '{tool_name}' error: {e}"


def _validate_tool_modules""",
        "new_code": """    try:
        module  = importlib.import_module(f"actions.{tool_name}")
        execute = getattr(module, "execute", None)
        if execute is None:
            return f"Tool '{tool_name}' has no execute() function."
        _result = str(execute(args))
        try:
            from nova_safety import log_tool_run
            log_tool_run(tool_name, args, _result)
        except ImportError:
            pass
        return _result
    except Exception as e:
        log.exception(f"Tool execution failed: {tool_name}")
        return f"Tool '{tool_name}' error: {e}"


def _validate_tool_modules""",
    },

    # ──────────────────────────────────────────────────────────────────────────
    # PATCH 4 — Prompt injection check on transcribed user speech
    # Target: inside _receive_audio(), after full_in is assembled
    # ──────────────────────────────────────────────────────────────────────────
    {
        "id": "P4 — injection check in _receive_audio",
        "old_code": """                                    full_in = " ".join(in_buf).strip()
                                    if full_in:
                                        print(f"\\n🎤 You: {full_in}")""",
        "new_code": """                                    full_in = " ".join(in_buf).strip()
                                    if full_in:
                                        print(f"\\n🎤 You: {full_in}")
                                        # Tier 6: prompt injection check
                                        try:
                                            from nova_safety import check_injection, flag_injection
                                            if check_injection(full_in):
                                                flag_injection(full_in, speak_fn=self.speak)
                                        except ImportError:
                                            pass""",
    },

    # ──────────────────────────────────────────────────────────────────────────
    # PATCH 5 — Heartbeat init in main() after ProactiveAgent
    # ──────────────────────────────────────────────────────────────────────────
    {
        "id": "P5 — Heartbeat init in main()",
        "old_code": """    _proactive = ProactiveAgent(
        speak_fn=lambda t: print(f"[PROACTIVE] {t}"),
        meta=meta,
        planner=_planner,
    )
    _proactive.start()""",
        "new_code": """    _proactive = ProactiveAgent(
        speak_fn=lambda t: print(f"[PROACTIVE] {t}"),
        meta=meta,
        planner=_planner,
    )
    _proactive.start()

    # ── Tier 5: Full heartbeat with held notices + quiet hours ────────────────
    try:
        from nova_heartbeat import Heartbeat
        _heartbeat = Heartbeat(
            speak_fn=lambda t: print(f"\\n🔔 NOVA: {t}"),
            meta=meta,
            planner=_planner,
        )
        _heartbeat.start()
        log.info("Heartbeat started (Tier 5 complete).")
    except ImportError:
        _heartbeat = None
        log.warning("nova_heartbeat.py not found — Tier 5 partial only.")""",
    },

    # ──────────────────────────────────────────────────────────────────────────
    # PATCH 6 — Update speak_fn for heartbeat when going live
    # Target: just before asyncio.run(nova.run())
    # ──────────────────────────────────────────────────────────────────────────
    {
        "id": "P6 — wire heartbeat speak_fn to nova.speak",
        "old_code": """    # Give planner + proactive agent a reference to NOVA's speak function
    _planner.set_speak(nova.speak)
    _proactive.update_speak(nova.speak)

    try:
        status = asyncio.run(nova.run())""",
        "new_code": """    # Give planner + proactive agent a reference to NOVA's speak function
    _planner.set_speak(nova.speak)
    _proactive.update_speak(nova.speak)
    if _heartbeat:
        _heartbeat.update_speak(nova.speak)

    # Tier 5: show any notices that arrived since last session
    if _heartbeat:
        missed = _heartbeat.show_missed()
        if missed:
            nova.speak(missed)

    try:
        status = asyncio.run(nova.run())""",
    },

    # ──────────────────────────────────────────────────────────────────────────
    # PATCH 7 — REPL commands in offline loop
    # Target: the "# Exit" check in run_offline_loop_v2
    # ──────────────────────────────────────────────────────────────────────────
    {
        "id": "P7 — /inbox /audit /pause /resume REPL commands",
        "old_code": """        # Exit
        if any(w in lower for w in ["goodbye nova", "shutdown nova", "exit nova", "close nova"]):
            speak_offline(f"Goodbye, {user_name}.")
            break""",
        "new_code": """        # ── Tier 5 & 6 REPL commands ────────────────────────────────────────
        if user_input.startswith("/"):
            _handled = False
            # Heartbeat commands
            try:
                from nova_heartbeat import handle_heartbeat_command
                _hb_result = handle_heartbeat_command(user_input, _heartbeat)
                if _hb_result is not None:
                    speak_offline(_hb_result)
                    set_offline_state(OfflineState.IDLE)
                    _handled = True
            except (ImportError, Exception):
                pass
            # Audit command
            if not _handled and user_input == "/audit":
                try:
                    from nova_safety import get_audit_log
                    speak_offline(get_audit_log(last_n=10))
                except ImportError:
                    speak_offline("nova_safety.py not installed.")
                _handled = True
            if not _handled and user_input == "/cost":
                try:
                    from nova_safety import get_cost_summary
                    speak_offline(get_cost_summary())
                except ImportError:
                    speak_offline("nova_safety.py not installed.")
                _handled = True
            if _handled:
                continue

        # Exit
        if any(w in lower for w in ["goodbye nova", "shutdown nova", "exit nova", "close nova"]):
            speak_offline(f"Goodbye, {user_name}.")
            break""",
    },

    # ──────────────────────────────────────────────────────────────────────────
    # PATCH 8 — Switch heartbeat speak_fn when falling back to offline
    # Target: the "status == offline" branch at bottom of main()
    # ──────────────────────────────────────────────────────────────────────────
    {
        "id": "P8 — update heartbeat when switching to offline",
        "old_code": """    if status == "offline":
        print("\\n[NOVA] 🔴 Internet lost — switching to offline mode automatically.")
        _planner.set_speak(speak_offline)
        _proactive.update_speak(speak_offline)
        run_offline_loop(meta)""",
        "new_code": """    if status == "offline":
        print("\\n[NOVA] 🔴 Internet lost — switching to offline mode automatically.")
        _planner.set_speak(speak_offline)
        _proactive.update_speak(speak_offline)
        if _heartbeat:
            _heartbeat.update_speak(speak_offline)
            missed = _heartbeat.show_missed()
            if missed:
                speak_offline(missed)
        run_offline_loop(meta)""",
    },
]


# ── Patch applier ─────────────────────────────────────────────────────────────

def apply_patches(dry_run: bool = False) -> list[str]:
    """
    Apply all patches to nova.py.
    Returns list of results per patch.
    If dry_run=True, only checks without writing.
    """
    if not NOVA_PY.exists():
        return ["ERROR: nova.py not found in current directory."]

    code = NOVA_PY.read_text(encoding="utf-8")
    results = []

    for p in PATCHES:
        pid   = p["id"]
        old   = p["old_code"]
        new   = p["new_code"]

        if old not in code:
            # Check if new_code already applied
            if new in code or (len(new) > 40 and new[:40] in code):
                results.append(f"✅ {pid} — already applied, skipped.")
            else:
                results.append(f"⚠️  {pid} — old_code not found. Manual review needed.")
            continue

        count = code.count(old)
        if count > 1:
            results.append(f"⚠️  {pid} — old_code found {count}×; applying first occurrence only.")

        if not dry_run:
            code = code.replace(old, new, 1)
            results.append(f"✅ {pid} — applied.")
        else:
            results.append(f"🔍 {pid} — would apply (dry_run).")

    if not dry_run:
        backup = NOVA_PY.parent / f"nova.py.bak.tier56.{int(__import__('time').time())}"
        backup.write_text(NOVA_PY.read_text(encoding="utf-8"), encoding="utf-8")
        NOVA_PY.write_text(code, encoding="utf-8")
        results.append(f"\nBackup saved: {backup.name}")
        results.append("nova.py updated.")

    return results


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    print(f"{'DRY RUN — ' if dry else ''}Applying {len(PATCHES)} patches to nova.py...\n")
    for r in apply_patches(dry_run=dry):
        print(r)
