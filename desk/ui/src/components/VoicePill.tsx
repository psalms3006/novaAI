import React, { useState } from 'react';
import { useNova } from '../context/NovaStateContext';
import { useRuntime } from '../nova/runtime';
import { phaseColor } from './TopHud';

/**
 * NOVA's voice controls and text input. The microphone itself belongs to the
 * backend's voice session -- this never opens one in the page, which would
 * fight the session for the device.
 */
export const VoicePill: React.FC = () => {
  const { theme, setCommandPaletteOpen } = useNova();
  const rt = useRuntime();
  const [text, setText] = useState('');
  const [editing, setEditing] = useState(false);

  const send = () => {
    const t = text.trim();
    if (!t) return;
    setText('');
    rt.sendText(t);
  };

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      send();
    } else if (e.key === 'Escape') {
      setEditing(false);
      (e.target as HTMLInputElement).blur();
    }
  };

  const micLive = rt.voiceRunning && !rt.muted;
  const micLabel = !rt.voiceRunning ? 'Start voice' : rt.muted ? 'Unmute microphone' : 'Mute microphone';
  const onMic = () => (rt.voiceRunning ? rt.setMuted(!rt.muted) : rt.startVoice());
  const color = phaseColor(rt.phase, theme.palette.accent);
  const speaking = rt.phase === 'speaking';
  const replying = rt.chatBusy;

  return (
    <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-30 pointer-events-auto select-none">
      <div
        className="flex items-center space-x-3 px-4 py-2 rounded-full border shadow-2xl backdrop-blur-3xl transition-all duration-300"
        style={{
          backgroundColor: theme.palette.glassSurface,
          borderColor: theme.palette.glassBorder,
          boxShadow: `0 20px 40px -10px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
        }}
      >
        <button
          onClick={onMic}
          disabled={!rt.backendReachable}
          className={`w-7 h-7 rounded-full flex items-center justify-center transition-all cursor-pointer active:scale-95 border disabled:opacity-40 ${micLive ? 'scale-105 shadow-md' : 'hover:scale-105'}`}
          style={{
            backgroundColor: micLive ? color : rt.voiceRunning ? theme.palette.glassSurface : theme.palette.accent,
            color: micLive ? '#ffffff' : rt.voiceRunning ? theme.palette.textSecondary : theme.isDark ? theme.palette.bgBase : '#ffffff',
            borderColor: theme.palette.glassBorder,
          }}
          title={`${micLabel} (Space)`}
          aria-label={micLabel}
          aria-pressed={micLive}
        >
          <i className={`fa-solid ${!rt.voiceRunning ? 'fa-power-off' : rt.muted ? 'fa-microphone-slash' : 'fa-microphone'} text-xs`} />
        </button>

        {(speaking || replying) && (
          <button
            onClick={speaking ? rt.interrupt : rt.stopReply}
            className="w-7 h-7 rounded-full flex items-center justify-center border cursor-pointer hover:scale-105 active:scale-95 transition-all"
            style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.bgElevated }}
            title={speaking ? 'Stop her speaking (Esc)' : 'Stop the reply'}
            aria-label={speaking ? 'Interrupt NOVA' : 'Stop the reply'}
          >
            <i className="fa-solid fa-stop text-[10px]" />
          </button>
        )}

        <div className="flex items-center space-x-2 text-xs font-sans pr-3 border-r min-w-[280px]" style={{ borderColor: theme.palette.glassBorder }}>
          {editing || text ? (
            <input
              type="text"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={onKey}
              onBlur={() => !text && setEditing(false)}
              autoFocus
              maxLength={4000}
              placeholder="Type to NOVA and press Enter…"
              aria-label="Message NOVA"
              className="bg-transparent outline-none w-full text-xs font-sans placeholder:opacity-40 select-text"
              style={{ color: theme.palette.textPrimary }}
            />
          ) : (
            <button
              onClick={() => setEditing(true)}
              className="cursor-text transition-colors flex items-center gap-2 w-full truncate opacity-80 hover:opacity-100 text-left"
              style={{ color: theme.palette.textPrimary }}
              aria-label="Type a message to NOVA"
            >
              <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
              <span className="truncate">
                {rt.phase === 'listening' || rt.phase === 'idle' ? `${rt.phaseLabel} · speak, or click to type` : rt.phaseLabel}
              </span>
            </button>
          )}
          {text && (
            <button onClick={send} className="cursor-pointer opacity-70 hover:opacity-100" style={{ color: theme.palette.accent }} aria-label="Send">
              <i className="fa-solid fa-paper-plane text-[11px]" />
            </button>
          )}
        </div>

        <div className="flex items-center space-x-2 text-[10px] font-sans opacity-60" style={{ color: theme.palette.textSecondary }}>
          <kbd className="px-1.5 py-0.5 rounded text-[9px] border font-mono" style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder }}>
            Ctrl K
          </kbd>
          <button
            onClick={() => setCommandPaletteOpen(true)}
            className="w-5 h-5 rounded-full flex items-center justify-center transition-colors hover:opacity-100 cursor-pointer"
            title="Commands"
            aria-label="Open commands"
          >
            <i className="fa-solid fa-plus text-[10px]" />
          </button>
        </div>
      </div>
    </div>
  );
};
