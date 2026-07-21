"""
nova_fixes_final.py  —  Run from project-nova/: python nova_fixes_final.py
Applies 8 targeted patches to nova.py, then prints verification summary.
"""
from pathlib import Path
import shutil, time, sys

NOVA = Path("nova.py")
if not NOVA.exists():
    sys.exit("ERROR: nova.py not found. Run from project-nova/ root.")

backup = NOVA.parent / f"nova.py.bak.fixes.{int(time.time())}"
shutil.copy(NOVA, backup)
print(f"Backup: {backup.name}\n")

code   = NOVA.read_text(encoding="utf-8")
log    = []

def patch(pid, old, new):
    global code
    if new[:50] in code:
        log.append(f"  ✅  {pid}  (already applied)")
        return
    if old not in code:
        log.append(f"  ⚠️   {pid}  OLD STRING NOT FOUND — review manually")
        return
    code = code.replace(old, new, 1)
    log.append(f"  ✅  {pid}")

# ─────────────────────────────────────────────────────────────────────────────
# P1  LAZY SentenceTransformer — eliminates ~90s startup freeze
#     torch loads at import time; move it inside the background thread instead
# ─────────────────────────────────────────────────────────────────────────────
patch("P1  Lazy SentenceTransformer import (fixes ~90s freeze)",
"""try:
    from sentence_transformers import SentenceTransformer
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False""",
"""# SentenceTransformer is imported lazily inside _load_embedder_async (saves ~90s)
HAS_SENTENCE_TRANSFORMERS = True  # will be corrected to False if import fails at load time""")

# ─────────────────────────────────────────────────────────────────────────────
# P2  FIX embedder thread — thread.start() was INSIDE the function body,
#     AND the function was never called. Embedder silently never loaded.
# ─────────────────────────────────────────────────────────────────────────────
patch("P2  Fix embedder thread bug (embedder never loaded)",
"""    def _load_embedder_async():
        global _embedder

        embed_start = time.time()      # ← Start timer here

        if HAS_SENTENCE_TRANSFORMERS:
            try:
                _embedder = SentenceTransformer(EMBED_MODEL)
                print(
                    f"✅ Embedding model ready "
                    f"({time.time() - embed_start:.2f}s)"
                )
            except Exception as e:
                log.warning(f"Embedding model failed: {e}")

        _embedder_loaded.set()

        threading.Thread(
            target=_load_embedder_async,
            daemon=True
        ).start()""",
"""    def _load_embedder_async():
        global _embedder, HAS_SENTENCE_TRANSFORMERS
        t0 = time.time()
        try:
            from sentence_transformers import SentenceTransformer  # lazy import
            _embedder = SentenceTransformer(EMBED_MODEL)
            print(f"  Embedder..........{time.time()-t0:.2f}s")
        except ImportError:
            HAS_SENTENCE_TRANSFORMERS = False
            log.warning("sentence-transformers not installed — memory search disabled.")
        except Exception as e:
            log.warning(f"Embedding model failed: {e}")
        _embedder_loaded.set()
        if _embedder and _memory_texts:
            _rebuild_index()

    threading.Thread(target=_load_embedder_async, daemon=True, name="EmbedLoader").start()""")

