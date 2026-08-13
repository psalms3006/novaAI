# NOVA voice/offline fixes — 2026-08-08

Only 3 files touched. Everything else in your repo is untouched — copy these
three over your existing copies, or diff them against what you have.

## nova.py (line ~195)
`check_network_recovery()` was a permanent stub returning False. Your own
main() at line ~1648 already has re-entry logic waiting for this to return
True — it was just never wired up, so offline mode could never return online
on its own. Now checks `is_online()` for real.

## offline_extra.py (listen_offline(), ~line 246)
Removed `vad_filter=True` from the faster-whisper transcribe() call. Your
RMS-threshold loop already does speech endpointing before calling Whisper —
Whisper's own internal Silero VAD was re-checking the same clip and
sometimes stripping 100% of it, producing an empty transcript that got
reported as "I didn't catch that" even though real speech was detected.
This was directly confirmed in your run log: "VAD filter removed
00:11.968 of audio" on a clip the RMS gate had already flagged as speech.

## live_extra.py (_send_realtime(), ~line 240)
After 5 consecutive send failures, the task now raises instead of logging
and continuing to loop on a dead websocket session. Previously the
exception was swallowed locally and never reached the TaskGroup, so the
session could sit dead for ~20+ seconds before receive_audio finally
noticed and forced a reconnect.

## NOT FIXED / NOT VERIFIED — genuinely limitations, not swept under the rug
- I have not run this. No Windows box, no mic, no GEMINI_API_KEY, no Ollama
  on my end. Syntax-checked with `ast.parse` only — that catches typos, not
  runtime behavior.
- The out_queue-overflow / feedback-loop pattern from your log (avg_rms
  spiking to 3800+, peak clipping at 32768, "out_queue FULL" drops) is
  UNTOUCHED. That looked like actual audio feedback (speaker bleeding into
  mic) or a genuine WiFi degradation event, not a code bug I could locate
  and patch — you'll need to test with the diagnostics already in place
  and watch whether avg_rms spikes correlate with speaker volume/placement.
- I have not tested that check_network_recovery's is_online() call is cheap
  enough to run every single offline-loop turn without adding latency —
  worth watching if `listen_offline()` starts feeling sluggish.
