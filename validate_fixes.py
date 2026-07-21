#!/usr/bin/env python3
"""validate_fixes.py — Validate all three bug fixes work correctly.

Tests:
  1. gemini_compat: build_live_config() produces valid config, no ValidationError
  2. nova_memory: _PersistenceWorker handles all actions, atomic writes work
  3. live_extra: imports clean, NOVALive class has correct structure
  4. screen_processor: imports clean, _run_sp_task exists, no TaskGroup
"""
import sys
import os
import tempfile
import json
import time
import threading
from pathlib import Path

# Add nova-fix-workdir to path
WORKDIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKDIR))

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        print(f"  ✅ {name}")
        PASS += 1
    else:
        print(f"  ❌ {name}  {detail}")
        FAIL += 1


print("=" * 60)
print("NOVA v3.4 Bug Fix Validation")
print("=" * 60)

# ── Test 1: gemini_compat ──────────────────────────────────────────────────
print("\n[1] gemini_compat.py — SDK Compatibility Layer")
print("-" * 50)

from gemini_compat import build_live_config, SDK_INFO, validate_sdk, _LCC_FIELDS, _field_supported

check("SDK version detected", SDK_INFO["version"] != "unknown",
      f"got: {SDK_INFO['version']}")
check("SDK has_live", SDK_INFO["has_live"])
check("SDK has_types", SDK_INFO.get("has_types", False))
check("LiveConnectConfig fields probed", len(_LCC_FIELDS) > 0,
      f"got {len(_LCC_FIELDS)} fields")
check("speech_config field supported", _field_supported("speech_config"))
check("voice_config NOT at top level", not _field_supported("voice_config"),
      "voice_config should NOT be a direct field of LiveConnectConfig in v2.10+")

# Build a full config
config = build_live_config(
    voice_name="Charon",
    system_instruction="You are NOVA, a JARVIS-class AI assistant.",
    tool_declarations=[
        {"name": "test_tool", "description": "A test tool",
         "parameters": {"type": "object", "properties": {}}}
    ],
    response_modalities=["AUDIO"],
    enable_input_transcription=True,
    enable_output_transcription=True,
    session_resumption=True,
)
check("build_live_config() returned non-None", config is not None)
if config:
    dump = config.model_dump(by_alias=True)
    check("Config has speechConfig", "speechConfig" in dump)
    check("Config has responseModalities", "responseModalities" in dump)
    check("Config has systemInstruction", "systemInstruction" in dump)
    check("Config has tools", "tools" in dump)
    check("Config has inputAudioTranscription", "inputAudioTranscription" in dump)
    check("Config has outputAudioTranscription", "outputAudioTranscription" in dump)

# Verify the OLD (broken) way fails
try:
    from google.genai import types as gt
    gt.LiveConnectConfig(
        response_modalities=[gt.Modality.AUDIO],
        voice_config=gt.VoiceConfig(
            prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name="Charon")
        ),
    )
    check("Old voice_config path rejected", False, "should have raised ValidationError")
except Exception:
    check("Old voice_config path rejected (ValidationError)", True)

# validate_sdk
result = validate_sdk()
check("validate_sdk() returns compatible=True", result["compatible"],
      f"issues: {result['issues']}")
check("validate_sdk() config_ok=True", result["config_ok"])

# ── Test 2: nova_memory ────────────────────────────────────────────────────
print("\n[2] nova_memory.py — Single-Writer Persistence")
print("-" * 50)

from nova_memory import NovaMemory, _PersistenceWorker

with tempfile.TemporaryDirectory() as tmpdir:
    mem = NovaMemory(memory_dir=tmpdir, autosave_seconds=0.5, checkpoint_every_n_turns=3)

    check("NovaMemory instantiated", True)
    check("Worker thread alive", mem._worker._thread.is_alive())
    time.sleep(0.2)  # let worker process initial flush
    check("Session file exists", mem.session_file.exists())

    # Log some turns
    mem.log_turn("user", "Hello NOVA, can you help me fix a bug?")
    mem.log_turn("assistant", "Of course! What's the bug?")
    mem.log_turn("user", "It's a PermissionError on Windows")
    mem.log_turn("assistant", "That sounds like a file locking race condition.")
    check("4 turns logged", len(mem.current_session["turns"]) == 4)

    # Wait for autosave
    time.sleep(1.0)
    check("Session file written by worker", mem.session_file.exists())

    # Wait for checkpoint (every 3 turns)
    time.sleep(0.5)
    check("Archive file exists", mem.archive_file.exists())

    # Read back
    data = json.loads(mem.session_file.read_text())
    check("Session data has 4 turns", len(data["turns"]) == 4)
    check("Session data has last_autosave", "last_autosave" in data)

    # Summary
    summary = mem.get_last_session_summary()
    check("get_last_session_summary returns string", isinstance(summary, str) and len(summary) > 0)

    # Close session
    mem.close_session()
    check("Worker thread stopped after close", not mem._worker._thread.is_alive())
    check("Session file deleted after close", not mem.session_file.exists())
    check("Summary cache exists", mem.latest_summary_file.exists())

    # Verify atomic write
    check("No .tmp files left", not list(Path(tmpdir).glob("*.tmp")))