# ─────────────────────────────────────────────────────────────────────────────
# P3  FIX audio crackling — asyncio.to_thread per tiny chunk adds scheduling
#     latency between every 42ms audio block → stuttering/crackling.
#     Replace with a dedicated playback thread and blocking queue.
# ─────────────────────────────────────────────────────────────────────────────
patch("P3  Fix audio crackling (replace asyncio.to_thread with play thread)",
"""    async def _play_audio(self) -> None:
        if self.audio_in_queue is None:
            raise RuntimeError("audio_in_queue not initialized")
        audio_in_queue = self.audio_in_queue
        print("🔊 Speaker ready")
        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE, channels=CHANNELS, dtype="int16", blocksize=CHUNK_SIZE
        )
        stream.start()
        try:
            while True:
                chunk = await audio_in_queue.get()
                self._set_speaking(True)
                await asyncio.to_thread(stream.write, chunk)
                if audio_in_queue.empty() and self._turn_done:
                    self._set_speaking(False)
                    self._turn_done = False
        except Exception as e:
            print(f"❌ Playback error: {e}")
            raise
        finally:
            self._set_speaking(False)
            stream.stop()
            stream.close()""",
"""    async def _play_audio(self) -> None:
        if self.audio_in_queue is None:
            raise RuntimeError("audio_in_queue not initialized")
        import queue as _q
        _play_q: _q.Queue = _q.Queue(maxsize=300)

        def _play_worker() -> None:
            # Dedicated OS thread — zero asyncio overhead per audio chunk
            stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE, channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE * 4,  # 4× block = smoother, fewer writes
                latency="low",
            )
            stream.start()
            try:
                while True:
                    chunk = _play_q.get()
                    if chunk is None:
                        break
                    try:
                        stream.write(chunk)
                    except Exception:
                        pass
            finally:
                stream.stop()
                stream.close()

        _pt = threading.Thread(target=_play_worker, daemon=True, name="AudioPlay")
        _pt.start()
        print("🔊 Speaker ready")
        try:
            while True:
                chunk = await self.audio_in_queue.get()
                self._set_speaking(True)
                try:
                    _play_q.put_nowait(chunk)
                except _q.Full:
                    pass  # drop rather than block event loop
                if self.audio_in_queue.empty() and self._turn_done:
                    self._set_speaking(False)
                    self._turn_done = False
        except Exception as e:
            print(f"❌ Playback error: {e}")
            raise
        finally:
            _play_q.put(None)
            self._set_speaking(False)""")

# ─────────────────────────────────────────────────────────────────────────────
# P4  FIX 1011 keepalive timeout — mic callback returns early while NOVA
#     speaks (no audio sent → Gemini WS times out with 1011 after ~15s).
#     Fix: send silence chunks during playback to keep WS alive.
# ─────────────────────────────────────────────────────────────────────────────
patch("P4  Fix 1011 keepalive (send silence during playback)",
"""        def _callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            with self._speaking_lock:
                if self._is_speaking:
                    return
                if time.time() - self._last_speak_end < 0.25:
                    return

            # ── Safe enqueue ───────────────────────────────────────────────────
            # put_nowait inside call_soon_threadsafe raises QueueFull uncaught in
            # the asyncio event loop, which crashes the TaskGroup and drops the
            # Gemini Live connection.  Use a wrapper that swallows the exception.
            data = {"data": indata.tobytes(), "mime_type": "audio/pcm"}

            def _safe_put() -> None:
                try:
                    out_queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass  # Queue momentarily full — silently discard chunk

            loop.call_soon_threadsafe(_safe_put)""",
"""        _silence: bytes = bytes(CHUNK_SIZE * 2)  # 16-bit silence = 2 bytes/sample

        def _callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            with self._speaking_lock:
                speaking  = self._is_speaking
                too_soon  = (time.time() - self._last_speak_end) < 0.25

            if speaking or too_soon:
                # Send silence to keep Gemini WebSocket alive (prevents 1011 ping timeout)
                data = {"data": _silence, "mime_type": "audio/pcm"}
            else:
                data = {"data": indata.tobytes(), "mime_type": "audio/pcm"}

            def _safe_put() -> None:
                try:
                    out_queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

            loop.call_soon_threadsafe(_safe_put)""")

# ─────────────────────────────────────────────────────────────────────────────
# P5  MOVE startup_start to main() — currently at module top level, causing
#     a confusing 0.0s print before imports even finish.
#     Replace with detailed per-subsystem timing log.
# ─────────────────────────────────────────────────────────────────────────────
patch("P5  Move startup timer + add perf log header",
"""startup_start = time.time()
_ = sd.query_devices()

# ... at the end of main(), before entering the loop:
print(f"\\n[NOVA] ⏱️  Total startup time: {time.time() - startup_start:.1f}s")""",
"""# startup_start is set inside main() — not at module level""")

patch("P5b Add startup_start + perf log inside main()",
"""    print("⚡ Initialising NOVA v3.4...")
    print("=" * 60)

    # ── Handle --setup flag ───────────────────────────────────────────────────
    if SETUP_MODE:""",
"""    startup_start = time.time()
    _t = {}   # subsystem timing dict

    _t0 = time.time(); _ = sd.query_devices(); _t["Audio devices"] = time.time() - _t0

    print("⚡ Initialising NOVA v3.4...")
    print("=" * 60)

    # ── Handle --setup flag ───────────────────────────────────────────────────
    if SETUP_MODE:""")

