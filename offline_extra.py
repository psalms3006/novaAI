"""offline_extra.py — offline fallback stack (Ollama/Whisper/pyttsx3/Piper/Wiki/Maps), extracted from nova.py (Phase 2)."""
from __future__ import annotations
import json, os, queue, subprocess, sys, tempfile, threading, time
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
import requests
import sounddevice as sd
from scipy.io import wavfile as wav_write

import nova_state
import nova as _nova
log = _nova.log
HAS_GEMINI = _nova.HAS_GEMINI
GEMINI_API_KEY = _nova.GEMINI_API_KEY
TOOL_DECLARATIONS = _nova.TOOL_DECLARATIONS
NOVA_OFFLINE_PROMPT = _nova.NOVA_OFFLINE_PROMPT
FORCE_OFFLINE = _nova.FORCE_OFFLINE
TEXT_MODE = _nova.TEXT_MODE
OFFLINE_MODELS = _nova.OFFLINE_MODELS
OFFLINE_TIMEOUTS = _nova.OFFLINE_TIMEOUTS
ZIM_DATA_PATH = _nova.ZIM_DATA_PATH
OFFLINE_MAPS_PATH = _nova.OFFLINE_MAPS_PATH
DEFAULT_THRESHOLD = _nova.DEFAULT_THRESHOLD
TTS_RATE = _nova.TTS_RATE
TTS_VOLUME = _nova.TTS_VOLUME
PIPER_MODEL = _nova.PIPER_MODEL
PIPER_RATE = _nova.PIPER_RATE
_MEM_EXTRACT_EVERY_N = _nova._MEM_EXTRACT_EVERY_N
_stt_loaded = _nova._stt_loaded
_stt_model_lock = _nova._stt_model_lock
pyttsx3 = _nova.pyttsx3
is_online = _nova.is_online
is_ollama_running = _nova.is_ollama_running
check_network_recovery = _nova.check_network_recovery
offline_greeting = _nova.offline_greeting
add_memory_fact = _nova.add_memory_fact
build_memory_context = _nova.build_memory_context
get_all_memory_text = _nova.get_all_memory_text
extract_memory_updates = _nova.extract_memory_updates
_execute_tool_sync = _nova._execute_tool_sync
agent_process = _nova.agent_process
_call_gemini_chat = _nova._call_gemini_chat
_trim_history = _nova._trim_history
get_text_input = _nova.get_text_input
_load_whisper_async = _nova._load_whisper_async
calibrate_ambient_noise = _nova.calibrate_ambient_noise

class OfflineState:
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    THINKING = "THINKING"

_offline_state = OfflineState.IDLE
_offline_state_lock = threading.Lock()
_tts_queue = queue.Queue()
_tts_done_event = threading.Event()

def set_offline_state(state: str):
    global _offline_state
    with _offline_state_lock:
        _offline_state = state
    print(f"   [{state}]")

def get_offline_state() -> str:
    with _offline_state_lock:
        return _offline_state

# ─── OFFLINE TTS ENGINE (NON-BLOCKING) ────────────────────────────────────────

def _offline_tts_worker():
    """Background thread for offline TTS. Never blocks the main loop."""
    while True:
        item = _tts_queue.get()
        if item is None:  # Shutdown signal
            break
        text, done_event = item
        set_offline_state(OfflineState.SPEAKING)
        _speak_offline_impl(text)
        set_offline_state(OfflineState.LISTENING)
        done_event.set()

# Start TTS worker
_tts_thread = threading.Thread(target=_offline_tts_worker, daemon=True)
_tts_thread.start()

def speak_offline(text: str, block: bool = False) -> None:
    """
    Queue text for speaking. Non-blocking by default.
    If block=True, waits until speech completes.
    """
    print(f"\n🔊 NOVA: {text}\n")
    _nova._broadcast_ui({"type": "nova_speak", "text": text})
    
    done_event = threading.Event()
    _tts_queue.put((text, done_event))
    
    if block:
        done_event.wait()

def _speak_offline_impl(text: str) -> None:
    if not _speak_pyttsx3(text):
        if not _speak_piper(text):
            log.warning("TTS failed — text only.")


