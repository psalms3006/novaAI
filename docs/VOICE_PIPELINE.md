# NOVA's voice pipeline

What it does, why it is shaped this way, and what was wrong with it before.

Every number here was measured against the real Gemini Live API from a real
machine. Where a value was chosen rather than measured, it says so.

---

## The shape of it

```
  microphone ──► VoiceGate ──► MicBatcher ──► asyncio.Queue ──► Gemini Live
   (64 ms)       (nova_voice)   (192 ms)                            │
                      ▲                                             │
                      │ reference                                   ▼
                 speaker ◄── pre-roll ◄── play queue ◄──────── receiver
                                                                    │
                                                          tool calls ──► NOVA Core
```

One `LiveManager` per process, in one background asyncio loop. The desktop
window and the ambient orb are two views of it, not two of it — they subscribe
to its events and never touch audio themselves. Screen frames, when switched
on, go into the same session as the conversation.

Four tasks run inside one `TaskGroup`: the microphone sender, the receiver, the
text sender and the video sender. Playback and capture are threads, because
PortAudio callbacks are threads and blocking writes belong off the loop.

---

## The four failures this replaced

### 1. NOVA interrupted herself

The listening policy interrupted playback whenever a microphone frame exceeded
RMS 350 for two consecutive frames. Every machine has speakers, and NOVA's own
voice comes back through them.

Measured against a recorded Gemini Live response: median frame RMS 2398, peak
9157. At only 10% acoustic coupling, **37% of frames** clear a threshold of
350. The production log shows the consequence — five barge-ins in 830 ms,
immediately after `opening sent (greeting)`. The greeting played about three
chunks and stopped.

Worse, the interruption latched a suppression epoch that discarded the rest of
the turn, so the model went on talking into a queue nobody drained.

**Now:** `EchoCanceller` aligns the audio we handed the speaker against each
microphone frame, solves for the coupling by least squares, subtracts it, and
reports the residual. NOVA's voice cancels; a person's does not.

Three things had to be right, each found by measurement:

- **Measurement and adaptation are separate steps.** Fitting the gain on the
  frame being judged makes the filter cancel the *user* during double-talk,
  which deletes the barge-in entirely. `residual_rms()` only measures;
  `accept()` adapts, and is called only for frames judged to be echo.
- **The decision runs on residual smoothed over 320 ms, as a ratio to playback
  level.** Reverberation spikes; people sustain. Instantaneous ratios reach
  0.52 between words; smoothed ones stay near 0.15.
- **The learned floor only moves on frames comfortably below threshold.**
  Without that gap it chases the user's own voice upward — measured at 25%
  coupling, a runaway tracked speech from 0.57 past 1.3 and no interruption
  was possible again.

### 2. The receiver read one turn and stopped

`session.receive()` yields one **model turn** and then the generator ends — the
SDK breaks its own loop on `turn_complete`:

```python
while result := await self._receive():
  if result.server_content and result.server_content.turn_complete:
    yield result
    break
```

NOVA had a bare `async for msg in session.receive()`. After her first reply the
receiver coroutine returned and never read the socket again: no second answer,
no transcripts, nothing heard. And an unread WebSocket is an unread transport —
the library's incoming queue filled, pongs stopped being processed, and about
seventy seconds later the connection closed with `keepalive ping timeout`.
Every 1011 in the logs traces back to this.

Measured with a scripted four-utterance conversation: **1 of 4 heard before,
4 of 4 after.**

### 3. The greeting deafened the session

Sending client content *before* the realtime audio stream is established leaves
Gemini Live never activating audio input for that connection. The model answers
the text and then ignores everything the user says, for the life of the
session.

Verified both ways against the API:

| order | result |
|---|---|
| greeting, then speech | the utterance is not even transcribed |
| 2 s of microphone, then greeting, then speech | heard and answered |

The greeting now waits for a second of real audio to have reached the model
(`_await_mic_stream`), and gives up rather than staying mute forever if the
microphone never produces anything.

### 4. Tool calls were never answered

The desktop session advertised sixteen tools and handled none of them. Gemini
Live blocks on a function call until the client returns a response, so the
moment NOVA decided to look something up the turn ended with no audio, no
transcript and no reply.

This is why the failure looked intermittent. Conversational turns need no tools
and worked — "hello", "can you hear me" — while anything factual silently did
nothing.

Tools now run off the event loop with a 30 s deadline, and a failure or timeout
still returns a response, because the model needs to hear that the tool failed
rather than wait forever.

---

## Transport

The uplink is the tightest resource in this pipeline, and the cost is **per
message, not per byte**. Measured against Gemini Live over 100 s at a constant
bit rate:

