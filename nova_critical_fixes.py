"""
nova_critical_fixes.py
Applies 5 critical fixes to nova.py.
Run from project-nova/: python nova_critical_fixes.py
"""
from pathlib import Path
import shutil, time

NOVA = Path("nova.py")
assert NOVA.exists(), "Run from project-nova root"
backup = NOVA.parent / f"nova.py.bak.prefix.{int(time.time())}"
shutil.copy(NOVA, backup)
print(f"Backup: {backup.name}")

code = NOVA.read_text(encoding="utf-8")
results = []

def patch(pid, old, new):
    global code
    if old not in code:
        if new[:60] in code:
            results.append(f"✅ {pid} already applied")
        else:
            results.append(f"⚠️  {pid} NOT FOUND — manual review needed")
        return
    code = code.replace(old, new, 1)
    results.append(f"✅ {pid} applied")

# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: Lazy-import SentenceTransformer (FIXES ~90s startup freeze)
# The top-level import triggers torch, which takes 60–100s on CPU.
# ─────────────────────────────────────────────────────────────────────────────
patch("F1 lazy SentenceTransformer import",
"""try:
    from sentence_transformers import SentenceTransformer
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False""",
"""# SentenceTransformer imported lazily in _load_embedder_async (saves ~90s startup)
HAS_SENTENCE_TRANSFORMERS = True   # will be set False if import fails at load time""")

# ─────────────────────────────────────────────────────────────────────────────
# FIX 2: Fix _load_embedder_async — thread.start() was INSIDE the function
# and the function was never called from main(). Embedder never loaded.
# ─────────────────────────────────────────────────────────────────────────────
patch("F2 fix embedder thread bug",
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
        embed_start = time.time()
        try:
            from sentence_transformers import SentenceTransformer  # lazy import
            _embedder = SentenceTransformer(EMBED_MODEL)
            print(f"✅ Embedding model ready ({time.time()-embed_start:.2f}s)")
        except ImportError:
            HAS_SENTENCE_TRANSFORMERS = False
            log.warning("sentence-transformers not installed — memory search disabled.")
        except Exception as e:
            log.warning(f"Embedding model failed: {e}")
        _embedder_loaded.set()
        # Rebuild index now that embedder is ready
        if _embedder and _memory_texts:
            _rebuild_index()

    threading.Thread(target=_load_embedder_async, daemon=True, name="EmbedLoader").start()""")

# ─────────────────────────────────────────────────────────────────────────────
# FIX 3: Audio crackling — replace asyncio.to_thread with dedicated play thread
# asyncio.to_thread adds thread pool overhead per chunk (42ms at 24kHz) = stutter
# ─────────────────────────────────────────────────────────────────────────────
patch("F3 fix audio crackling",
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
        _play_q: _q.Queue = _q.Queue(maxsize=200)

        def _play_worker() -> None:
            # Dedicated thread — no asyncio overhead per chunk
            stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE, channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE * 4,   # larger block = smoother playback
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
                    pass   # drop oldest would be better but put_nowait is safe
                if self.audio_in_queue.empty() and self._turn_done:
                    self._set_speaking(False)
                    self._turn_done = False
        except Exception as e:
            print(f"❌ Playback error: {e}")
            raise
        finally:
            _play_q.put(None)   # signal worker to stop
            self._set_speaking(False)""")

# ─────────────────────────────────────────────────────────────────────────────
# FIX 4: Gemini Live keepalive — connection drops with 1011 when NOVA speaks
# because mic callback returns early (no audio sent = keepalive timeout).
# Fix: send silence chunks during playback to keep WS alive.
# ─────────────────────────────────────────────────────────────────────────────
patch("F4 keepalive during playback",
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
"""        _silence_chunk: bytes = bytes(CHUNK_SIZE * 2)  # 16-bit = 2 bytes/sample

        def _callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            with self._speaking_lock:
                speaking = self._is_speaking
                too_soon = (time.time() - self._last_speak_end) < 0.25

            if speaking or too_soon:
                # Send silence to keep Gemini WS alive (prevents 1011 keepalive timeout)
                data = {"data": _silence_chunk, "mime_type": "audio/pcm"}
            else:
                data = {"data": indata.tobytes(), "mime_type": "audio/pcm"}

            def _safe_put() -> None:
                try:
                    out_queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

            loop.call_soon_threadsafe(_safe_put)""")

# ─────────────────────────────────────────────────────────────────────────────
# FIX 5: Move startup_start + sd.query_devices + print into main()
# They're currently at module top level, causing the confusing 0.0s print
# AND adding sd.query_devices() to import time.
# ─────────────────────────────────────────────────────────────────────────────
patch("F5 move startup timer into main()",
"""startup_start = time.time()
_ = sd.query_devices()

# ... at the end of main(), before entering the loop:
print(f"\\n[NOVA] ⏱️  Total startup time: {time.time() - startup_start:.1f}s")""",
"""# startup_start set in main() — not at module level""")

# Also fix the double startup_start at top that conflicts with the one in nova_config block
patch("F5b startup_start in main",
"""    print("⚡ Initialising NOVA v3.4...")
    print("=" * 60)""",
"""    startup_start = time.time()
    _ = sd.query_devices()   # warm up audio device enumeration once
    print("⚡ Initialising NOVA v3.4...")
    print("=" * 60)""")

# ─────────────────────────────────────────────────────────────────────────────
# FIX 6: Re-greet on reconnect (assistant appears frozen after reconnect)
# _has_greeted blocks greeting on retry; just reset it on each connect
# ─────────────────────────────────────────────────────────────────────────────
patch("F6 re-greet on reconnect",
"""                    if not self._has_greeted:
                        self._has_greeted = True
                        tg.create_task(self._send_greeting())""",
"""                    # Always greet on each new connection (reconnects feel alive)
                    tg.create_task(self._send_greeting())""")

# ─────────────────────────────────────────────────────────────────────────────
# FIX 7: startup performance log
# ─────────────────────────────────────────────────────────────────────────────
patch("F7 startup perf log",
"""    print(
        f"\\n[NOVA] ⏱️ Startup completed in "
        f"{time.time() - startup_start:.2f}s"
    )""",
"""    _t_total = time.time() - startup_start
    print(f"\\n{'─'*44}")
    print(f"  ⚡ NOVA startup complete in {_t_total:.2f}s")
    print(f"{'─'*44}")""")

NOVA.write_text(code, encoding="utf-8")
print()
for r in results:
    print(r)
print(f"\n✅ Wrote {NOVA} ({len(code.splitlines())} lines)")