def _speak_pyttsx3(text: str) -> bool:
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", TTS_RATE)
        engine.setProperty("volume", TTS_VOLUME)
        voices = engine.getProperty("voices")
        if voices is None:
            voices = []
        elif not isinstance(voices, (list, tuple, set)):
            voices = [voices]
        for v in voices:
            name = getattr(v, "name", None)
            vid = getattr(v, "id", None)
            if isinstance(name, str) and isinstance(vid, str) and "english" in name.lower():
                engine.setProperty("voice", vid)
                break
        engine.say(text)
        engine.runAndWait()
        engine.stop()
        return True
    except Exception as e:
        log.error(f"pyttsx3: {e}")
        return False

def _speak_piper(text: str) -> bool:
    if not os.path.exists(PIPER_MODEL) or not PIPER_MODEL.endswith(".onnx"):
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-m", "piper", "--model", PIPER_MODEL, "--output-raw"],
            input=text.encode(), capture_output=True, timeout=15, shell=False,
        )
        if result.returncode != 0 or not result.stdout:
            return False
        audio = np.frombuffer(result.stdout, dtype=np.int16)
        if len(audio) == 0:
            return False
        sd.play(audio, samplerate=PIPER_RATE)
        sd.wait()
        return True
    except Exception as e:
        log.error(f"Piper: {e}")
        return False

# ─── OFFLINE STT — UNIFIED MIC LOOP (same as online) ─────────────────────────

def listen_offline() -> str:
    """
    Offline listening with proper state management.
    Uses the same calibrated threshold approach as online mode.
    """
    global _offline_state
    
    if get_offline_state() == OfflineState.SPEAKING:
        # Don't listen while speaking
        return ""
    
    set_offline_state(OfflineState.LISTENING)
    
    fs = 16000
    chunk_size = 1024
    threshold = _nova.AMBIENT_THRESHOLD or DEFAULT_THRESHOLD
    silence_limit = 1.8
    min_speech = 0.6
    max_record = 12.0
    
    max_silent = int((fs / chunk_size) * silence_limit)
    min_speech_r = int((fs / chunk_size) * min_speech)
    max_total = int((fs / chunk_size) * max_record)
    
    print(f"\n🎙️  LISTENING... (threshold: {threshold:.4f})")
    
    recorded = []
    silent = 0
    speech = 0
    is_recording = False
    
    try:
        with sd.InputStream(samplerate=fs, channels=1, dtype="float32", blocksize=chunk_size) as stream:
            while True:
                chunk, _ = stream.read(chunk_size)
                vol = float(np.sqrt(np.mean(chunk ** 2)))
                
                # State visualization
                if vol > threshold:
                    if not is_recording:
                        is_recording = True
                        print("🟢 SPEECH DETECTED")
                    speech += 1
                    silent = 0
                    recorded.append(chunk.copy())
                else:
                    if is_recording:
                        recorded.append(chunk.copy())
                        silent += 1
                    else:
                        # Still buffering pre-speech for context
                        recorded.append(chunk.copy())
                        if len(recorded) > int((fs / chunk_size) * 0.5):  # Keep 0.5s buffer
                            recorded.pop(0)
                
                # Check end conditions
                if silent >= max_silent and speech >= min_speech_r:
                    print("🔇 Silence detected, processing...")
                    break
                if len(recorded) >= max_total and is_recording:
                    print("⏱️ Max recording time reached")
                    break
                    
    except Exception as e:
        print(f"❌ Mic error: {e}")
        set_offline_state(OfflineState.IDLE)
        return ""
    
    if speech < min_speech_r:
        print("🔇 No speech detected.")
        set_offline_state(OfflineState.IDLE)
        return ""
    
    # Process audio
    audio = np.concatenate(recorded, axis=0)
    audio_i16 = (audio * 32767).astype(np.int16)
    
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
        wav_write(tmp_path, fs, audio_i16)
    
    print("🔍 Transcribing with faster-whisper...")
    set_offline_state(OfflineState.THINKING)
    
    try:
        if not _stt_loaded.wait(timeout=10):
            print("⚠️  Whisper not loaded yet — skipping.")
            return ""
        with _stt_model_lock:
            if _nova._stt_model is None:
                return ""
            segments, _ = _nova._stt_model.transcribe(
                tmp_path,
                language="en",
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,
            )
            transcript = " ".join([seg.text.strip() for seg in segments]).strip()
            print(f"📝 Heard: {transcript}")
            return transcript
    except Exception as e:
        print(f"⚠️  Transcription error: {e}")
        return ""
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