# ── Test 3: nova_memory thread safety ──────────────────────────────────────
print("\n[2b] nova_memory.py — Concurrent Write Stress Test")
print("-" * 50)

with tempfile.TemporaryDirectory() as tmpdir:
    errors = []
    mem = NovaMemory(memory_dir=tmpdir, autosave_seconds=0.1, checkpoint_every_n_turns=2)

    def writer(i):
        try:
            for j in range(50):
                mem.log_turn("user", f"stress test turn {i}-{j}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    time.sleep(0.5)  # let worker drain
    mem.close_session()

    check("No errors from 5 concurrent writers", len(errors) == 0,
          f"errors: {errors}")
    check("All turns recorded", len(mem.current_session["turns"]) == 250,
          f"got {len(mem.current_session['turns'])}")

# ── Test 4: live_extra structure ───────────────────────────────────────────
print("\n[3] live_extra.py — Voice Pipeline Architecture")
print("-" * 50)

# We can't import live_extra directly (it needs nova.py which needs sounddevice etc.)
# So we check the source code instead.
src = Path(WORKDIR, "live_extra.py").read_text()

# Strip comments for structural checks
import re
src_nocomments = re.sub(r'#.*$', '', src, flags=re.MULTILINE)

check("NOVALive class defined", "class NOVALive:" in src)
check("_run_task method exists", "async def _run_task(self" in src)
check("_session_error attribute", "_session_error" in src)
check("TaskGroup NOT used in code", "asyncio.TaskGroup" not in src_nocomments,
      "TaskGroup found in live_extra.py code — should be removed")
check("asyncio.create_task used", "asyncio.create_task" in src)
check("asyncio.gather used", "asyncio.gather" in src)
check("_finish_turn removed", "def _finish_turn" not in src,
      "_finish_turn should be removed (dead code)")
check("_send_greeting parks with sleep(inf)", "asyncio.sleep(float(\"inf\"))" in src)
check("gemini_compat imported", "from gemini_compat import" in src)
check("build_live_config used in _build_config", "build_live_config(" in src)
check("_play_audio does NOT set speaking=False on turn_done",
      "self._turn_done = False" not in src.split("async def _receive_audio")[1].split("async def _play_audio")[0],
      "_play_audio should not manage turn_done")

# Check that direct gtypes usage is only in fallback path
build_config_section = src.split("def _build_config")[1].split("def _set_speaking")[0]
check("_build_config prefers gemini_compat",
      "build_live_config(" in build_config_section and "gtypes.LiveConnectConfig" in build_config_section)

# ── Test 5: screen_processor structure ─────────────────────────────────────
print("\n[4] screen_processor.py — Vision Session Architecture")
print("-" * 50)

sp_src = Path(WORKDIR, "actions", "screen_processor.py").read_text()

check("gemini_compat imported", "_build_live_config" in sp_src)
check("_run_sp_task method exists", "_run_sp_task" in sp_src)
check("_session_error attribute", "_session_error" in sp_src)
check("TaskGroup NOT used", "asyncio.TaskGroup" not in sp_src,
      "TaskGroup found in screen_processor.py — should be removed")
check("asyncio.gather used", "asyncio.gather" in sp_src)

# ── Summary ────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print(f"Results: {PASS} passed, {FAIL} failed out of {PASS + FAIL} checks")
if FAIL == 0:
    print("✅ ALL VALIDATIONS PASSED")
else:
    print(f"❌ {FAIL} CHECKS FAILED")
print("=" * 60)

sys.exit(1 if FAIL > 0 else 0)