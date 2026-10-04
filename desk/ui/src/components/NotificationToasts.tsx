import React from 'react';
import { useNova } from '../context/NovaStateContext';

export const NotificationToasts: React.FC = () => {
  const { toasts, dismissToast, theme } = useNova();

  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-14 right-6 z-50 flex flex-col space-y-2 pointer-events-none max-w-sm w-full">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className="pointer-events-auto p-3.5 rounded-2xl border shadow-xl backdrop-blur-2xl transition-all duration-300 flex items-start justify-between gap-3 animate-slide-up"
          style={{
            backgroundColor: theme.palette.glassSurface,
            borderColor: theme.palette.glassBorder,
            boxShadow: `0 15px 35px -10px rgba(0,0,0,0.5), inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
          }}
        >
          <div className="flex items-start gap-2.5">
            <span
              className="w-2 h-2 rounded-full mt-1.5 shrink-0"
              style={{
                backgroundColor:
                  toast.type === 'success'
                    ? '#10b981'
                    : toast.type === 'warning'
                    ? '#f59e0b'
                    : theme.palette.accent,
              }}
            />
            <div>
              <div className="text-xs font-medium font-sans" style={{ color: theme.palette.textPrimary }}>
                {toast.title}
              </div>
              {toast.description && (
                <div className="text-[11px] leading-relaxed mt-0.5" style={{ color: theme.palette.textSecondary }}>
                  {toast.description}
                </div>
              )}
            </div>
          </div>
          <button
            onClick={() => dismissToast(toast.id)}
            className="opacity-60 hover:opacity-100 transition-opacity text-[10px] p-1"
          >
            <i className="fa-solid fa-xmark" />
          </button>
        </div>
      ))}
    </div>
  );
};