# ─── OFFLINE TOOL SYSTEM ───────────────────────────────────────────────────────

# Tool definitions in Ollama format
OLLAMA_TOOLS = []

def _build_ollama_tools():
    """Convert tool declarations to Ollama-compatible format."""
    tools = []
    for decl in TOOL_DECLARATIONS:
        if decl["name"] == "remember_fact":
            continue  # Skip memory tools for now
            
        tool = {
            "type": "function",
            "function": {
                "name": decl["name"],
                "description": decl.get("description", ""),
                "parameters": {
                    "type": "object",
                    "properties": decl.get("parameters", {}).get("properties", {}),
                    "required": decl.get("parameters", {}).get("required", [])
                }
            }
        }
        tools.append(tool)
    return tools

# ─── OFFLINE WIKIPEDIA (ZIM) ─────────────────────────────────────────────────

class OfflineWiki:
    """Offline Wikipedia using ZIM files."""
    
    def __init__(self, data_path: str = ZIM_DATA_PATH):
        self.data_path = Path(data_path)
        self.zim_files = list(self.data_path.glob("*.zim"))
        self.libzim_available = False
        
        try:
            import libzim
            archive_cls = getattr(libzim, "Archive", None) or getattr(libzim, "ZimFile", None)
            if archive_cls is None:
                log.warning("libzim does not expose Archive or ZimFile. Offline Wikipedia unavailable.")
                self.libzim_available = False
                self._readers = {}
                return

            self.libzim_available = True
            self._readers = {}
            for zim_file in self.zim_files:
                try:
                    self._readers[zim_file.stem] = archive_cls(str(zim_file))
                except Exception as e:
                    log.warning(f"Failed to load ZIM {zim_file}: {e}")
        except ImportError:
            log.warning("libzim not installed. Offline Wikipedia unavailable.")
            log.warning("Install: pip install libzim")
    
    def search(self, query: str, limit: int = 3) -> List[Dict[str, str]]:
        """Search offline Wikipedia."""
        if not self.libzim_available or not self._readers:
            return []
        
        results = []
        
        for name, reader in self._readers.items():
            try:
                # Try exact entry first
                entry = reader.get_entry_by_path(query.replace(" ", "_"))
                if entry:
                    content = entry.get_item().content
                    results.append({
                        "source": f"Wikipedia ({name})",
                        "title": query,
                        "content": content[:2000]  # First 2000 chars
                    })
                    continue
                
                # Fallback: search suggestions (limited in libzim)
                suggestions = list(reader.get_suggestions(query, limit=limit))
                for suggestion in suggestions:
                    entry = reader.get_entry_by_path(suggestion[0])
                    if entry:
                        content = entry.get_item().content
                        results.append({
                            "source": f"Wikipedia ({name})",
                            "title": suggestion[0].replace("_", " "),
                            "content": content[:2000]
                        })
                        
            except Exception as e:
                log.debug(f"ZIM search error: {e}")
                continue
        
        return results

_offline_wiki = None

def get_offline_wiki() -> OfflineWiki:
    global _offline_wiki
    if _offline_wiki is None:
        _offline_wiki = OfflineWiki()
    return _offline_wiki

# ─── OFFLINE MAPS ──────────────────────────────────────────────────────────────

class OfflineMaps:
    """Offline maps using OpenStreetMap data (MBTiles or OSM.PBF)."""
    
    def __init__(self, maps_path: str = OFFLINE_MAPS_PATH):
        self.maps_path = Path(maps_path)
        self.mbtiles_files = list(self.maps_path.glob("*.mbtiles"))
        self.has_maps = len(self.mbtiles_files) > 0
        
        if not self.has_maps:
            log.info("No offline maps found. Place .mbtiles files in " + maps_path)
    
    def search_location(self, query: str) -> List[Dict[str, Any]]:
        """Search for locations in offline map data."""
        if not self.has_maps:
            return []
        
        results = []
        try:
            import sqlite3
            for mbtile in self.mbtiles_files:
                conn = sqlite3.connect(str(mbtile))
                cursor = conn.cursor()
                
                # Search in MBTiles metadata and tiles
                cursor.execute("""
                    SELECT zoom_level, tile_column, tile_row, tile_data 
                    FROM tiles 
                    LIMIT 1
                """)
                
                # For proper geocoding, we'd need a spatialite extension
                # This is a basic implementation
                results.append({
                    "source": str(mbtile.name),
                    "query": query,
                    "note": "Basic offline map search. For full geocoding, install spatialite."
                })
                conn.close()
        except Exception as e:
            log.error(f"Offline map search error: {e}")
        
        return results

