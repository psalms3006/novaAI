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
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 backdrop-blur-md animate-fade-in" style={{ backgroundColor: 'rgba(0,0,0,0.45)' }}>
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="nova-confirm-title"
        aria-describedby="nova-confirm-body"
        className="w-full max-w-md p-5 rounded-3xl border shadow-2xl space-y-4"
        style={{
          backgroundColor: theme.palette.bgElevated,
          borderColor: 'rgba(245, 158, 11, 0.45)',
          boxShadow: `0 25px 60px -15px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
        }}
        onKeyDown={(e) => {
          if (e.key === 'Escape') decide(id, false);
        }}
      >
        <div className="flex items-center gap-2.5">
          <span className="w-8 h-8 rounded-full flex items-center justify-center" style={{ backgroundColor: 'rgba(245,158,11,0.15)', color: '#f59e0b' }}>
            <i className="fa-solid fa-shield-halved text-xs" />
          </span>
          <div>
            <h3 id="nova-confirm-title" className="text-sm font-medium" style={{ color: theme.palette.textPrimary }}>
              NOVA is asking permission
            </h3>
            <div className="text-[10px] font-mono opacity-60" style={{ color: theme.palette.textMuted }}>
              {tool}
            </div>
          </div>
        </div>
        <p id="nova-confirm-body" className="text-xs leading-relaxed select-text" style={{ color: theme.palette.textSecondary }}>
          {prompt}
        </p>
        <div className="flex justify-end gap-2">
          <button
            ref={noRef}
            onClick={() => decide(id, false)}
            className="px-4 py-1.5 rounded-xl border text-xs font-medium cursor-pointer"
            style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface }}
          >
            No
          </button>
          <button
            onClick={() => decide(id, true)}
            className="px-4 py-1.5 rounded-xl border text-xs font-medium cursor-pointer"
            style={{ backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#ffffff', borderColor: theme.palette.accentBorder }}
          >
            Yes, go ahead
          </button>
        </div>
        <div className="text-[10px]" style={{ color: theme.palette.textMuted }}>
          Change what needs asking in Settings → Permissions.
        </div>
      </div>
    </div>
  );
};