| message size | rate | sends stalled >250 ms | worst |
|---|---|---|---|
| 64 ms | 15.6/s | 6 | 2.9 s |
| 192 ms | 5.0/s | 2 | 3.0 s |
| 500 ms | 2.0/s | 5 | 14.9 s |

So frames are captured at 64 ms — the granularity barge-in needs — and
transmitted in batches of three. Batching helps only up to a point, past which
each message is large enough to stall on its own.

While NOVA is speaking the microphone is muted, so every batch is digital
silence; sending five messages a second of zeroes competes with her own audio
for the same congested uplink. Silence is throttled to one message per 800 ms —
throttled, not withheld, because a stream that stops looks abandoned.

The WebSocket keepalive timeout is widened from the library default of 20 s to
75 s. Individual sends were measured stalling up to 15 s under congestion, and
the pong sits behind that stall in the same socket, so the default closed
connections that were about to recover. Pinging continues at the same rate — a
genuinely dead socket must still be noticed.

---

## Startup

Nothing is said until everything is genuinely up.

```
server starts
      ↓
interface loads and attaches to the event stream
      ↓
POST /api/live/start
      ↓
Live session connects
      ↓
receiver, senders started            (consumers before producers)
      ↓
speaker opened synchronously
microphone opened
      ↓
state: ready
      ↓
a second of real microphone audio reaches the model
      ↓
greeting
      ↓
listening
```

The backend used to spawn a thread that slept two seconds after the HTTP server
bound and then opened the session regardless of anything else — usually before
the window had painted. That is where "NOVA started talking before I could see
the UI" came from.

The speaker is opened synchronously rather than on the playback thread; it used
to be opened inside the worker, so the first chunks of the first sentence
arrived to find it not yet alive and were dropped as `speaker_unavailable`.

---

## Failure and recovery

| condition | behaviour |
|---|---|
| network gone | state `offline`, retry every 5 s indefinitely, plain message |
| credential rejected | stop immediately, say so, do not retry |
| transient drop | exponential backoff, capped at 60 s, 6 attempts |
| model `go_away` | reconnect |
| speaker missing | report it; never mute the microphone on its behalf |
| microphone missing | report it as a hard failure with what to check |
| tool wedged | 30 s deadline, then answer the model anyway |

Being offline does not consume the retry budget. A laptop that sleeps, changes
wifi or goes through a tunnel produces name-resolution failures for as long as
it is disconnected, and counting those meant voice was dead until the app was
restarted.

Greeting happens once per session, never on reconnect — the socket drops
routinely, and re-greeting made NOVA introduce herself repeatedly to someone
mid-conversation.

---

## Screen awareness

Ambient mode can see the screen, in the session the conversation is already
happening in rather than through a separate vision call that would lose the
thread and add a round trip.

It samples rather than streams, because a screenshot is two orders of magnitude
larger than an audio frame and the uplink is already the bottleneck:

- at most one frame every 2 s
- only when the screen actually changed, measured on a 64×64 greyscale
  thumbnail
- downscaled to 1024 px and JPEG-compressed to roughly the size of a second of
  audio

Measured on a real desktop: nine seconds of a static screen cost **one 49 kB
frame and three skips**.

It is off unless explicitly switched on, stops with the session, holds nothing
on disk, and shows a ring on the orb the whole time it is active.

---

## Measured latency

End of the user's speech to the first audible audio, over a healthy connection:

| turn | latency |
|---|---|
| "NOVA, can you hear me?" | 735 ms |
| "Explain that in simple terms." | 932 ms |
| "Hello NOVA." | 1173 ms |
| "What is the capital of Nigeria?" (web_search) | 4707 ms |
| "Who is the current president?" (web_search) | 5093 ms |
| "Tell me in detail how a jet engine works." (long answer) | 6032 ms |

Conversational turns land around a second. Tool-backed turns are dominated by
the tool, not the voice pipeline — the web search itself took 3–5 s of that.

On the same run: 201 s, six turns, no reconnect, **zero self-interruptions**,
636 frames of her own echo correctly suppressed, and the canceller locked onto
the echo path at 248 ms delay with correlation 0.95.

---

## Diagnosing a machine

```
python tools/voice_doctor.py
```

Tests each stage and names the one that failed, rather than leaving "voice
doesn't work" to cover a rejected key, a muted device, a blocked microphone and
a TLS proxy equally. The echo-coupling stage measures how loudly this machine's
speakers reach its microphone, through the real `EchoCanceller`.

Under about 45% coupling there is comfortable margin. Above it NOVA still never
interrupts herself — the design fails safe — but barge-in gets slower.
Headphones fix it outright.
