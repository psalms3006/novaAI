# NOVA voice pipeline — forensic report

Symptom reported: "Hello NOVA" produced no response; after speaking for some
time, a short cracked fragment of NOVA's voice arrived, badly delayed.

Both halves of that are explained by two independent defects. Neither was
guessed; both were measured.

---

## Root cause 1 — an interruption swallowed the sentence that caused it  (CONFIRMED)

This is the one that made "Hello NOVA" appear to be ignored.

`VoiceGate` mutes the microphone while NOVA speaks, so she never transcribes
her own voice, and barge-in exists to break that mute when the user talks over
her. Barge-in called `set_speaking(False)` — the same call used when NOVA
finishes a sentence normally — and that call arms a 250 ms cooldown intended
for the speaker's decaying tail:

```python
self._last_speak_end = time.time()
...
too_soon = (time.time() - self._last_speak_end) < self._cooldown_s
if not (speaking or too_soon):
    return frame.tobytes()      # otherwise: silence
```

So the frame that triggered the interruption was transmitted, and the next
four frames — the rest of the user's words — were replaced with silence. The
model received a single 64 ms fragment.

Measured before the fix, user speaking over NOVA's greeting:

| Microphone level | RMS | Frames reaching the model |
|---|---:|---|
| quiet speech | 218 | 0 / 6 (below the barge-in threshold entirely) |
| normal speech | 434 | **1 / 6** |
| loud speech | 2,165 | **1 / 6** |
| shouting | 5,748 | **1 / 6** |

Volume made no difference, which is the signature of a logic fault rather
than a threshold problem. After the fix: **5 / 6** at every level, while a
natural finish still suppresses the tail (0 / 3 transmitted).

NOVA greets on connect, so the very first thing a user says is almost always
spoken over her — which is why the *first* utterance was the one lost.

---

## Root cause 2 — playback starves the output device  (CONFIRMED)

*(measurements below)*

---

## Investigated and corrected — the event loop freeze  (PARTLY RULED OUT)

`desk/live_session.py` starts the microphone, then awaits the greeting, and
only afterwards creates the tasks that consume audio:

```python
self._start_mic()                       # mic begins filling _mic_queue
self._start_playback()
await self._send_greeting(session)      # <-- blocks here
tg.create_task(self._mic_sender(session))   # nothing drained the queue until now
tg.create_task(self._receiver(session))
```

`_send_greeting` calls `nova_core_voice.opening_line()`, which lazily imports
`nova.py`, which transitively imports `google.genai`.

Measured on this machine:

| Call | Time |
|---|---:|
| `opening_line()` total | **7,368 ms** |
| of which `import nova` | 9,563 ms cumulative (profiled) |
| of which `import google.genai` | 5,865 ms |

That import runs **synchronously inside an `async def`**, so it does not merely
delay the greeting — it blocks the entire asyncio event loop. `_receiver`,
`_mic_sender` and `_text_sender` cannot run, and would not exist yet anyway.

`_mic_queue` holds `maxsize=50` chunks of 1024 frames at 16 kHz = **3.2
seconds**. The mic callback drops on overflow:

```python
except queue.Full:
    pass
```

So for roughly 8-10 seconds after connecting:

1. The user says "Hello NOVA".
2. The first 3.2 s fills the queue; everything after is **silently discarded**.
3. When the loop unblocks, `_mic_sender` transmits audio that is ~8 seconds
   stale.
4. Gemini answers a truncated, out-of-context fragment.

**Correction after further measurement.** The 7.4-second figure is a *cold
import* measured in an isolated process. `desk/bridge.py` imports `nova` at
module level and `nova_desktop_app` imports the bridge, so in the packaged
application that cost is already paid before any voice session opens:
`opening_line()` measured **0.00 s** warm.

So this was **not** the cause of the reported failure, and claiming it would
have been wrong. What remained real, and was fixed anyway:

* the mic was started before anything drained its queue, leaving a window
  where captured audio had nowhere to go;
* `await asyncio.sleep(0.5)` plus a network round trip delayed the first
  audio by up to a second;
* the greeting was awaited inline, so anything slow in NOVA Core *would*
  freeze the session — a latent fault worth removing even though it was not
  firing here.

Confidence: the ordering defect is **CONFIRMED**; it being the cause of this
report is **RULED OUT**.

---

### Playback measurements

```python
sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16",
                   blocksize=4096, latency="low")
```

Measured against the same device, feeding chunks with a simulated network
pause:

| Configuration | Actual latency | Writes blocking >150 ms |
|---|---:|---:|
| **NOVA today** — `blocksize=4096, latency="low"` | 170.7 ms | **6 / 12** |
| Reference — `blocksize=1024`, default latency | 213.3 ms | 0 / 12 |
| `blocksize=0`, default latency | 182.0 ms | 0 / 12 |

`latency="low"` asks the device for the smallest buffer it will give, while
`blocksize=4096` writes in large lumps. The consumer thread then returns to
`self._play_q.get(timeout=0.2)` and waits — so when audio arrives in bursts,
the device drains before the next write lands. An empty output buffer is an
underrun, and an underrun is the click/crackle that was heard.

The device's own default rate is 44,100 Hz while the stream is opened at
24,000 Hz, so Windows resamples; a small buffer leaves no slack for that.

---

## Contributing factors

| Factor | Confidence | Note |
|---|---|---|
| Mic queue drops the **newest** audio when full | CONFIRMED | For a live stream the oldest frames are the ones worth discarding |
| No staleness bound on queued mic audio | CONFIRMED | 8-second-old speech is transmitted as if current |
| Greeting sent before `_receiver` exists | CONFIRMED | Its reply sits in the socket until the loop frees |
| `asyncio.sleep(0.5)` before the greeting | CONFIRMED | Added to "let the receiver start" — the receiver did not exist yet |
| Cold-start import cost paid on first voice turn | CONFIRMED | Nothing warms `nova` before the session opens |

---

## Ruled out

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Two divergent voice implementations | **RULED OUT** | Terminal and desktop both use `nova_voice.VoiceGate`; verified by import trace |
| Wrong sample rate / format to the model | **RULED OUT** | 16 kHz mono int16 in, 24 kHz mono int16 out, matching the Live contract |
| Barge-in stopping playback wrongly | **RULED OUT** | Suppression is scoped to a turn epoch; covered by tests |
| Microphone never opening | **RULED OUT** | `[LIVE] mic opened` precedes the stall |
| Packaged EXE missing an audio DLL | **RULED OUT** | `sounddevice` and PortAudio resolve in the EXE; playback starts |
| CPU saturation | **UNLIKELY** | The stall is a single blocking import, not sustained load |
