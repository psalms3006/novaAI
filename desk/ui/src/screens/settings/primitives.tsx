// The Settings screen's building blocks, in the Stitch design's styling.
// Sections compose these instead of repeating inline styles, so a theme or
// glass change reaches every section the same way.

import React from 'react';
import { useNova } from '../../context/NovaStateContext';

export const SectionHeader: React.FC<{ title: string; subtitle: string; right?: React.ReactNode }> = ({ title, subtitle, right }) => {
  const { theme } = useNova();
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <h2 className="text-base font-medium tracking-tight" style={{ color: theme.palette.textPrimary }}>
          {title}
        </h2>
        <p className="text-xs leading-relaxed mt-0.5" style={{ color: theme.palette.textSecondary }}>
          {subtitle}
        </p>
      </div>
      {right}
    </div>
  );
};

export const Card: React.FC<{ title?: string; right?: React.ReactNode; children: React.ReactNode; className?: string }> = ({
  title,
  right,
  children,
  className = '',
}) => {
  const { theme } = useNova();
  return (
    <div
      className={`p-5 rounded-2xl border space-y-4 transition-all duration-300 ${className}`}
      style={{
        backgroundColor: theme.palette.bgElevated,
        borderColor: theme.palette.glassBorder,
        boxShadow: `0 10px 25px -5px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
      }}
    >
      {(title || right) && (
        <div className="flex items-center justify-between gap-3">
          {title && (
            <span className="text-[11px] font-mono uppercase tracking-wider block opacity-70" style={{ color: theme.palette.accent }}>
              {title}
            </span>
          )}
          {right}
        </div>
      )}
      {children}
    </div>
  );
};

export const Row: React.FC<{ label: string; hint?: React.ReactNode; children?: React.ReactNode; first?: boolean }> = ({
  label,
  hint,
  children,
  first,
}) => {
  const { theme } = useNova();
  return (
    <div
      className={`flex items-center justify-between gap-4 ${first ? '' : 'pt-3 border-t'}`}
      style={{ borderColor: theme.palette.glassBorder }}
    >
      <div className="min-w-0">
        <div className="text-xs font-medium" style={{ color: theme.palette.textPrimary }}>
          {label}
        </div>
        {hint && (
          <div className="text-[11px] opacity-60 mt-0.5" style={{ color: theme.palette.textSecondary }}>
            {hint}
          </div>
        )}
      </div>
      {children && <div className="shrink-0">{children}</div>}
    </div>
  );
};

export const Toggle: React.FC<{ checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label: string }> = ({
  checked,
  onChange,
  disabled,
  label,
}) => {
  const { theme } = useNova();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="relative w-9 h-5 rounded-full border transition-all cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
      style={{
        backgroundColor: checked ? theme.palette.accent : theme.palette.glassSurface,
        borderColor: checked ? theme.palette.accentBorder : theme.palette.glassBorder,
      }}
    >
      <span
        className="absolute top-0.5 w-3.5 h-3.5 rounded-full transition-all"
        style={{
          left: checked ? 'calc(100% - 1.05rem)' : '0.15rem',
          backgroundColor: checked ? theme.palette.bgBase : theme.palette.textMuted,
        }}
      />
    </button>
  );
};

export function Select<T extends string>({
  value,
  options,
  onChange,
  label,
  disabled,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
  disabled?: boolean;
}) {
  const { theme } = useNova();
  return (
    <select
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as T)}
      className="text-xs px-2.5 py-2 rounded-xl border outline-none font-sans cursor-pointer disabled:opacity-40"
      style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.bgElevated }}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export const TextField: React.FC<{
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  label: string;
  multiline?: boolean;
  maxLength?: number;
}> = ({ value, onChange, placeholder, label, multiline, maxLength }) => {
  const { theme } = useNova();
  const style = { borderColor: theme.palette.glassBorder, color: theme.palette.textPrimary, backgroundColor: theme.palette.glassSurface };
  const cls = 'w-full text-xs p-2.5 rounded-xl border outline-none font-sans placeholder:opacity-40 select-text';
  return multiline ? (
    <textarea aria-label={label} value={value} maxLength={maxLength} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} rows={6} className={`${cls} resize-y leading-relaxed`} style={style} />
  ) : (
    <input aria-label={label} type="text" value={value} maxLength={maxLength} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} className={cls} style={style} />
  );
};

export const Button: React.FC<{
  onClick: () => void;
  children: React.ReactNode;
  tone?: 'default' | 'accent' | 'danger';
  disabled?: boolean;
  title?: string;
}> = ({ onClick, children, tone = 'default', disabled, title }) => {
  const { theme } = useNova();
  const colors =
    tone === 'accent'
      ? { backgroundColor: theme.palette.accent, color: theme.isDark ? theme.palette.bgBase : '#ffffff', borderColor: theme.palette.accentBorder }
      : tone === 'danger'
        ? { backgroundColor: 'rgba(239, 68, 68, 0.12)', color: '#ef4444', borderColor: 'rgba(239, 68, 68, 0.35)' }
        : { backgroundColor: theme.palette.bgElevated, color: theme.palette.textSecondary, borderColor: theme.palette.glassBorder };
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="px-3.5 py-1.5 rounded-xl border text-xs font-medium transition-all hover:brightness-110 active:scale-[0.98] cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
      style={colors}
    >
      {children}
    </button>
  );
};

/**
 * A control the design has but NOVA's backend does not implement yet. Shown,
 * so nothing is silently missing, but plainly inert and saying why.
 */
export const NotConnected: React.FC<{ items: { label: string; why: string }[] }> = ({ items }) => {
  const { theme } = useNova();
  if (!items.length) return null;
  return (
    <div className="p-4 rounded-2xl border border-dashed space-y-2" style={{ borderColor: theme.palette.glassBorder }}>
      <div className="text-[10px] font-mono uppercase tracking-wider opacity-60" style={{ color: theme.palette.textMuted }}>
        Not connected yet
      </div>
      {items.map((i) => (
        <div key={i.label} className="flex items-start gap-2 text-[11px]" style={{ color: theme.palette.textSecondary }}>
          <i className="fa-solid fa-plug-circle-xmark text-[10px] mt-0.5 opacity-50" />
          <span>
            <span className="font-medium" style={{ color: theme.palette.textPrimary }}>
              {i.label}
            </span>{' '}
            <span className="opacity-70">— {i.why}</span>
          </span>
        </div>
      ))}
    </div>
  );
};

export const Stat: React.FC<{ label: string; value: React.ReactNode; tone?: 'ok' | 'warn' | 'bad' | 'muted' }> = ({ label, value, tone }) => {
  const { theme } = useNova();
  const color =
    tone === 'ok' ? '#10b981' : tone === 'warn' ? '#f59e0b' : tone === 'bad' ? '#ef4444' : tone === 'muted' ? theme.palette.textMuted : theme.palette.textPrimary;
  return (
    <div className="p-3 rounded-xl border" style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}>
      <div className="text-[10px] font-mono uppercase tracking-wider opacity-60" style={{ color: theme.palette.textMuted }}>
        {label}
      </div>
      <div className="text-sm font-medium mt-1 font-mono truncate" style={{ color }}>
        {value}
      </div>
    </div>
  );
};

/** A small JSON fetch hook for sections that show one backend resource. */
export function useResource<T>(load: () => Promise<T>, deps: React.DependencyList = [], refreshMs = 0) {
  const [data, setData] = React.useState<T | null>(null);
  const [error, setError] = React.useState('');
  const [loading, setLoading] = React.useState(true);
  const run = React.useCallback(async () => {
    setLoading(true);
    try {
      setData(await load());
      setError('');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  React.useEffect(() => {
    run();
    if (!refreshMs) return;
    const t = setInterval(() => !document.hidden && run(), refreshMs);
    return () => clearInterval(t);
  }, [run, refreshMs]);
  return { data, error, loading, reload: run };
}

export const Loading: React.FC<{ error?: string; what: string }> = ({ error, what }) => {
  const { theme } = useNova();
  return (
    <div className="text-[11px] py-3" style={{ color: error ? '#ef4444' : theme.palette.textMuted }}>
      {error ? `Could not load ${what}: ${error}` : `Loading ${what}…`}
    </div>
  );
};