# ─────────────────────────────────────────────────────────────────────────────
# P6  Per-subsystem timing in main()
# ─────────────────────────────────────────────────────────────────────────────
patch("P6  Add per-subsystem timing",
"""    _TOOL_AVAILABILITY = _validate_tool_modules()
    print(f"Tools: {time.time()-t:.2f}s")
    available = [k for k, v in _TOOL_AVAILABILITY.items() if v]
    missing   = [k for k, v in _TOOL_AVAILABILITY.items() if not v]
    print(f"✅ Tools ready   : {', '.join(available)}")
    if missing:
        print(f"⚠️  Tools missing : {', '.join(missing)}")""",
"""    _t0 = time.time()
    _TOOL_AVAILABILITY = _validate_tool_modules()
    _t["Tools"] = time.time() - _t0
    available = [k for k, v in _TOOL_AVAILABILITY.items() if v]
    missing   = [k for k, v in _TOOL_AVAILABILITY.items() if not v]
    print(f"  Tools.............{_t['Tools']:.2f}s  ({len(available)} ready)")
    if missing:
        print(f"  ⚠️  Missing: {', '.join(missing)}")""")

patch("P6b Memory timing",
"""    meta = load_memory()
    name = meta.get("user_name", "")
    if name:
        print(f"👤 Welcome back, {name}.")

     # ── Start planner ─────────────────────────────────────────────────────────
    _planner = NOVAPlanner()

    # ── Start memory ──────────────────────────────────────────────────────────
    _nova_memory = NovaMemory(checkpoint_every_n_turns=10)
    import atexit
    atexit.register(_nova_memory.close_session)""",
"""    _t0 = time.time(); meta = load_memory(); _t["Memory load"] = time.time() - _t0
    print(f"  Memory............{_t['Memory load']:.2f}s  ({len(_memory_texts)} facts)")
    name = meta.get("user_name", "")
    if name:
        print(f"  👤 Welcome back, {name}.")

    _t0 = time.time()
    _planner = NOVAPlanner()
    _t["Planner"] = time.time() - _t0
    print(f"  Planner...........{_t['Planner']:.2f}s")

    _t0 = time.time()
    _nova_memory = NovaMemory(checkpoint_every_n_turns=10)
    _t["NovaMemory"] = time.time() - _t0
    print(f"  NovaMemory........{_t['NovaMemory']:.2f}s")
    import atexit
    atexit.register(_nova_memory.close_session)""")

patch("P6c Total startup timing",
"""    print(
        f"\\n[NOVA] ⏱️ Startup completed in "
        f"{time.time() - startup_start:.2f}s"
    )""",
"""    _t_total = time.time() - startup_start
    print(f"\\n{'─'*42}")
    print(f"  ⚡ NOVA ready in {_t_total:.2f}s  (embedder loading in bg)")
    print(f"{'─'*42}")""")

# ─────────────────────────────────────────────────────────────────────────────
# P7  Always greet on each new Gemini connection (not just the first).
#     After reconnect, _has_greeted=True silences NOVA → appears frozen.
# ─────────────────────────────────────────────────────────────────────────────
patch("P7  Re-greet on every reconnect (fixes frozen-after-reconnect)",
"""                    if not self._has_greeted:
                        self._has_greeted = True
                        tg.create_task(self._send_greeting())""",
"""                    # Greet on every new connection so reconnects don't feel dead
                    tg.create_task(self._send_greeting())""")

# ─────────────────────────────────────────────────────────────────────────────
# Write patched file
# ─────────────────────────────────────────────────────────────────────────────
NOVA.write_text(code, encoding="utf-8")

print("Patches applied:")
for r in log:
    print(r)

total = len([r for r in log if "✅" in r])
warns = len([r for r in log if "⚠️" in r])
print(f"\n{'─'*44}")
print(f"  {total} patches applied   {warns} warnings")
print(f"  nova.py saved ({len(code.splitlines())} lines)")
print(f"  Backup: {backup.name}")
if warns:
    print("\n  ⚠️  For each WARNING above, apply the patch manually.")
    print("     The target string changed — diff the backup to find the right location.")
print(f"{'─'*44}")
print("\nRun NOVA:  python nova.py")