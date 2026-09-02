const src = require('fs').readFileSync('desk/static/app.js','utf8');
const checks = ['liveStart', 'liveDisconnect', 'liveScheduleAudio', 'liveFlushQueue', 'liveCancelPlayback', 'liveUpdateTranscript'];
for (const fn of checks) {
  if (!src.includes('function ' + fn)) {
    console.error('MISSING: ' + fn);
    process.exit(1);
  }
}
if (!src.includes('liveWs:')) { console.error('MISSING state.liveWs'); process.exit(1); }
if (!src.includes('liveEngine:')) { console.error('MISSING state.liveEngine'); process.exit(1); }
if (!src.includes('liveAudioCtx:')) { console.error('MISSING state.liveAudioCtx'); process.exit(1); }
if (!src.includes('new WebSocket(')) { console.error('MISSING WebSocket constructor'); process.exit(1); }
if (!src.includes('/api/live/ws')) { console.error('MISSING /api/live/ws endpoint ref'); process.exit(1); }
if (!src.includes('liveCancelPlayback()')) { console.error('MISSING barge-in cancel'); process.exit(1); }
if (!src.includes('liveDisconnect()')) { console.error('MISSING disconnect cleanup'); process.exit(1); }
if (!src.includes('AudioContext')) { console.error('MISSING WebAudio'); process.exit(1); }
console.log('ALL SPA CHECKS PASSED');
