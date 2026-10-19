import React, { useEffect, useRef } from 'react';
import { useNova } from '../context/NovaStateContext';
import { useRuntime } from '../nova/runtime';

/**
 * NOVA asking permission before a consequential action (desk/confirm.py).
 * Her tool thread is blocked until this is answered, so it sits above every
 * screen. The answer goes back through POST /api/confirm -- the interface
 * never decides for the user, and there is no auto-approve here.
 */
export const ConfirmDialog: React.FC = () => {
  const { theme } = useNova();
  const { pendingConfirm, decide } = useRuntime();
  const noRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // Focus lands on "No": an accidental Enter must never approve an action.
    if (pendingConfirm) noRef.current?.focus();
  }, [pendingConfirm?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!pendingConfirm) return null;
  const { id, tool, prompt } = pendingConfirm;

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 backdrop-blur-xl animate-fade-in" style={{ backgroundColor: 'rgba(3, 4, 6, 0.72)' }}>
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="nova-confirm-title"
        aria-describedby="nova-confirm-body"
        className="w-full max-w-lg p-6 rounded-3xl border shadow-2xl space-y-4 backdrop-blur-3xl transition-all"
        style={{
          backgroundColor: theme.palette.bgElevated,
          borderColor: 'rgba(245, 158, 11, 0.35)',
          boxShadow: `0 30px 70px -15px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
        }}
        onKeyDown={(e) => {
          if (e.key === 'Escape') decide(id, false);
        }}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="w-9 h-9 rounded-2xl flex items-center justify-center border shrink-0" style={{ backgroundColor: 'rgba(245,158,11,0.12)', borderColor: 'rgba(245,158,11,0.25)', color: '#f59e0b' }}>
              <i className="fa-solid fa-shield-halved text-sm" />
            </span>
            <div>
              <h3 id="nova-confirm-title" className="text-sm font-semibold tracking-tight" style={{ color: theme.palette.textPrimary }}>
                Permission Required
              </h3>
              <p className="text-[11px] font-sans" style={{ color: theme.palette.textMuted }}>
                NOVA is requesting your approval before taking action.
              </p>
            </div>
          </div>
          <span className="px-2 py-0.5 rounded-full border text-[10px] font-mono tracking-wide uppercase shrink-0" style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}>
            {tool}
          </span>
        </div>

        <div className="p-3.5 rounded-2xl border text-xs leading-relaxed select-text font-sans" style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary }}>
          <p id="nova-confirm-body">{prompt}</p>
        </div>

        <div className="flex items-center justify-between pt-1">
          <div className="flex items-center gap-2 text-[10px] font-mono" style={{ color: theme.palette.textMuted }}>
            <span><kbd className="px-1.5 py-0.5 rounded border" style={{ borderColor: theme.palette.glassBorder }}>Esc</kbd> Refuse</span>
            <span>·</span>
            <span><kbd className="px-1.5 py-0.5 rounded border" style={{ borderColor: theme.palette.glassBorder }}>Enter</kbd> on focused</span>
          </div>
          <div className="flex items-center gap-2.5">
            <button
              ref={noRef}
              onClick={() => decide(id, false)}
              className="px-4 py-2 rounded-xl border text-xs font-medium cursor-pointer transition-all hover:brightness-110 active:scale-98"
              style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary, backgroundColor: theme.palette.glassSurface }}
            >
              Refuse
            </button>
            <button
              onClick={() => decide(id, true)}
              className="px-4 py-2 rounded-xl border text-xs font-medium cursor-pointer transition-all hover:brightness-110 active:scale-98 shadow-md"
              style={{ backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#ffffff', borderColor: theme.palette.accentBorder }}
            >
              Allow Action
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