_offline_maps = None

def get_offline_maps() -> OfflineMaps:
    global _offline_maps
    if _offline_maps is None:
        _offline_maps = OfflineMaps()
    return _offline_maps

# ─── OFFLINE TOOL EXECUTION ──────────────────────────────────────────────────

def _execute_offline_tool(name: str, args: Dict[str, Any], meta: dict) -> str:
    """Execute tools in offline mode with offline-aware fallbacks."""
    
    # Offline Wikipedia fallback for web_search
    if name == "web_search":
        query = args.get("query", "")
        wiki_results = get_offline_wiki().search(query)
        if wiki_results:
            return json.dumps({
                "offline": True,
                "source": "Wikipedia (ZIM)",
                "results": wiki_results
            }, indent=2)
        return "No internet and no offline Wikipedia data available. I can't search for that right now."
    
    # Offline maps fallback for location queries
    if name in ["get_location", "find_place", "navigate"]:
        query = args.get("query", args.get("destination", ""))
        map_results = get_offline_maps().search_location(query)
        if map_results:
            return json.dumps({
                "offline": True,
                "source": "Offline Maps",
                "results": map_results
            }, indent=2)
        return "No internet and no offline map data available for that location."
    
    # File operations work offline
    if name in ["read_file", "write_file", "list_directory", "run_shell"]:
        return _execute_tool_sync(name, args, meta)
    
    # Self-editing works offline
    if name == "self_editor":
        return _execute_tool_sync(name, args, meta)
    
    # Planner works offline
    if name == "planner":
        return _execute_tool_sync(name, args, meta)
    
    # Memory works offline
    if name == "remember_fact":
        return _execute_tool_sync(name, args, meta)
    
    # For tools that absolutely need internet
    internet_required = ["web_search", "send_email", "get_weather", "download_file"]
    if name in internet_required:
        return f"❌ Tool '{name}' requires internet connection. Currently offline."
    
    # Default: try anyway
    return _execute_tool_sync(name, args, meta)

# ─── OFFLINE LLM CALLERS ─────────────────────────────────────────────────────

def _call_ollama_v2(
    model: str,
    messages: List[Dict[str, Any]],
    use_tools: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    v2: Fixed Ollama tool calling.
    Ollama uses 'tools' array with specific format, not OpenAI format.
    """
    timeout = OFFLINE_TIMEOUTS.get(model, 20)
    
    try:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_predict": 1024,
            }
        }
        
        # Only add tools for capable models
        if use_tools and model not in ["tinyllama"]:
            tools = _build_ollama_tools()
            if tools:
                payload["tools"] = tools
        
        r = requests.post(
            "http://localhost:11434/api/chat",
            json=payload,
            timeout=timeout
        )
        r.raise_for_status()
        data = r.json()
        message = data.get("message", {})
        
        # Parse tool calls from Ollama response
        tool_calls = []
        raw_tool_calls = message.get("tool_calls") or []
        
        for tc in raw_tool_calls:
            if isinstance(tc, dict) and "function" in tc:
                func = tc["function"]
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                tool_calls.append({
                    "id": tc.get("id") or str(int(time.time())),
                    "name": func.get("name", ""),
                    "args": args,
                })
        
        return {
            "text": message.get("content", "").strip(),
            "tool_calls": tool_calls,
            "raw_message": message,
        }
        
    except requests.exceptions.Timeout:
        log.warning(f"Ollama/{model} timed out after {timeout}s.")
        return None
    except requests.exceptions.ConnectionError:
        log.warning(f"Ollama/{model} not running. Start with: ollama serve")
        return None
    except Exception as e:
        log.warning(f"Ollama/{model} failed: {e}")
        return None


def _get_offline_response_v2(
    messages: List[Dict[str, Any]],
    use_tools: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    v2: Fixed offline brain with proper tool handling.
    Priority: Gemini REST → Ollama with tools → Ollama without tools.
    """
    # Try Gemini first if online
    if is_online() and HAS_GEMINI and GEMINI_API_KEY:
        print("🌐 [Gemini REST]...")
        result = _call_gemini_chat(messages, use_tools=use_tools)
        if result:
            return result
        print("⚠️  Gemini failed → local brain...")
    
    print("📴 Offline — using local brain...")
    
    if not is_ollama_running():
        print("❌ Ollama not running. Start: ollama serve")
        return None
    
    # Try models with tools first
    for model in OFFLINE_MODELS:
        if model == "tinyllama":
            continue  # Skip tinyllama for tool calls
            
        timeout = OFFLINE_TIMEOUTS.get(model, 20)
        print(f"🔌 [{model}] with tools... (max {timeout}s)")
        
        # Use slim prompt for offline models
        slim = [{"role": "system", "content": NOVA_OFFLINE_PROMPT}] + messages[1:]
        result = _call_ollama_v2(model, slim, use_tools=True)
        
        if result:
            print(f"✅ {model} responded.")
            return result
        print(f"⚠️  {model} failed or timed out.")
    
    # Fallback: try without tools
    for model in OFFLINE_MODELS:
        timeout = OFFLINE_TIMEOUTS.get(model, 20)
        print(f"🔌 [{model}] conversation only... (max {timeout}s)")
        
        slim = [{"role": "system", "content": NOVA_OFFLINE_PROMPT}] + messages[1:]
        result = _call_ollama_v2(model, slim, use_tools=False)
        
        if result:
            print(f"✅ {model} responded (no tools).")
            return result
        print(f"⚠️  {model} failed.")
    
    return None

# ─── OFFLINE THINKING — FIXED AGENT HIJACKING ───────────────────────────────

def think_offline_v2(user_message: str, meta: dict) -> str:
    """
    v2: Fixed offline thinking.
    - Agent only handles specific dev tasks, not everything
    - Proper tool execution with offline fallbacks
    - Memory integration
    """
    
    # [FIX] Agent only for specific patterns, not hijacking
    dev_patterns = [
        "write code", "create file", "edit file", "fix bug", 
        "debug", "refactor", "code review", "implement"
    ]
    is_dev_request = any(p in user_message.lower() for p in dev_patterns)
    
    if is_dev_request:
        agent_result = agent_process(user_message, meta)
        if agent_result and "DEV AGENT" not in agent_result:
            # Agent actually did something useful
            _nova.conversation_history.append({"role": "user", "content": user_message})
            _nova.conversation_history.append({"role": "assistant", "content": agent_result})
            _trim_history()
            return agent_result
    
    # Normal LLM flow
    _nova.conversation_history.append({"role": "user", "content": user_message})
    
    # Build memory context
    mem_ctx = build_memory_context(meta, query=user_message)
    sys_content = NOVA_OFFLINE_PROMPT
    if mem_ctx:
        sys_content += f"\n\nMEMORY:\n{mem_ctx}"
    
    # Add offline capability awareness
    sys_content += "\n\nOFFLINE CAPABILITIES: You have access to offline Wikipedia (ZIM) and offline maps. Use them when relevant."
    
    messages = [{"role": "system", "content": sys_content}] + _nova.conversation_history
    
    # Get LLM response
    result = _get_offline_response_v2(messages, use_tools=True)
    
    if result is None:
        err = "All brain engines offline. Check your connection or start Ollama."
        _nova.conversation_history.append({"role": "assistant", "content": err})
        _trim_history()
        return err
    
    # Handle tool calls
    if result["tool_calls"]:
        print(f"🔧 Tool calls detected: {[tc['name'] for tc in result['tool_calls']]}")
        
        # Build follow-up messages
        followup = list(messages) + [result["raw_message"]]
        
        for tc in result["tool_calls"]:
            tool_name = tc["name"]
            tool_args = tc["args"]
            
            print(f"   Executing: {tool_name}({json.dumps(tool_args)[:100]})")
            
            # Use offline-aware execution
            tool_result = _execute_offline_tool(tool_name, tool_args, meta)
            print(f"   Result: {tool_result[:200]}...")
            
            # Add to conversation
            tool_msg = {
                "role": "tool",
                "content": tool_result,
                "name": tool_name
            }
            if tc.get("id"):
                tool_msg["tool_call_id"] = tc["id"]
            followup.append(tool_msg)
        
        # Get final response after tool execution
        final = _get_offline_response_v2(followup, use_tools=False)
        text = final["text"].strip() if final and final["text"].strip() else "Done."
        
        _nova.conversation_history.append({"role": "assistant", "content": text})
        _trim_history()
        return text
    
    # No tool calls, just text
    text = result["text"].strip() or "I couldn't generate a response."
    _nova.conversation_history.append({"role": "assistant", "content": text})
    _trim_history()
    return text

# ─── OFFLINE LOOP — OVERHAULED ─────────────────────────────────────────────────

def run_offline_loop_v2(meta: dict) -> None:
    """
    v2: Fixed offline loop with:
    - Proper state machine
    - Non-blocking TTS
    - Better mic handling
    - Tool support
    - Offline Wikipedia/Maps
    """
    global user_name
    
    print("\n[NOVA] 🔌 Offline mode v2.0")
    print("       Tools: " + ("✅" if is_ollama_running() else "❌"))
    print("       Wiki:  " + ("✅" if get_offline_wiki().libzim_available else "❌"))
    print("       Maps:  " + ("✅" if get_offline_maps().has_maps else "❌"))
    
    # Set up planner speak callback
    if nova_state._planner is not None:
        nova_state._planner.set_speak(speak_offline)
    
    # Load STT
    if not TEXT_MODE:
        threading.Thread(target=_load_whisper_async, daemon=True).start()
        calibrate_ambient_noise()
    
    # Greeting
    _input_fn = get_text_input if TEXT_MODE else listen_offline
    user_name = offline_greeting(meta, speak_offline, _input_fn)
    
    # Main loop
    while True:
        # Check state
        if get_offline_state() == OfflineState.SPEAKING:
            time.sleep(0.1)
            continue
        
        # Get input
        set_offline_state(OfflineState.LISTENING)
        user_input = get_text_input() if TEXT_MODE else listen_offline()
        
        # Network recovery check
        if check_network_recovery(
            meta, speak_offline,
            get_text_input if TEXT_MODE else listen_offline,
            GEMINI_API_KEY or "", FORCE_OFFLINE
        ):
            return
        
        if not user_input:
            if not TEXT_MODE:
                speak_offline("I didn't catch that.")
            time.sleep(0.5)  # prevent CPU-pinning busy-loop on repeated mic/input failure
            continue
        
        print(f"👤 You: {user_input}")
        set_offline_state(OfflineState.PROCESSING)
        
        lower = user_input.lower()
        
        # Memory queries
        if any(p in lower for p in ["what do you remember", "what do you know about me"]):
            speak_offline(get_all_memory_text(meta))
            set_offline_state(OfflineState.IDLE)
            continue
        
        # Memory storage
        handled = False
        for phrase in ["remember that", "don't forget", "note that", "keep in mind", "remember"]:
            if lower.startswith(phrase):
                fact = user_input[len(phrase):].strip()
                add_memory_fact(fact, meta)
                speak_offline("Got it. Noted.")
                handled = True
                break
        if handled:
            set_offline_state(OfflineState.IDLE)
            continue
        
        # ── Tier 5 & 6 REPL commands ────────────────────────────────────────
        if user_input.startswith("/"):
            _handled = False
            # Heartbeat commands
            try:
                from nova_heartbeat import handle_heartbeat_command
                if _nova._heartbeat is not None:
                    _hb_result = handle_heartbeat_command(user_input, _nova._heartbeat)
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
            break
        
        # Main thinking
        reply = think_offline_v2(user_input, meta)
        speak_offline(reply)
        
        # Periodic memory extraction
        _nova._mem_extract_turn_counter += 1
        if _nova._mem_extract_turn_counter % _MEM_EXTRACT_EVERY_N == 0:
            meta = extract_memory_updates(user_input, reply, meta)
        
        set_offline_state(OfflineState.IDLE)
# At the bottom of your offline code, add:
run_offline_loop = run_offline_loop_v2  # Alias for compatibility
