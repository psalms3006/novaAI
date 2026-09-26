// Interface state: which screen is up, the theme and glass material, toasts
// and modals. NOVA's own state (voice, tasks, memory, telemetry) is not here
// -- it lives in nova/runtime.tsx and comes from the backend.
//
// Preferences are persisted through the backend (settings.json `ui_prefs`),
// not localStorage: the desktop window runs in pywebview's private mode,
// which wipes browser storage at every launch.

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { getJSON, postJSON } from '../nova/api';
import { NOVA_THEMES } from '../theme/themes';
import type { NotificationToast, PresenceType, ScreenMode, ThemeDefinition, ThemeId } from '../types/nova';

export type GlassQuality = 'quality' | 'balanced' | 'performance';

export interface UiPrefs {
  theme_id: ThemeId;
  glass_opacity: number; // 30..95, % of the theme's own glass alpha at 76
  glass_blur: number; // 8..40 px, the main panels' backdrop blur
  specular: number; // 0..100
  ui_scale: '90' | '100' | '110';
  corner_radius: 'sharp' | 'soft' | 'round';
  ambient_motion: boolean;
  quality: GlassQuality;
  landing_view: ScreenMode;
  time_format: '12h' | '24h';
  presence: PresenceType;
  show_transcript: boolean;
}

/** Weaker machines start on "balanced"; the user can always change it. */
function defaultQuality(): GlassQuality {
  const cores = navigator.hardwareConcurrency || 8;
  const mem = (navigator as Navigator & { deviceMemory?: number }).deviceMemory || 8;
  return cores <= 4 || mem <= 4 ? 'balanced' : 'quality';
}

const DEFAULT_PREFS: UiPrefs = {
  theme_id: 'warm-graphite',
  glass_opacity: 76,
  glass_blur: 28,
  specular: 85,
  ui_scale: '100',
  corner_radius: 'soft',
  ambient_motion: !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches,
  quality: defaultQuality(),
  landing_view: 'substrate',
  time_format: '12h',
  presence: 'orb',
  show_transcript: true,
};

/** How much each quality preset scales blur, and how many particles the orb draws. */
export const QUALITY = {
  quality: { blur: 1, particles: 12000, pixelRatio: 2, antialias: true, fps: 60 },
  balanced: { blur: 0.65, particles: 7000, pixelRatio: 1.5, antialias: true, fps: 60 },
  performance: { blur: 0.35, particles: 3500, pixelRatio: 1, antialias: false, fps: 30 },
} as const;

/** Scale the alpha of an rgba() colour; hex and anything else pass through. */
function scaleAlpha(color: string, factor: number): string {
  const m = color.match(/^rgba\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)$/);
  if (!m) return color;
  const a = Math.max(0, Math.min(1, Number(m[4]) * factor));
  return `rgba(${m[1]}, ${m[2]}, ${m[3]}, ${a.toFixed(3)})`;
}

/** The theme as rendered: the palette with the user's glass settings applied. */
function renderTheme(base: ThemeDefinition, prefs: UiPrefs): ThemeDefinition {
  const opacity = prefs.glass_opacity / 76;
  const spec = prefs.specular / 85;
  return {
    ...base,
    palette: {
      ...base.palette,
      glassSurface: scaleAlpha(base.palette.glassSurface, opacity),
      glassHighlight: scaleAlpha(base.palette.glassHighlight, spec),
      specularRim: scaleAlpha(base.palette.specularRim, spec),
      specularInner: scaleAlpha(base.palette.specularInner, spec),
    },
  };
}

interface NovaStateContextType {
  currentScreen: ScreenMode;
  setCurrentScreen: (screen: ScreenMode) => void;
  presenceType: PresenceType;
  setPresenceType: (presence: PresenceType) => void;

  theme: ThemeDefinition;
  themeId: ThemeId;
  setThemeId: (id: ThemeId) => void;
  glassOpacity: number;
  setGlassOpacity: (opacity: number) => void;

  prefs: UiPrefs;
  prefsLoaded: boolean;
  setPref: <K extends keyof UiPrefs>(key: K, value: UiPrefs[K]) => void;
  resetPrefs: () => void;
  quality: (typeof QUALITY)[GlassQuality];

  toasts: NotificationToast[];
  addToast: (title: string, description?: string, type?: NotificationToast['type']) => void;
  dismissToast: (id: string) => void;

  commandPaletteOpen: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
  themeModalOpen: boolean;
  setThemeModalOpen: (open: boolean) => void;
}

const NovaStateContext = createContext<NovaStateContextType | null>(null);

export const NovaStateProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [prefs, setPrefs] = useState<UiPrefs>(DEFAULT_PREFS);
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const [currentScreen, setCurrentScreen] = useState<ScreenMode>('substrate');
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const [themeModalOpen, setThemeModalOpen] = useState(false);
  const [toasts, setToasts] = useState<NotificationToast[]>([]);
  const toastTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  // ── preferences: load once, save changes (debounced, one request per burst)
  useEffect(() => {
    let cancelled = false;
    getJSON<{ settings: { ui_prefs?: Partial<UiPrefs> } }>('/api/settings', 8000)
      .then((j) => {
        if (cancelled) return;
        const saved = j.settings?.ui_prefs || {};
        const merged = { ...DEFAULT_PREFS, ...saved };
        setPrefs(merged);
        setCurrentScreen(merged.landing_view);
      })
      .catch(() => {
        /* the backend is not up yet: defaults until it is */
      })
      .finally(() => !cancelled && setPrefsLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  const pending = useRef<Partial<UiPrefs>>({});
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flush = useCallback(() => {
    saveTimer.current = null;
    const patch = pending.current;
    pending.current = {};
    if (Object.keys(patch).length) {
      postJSON('/api/settings', { ui_prefs: patch }).catch(() => {
        // Keep it for the next change rather than lose it silently.
        pending.current = { ...patch, ...pending.current };
      });
    }
  }, []);

  const setPref = useCallback(
    <K extends keyof UiPrefs>(key: K, value: UiPrefs[K]) => {
      setPrefs((p) => (p[key] === value ? p : { ...p, [key]: value }));
      pending.current = { ...pending.current, [key]: value };
      if (saveTimer.current) clearTimeout(saveTimer.current);
      // Sliders fire on every pixel; persist once the hand stops.
      saveTimer.current = setTimeout(flush, 400);
    },
    [flush],
  );

  useEffect(
    () => () => {
      if (saveTimer.current) {
        clearTimeout(saveTimer.current);
        flush();
      }
    },
    [flush],
  );

  const resetPrefs = useCallback(() => {
    setPrefs(DEFAULT_PREFS);
    pending.current = { ...DEFAULT_PREFS };
    flush();
  }, [flush]);

  // ── theme and glass, applied once at the root ───────────────────────────
  const baseTheme = NOVA_THEMES[prefs.theme_id] || NOVA_THEMES['warm-graphite'];
  const theme = useMemo(() => renderTheme(baseTheme, prefs), [baseTheme, prefs]);
  const quality = QUALITY[prefs.quality];

  useEffect(() => {
    const root = document.documentElement;
    const p = theme.palette;
    // Colour tokens, for CSS that cannot read React context.
    const tokens: Record<string, string> = {
      '--nova-bg': p.bgBase,
      '--nova-bg-elevated': p.bgElevated,
      '--nova-glass': p.glassSurface,
      '--nova-glass-border': p.glassBorder,
      '--nova-glass-highlight': p.glassHighlight,
      '--nova-text': p.textPrimary,
      '--nova-text-secondary': p.textSecondary,
      '--nova-text-muted': p.textMuted,
      '--nova-accent': p.accent,
    };
    for (const [k, v] of Object.entries(tokens)) root.style.setProperty(k, v);
    // Tailwind v4's backdrop-blur-* utilities read these theme variables, so
    // setting them here restyles every glass panel at once.
    const blur = prefs.glass_blur * quality.blur;
    root.style.setProperty('--blur-xl', `${Math.round(blur * 0.6)}px`);
    root.style.setProperty('--blur-2xl', `${Math.round(blur)}px`);
    root.style.setProperty('--blur-3xl', `${Math.round(blur * 1.4)}px`);
    const radius = { sharp: 0.5, soft: 1, round: 1.4 }[prefs.corner_radius];
    root.style.setProperty('--radius-xl', `${(0.75 * radius).toFixed(3)}rem`);
    root.style.setProperty('--radius-2xl', `${(1 * radius).toFixed(3)}rem`);
    root.style.setProperty('--radius-3xl', `${(1.5 * radius).toFixed(3)}rem`);
    root.style.colorScheme = theme.isDark ? 'dark' : 'light';
    root.style.backgroundColor = p.bgBase;
    (root.style as CSSStyleDeclaration & { zoom: string }).zoom = String(Number(prefs.ui_scale) / 100);
    root.dataset.quality = prefs.quality;
    root.classList.toggle('nova-still', !prefs.ambient_motion);
  }, [theme, prefs, quality]);

  // ── toasts ───────────────────────────────────────────────────────────────
  const dismissToast = useCallback((id: string) => {
    const t = toastTimers.current.get(id);
    if (t) clearTimeout(t);
    toastTimers.current.delete(id);
    setToasts((prev) => prev.filter((x) => x.id !== id));
  }, []);

  const addToast = useCallback(
    (title: string, description?: string, type: NotificationToast['type'] = 'info') => {
      const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
      const toast: NotificationToast = {
        id,
        title,
        description,
        type,
        timestamp: new Date().toLocaleTimeString([], { hour12: prefs.time_format === '12h' }),
      };
      setToasts((prev) => [toast, ...prev.slice(0, 4)]);
      toastTimers.current.set(id, setTimeout(() => dismissToast(id), 4500));
    },
    [dismissToast, prefs.time_format],
  );

  useEffect(() => {
    const timers = toastTimers.current;
    return () => timers.forEach((t) => clearTimeout(t));
  }, []);

  const value: NovaStateContextType = {
    currentScreen,
    setCurrentScreen,
    presenceType: prefs.presence,
    setPresenceType: (v) => setPref('presence', v),
    theme,
    themeId: prefs.theme_id,
    setThemeId: (v) => setPref('theme_id', v),
    glassOpacity: prefs.glass_opacity,
    setGlassOpacity: (v) => setPref('glass_opacity', v),
    prefs,
    prefsLoaded,
    setPref,
    resetPrefs,
    quality,
    toasts,
    addToast,
    dismissToast,
    commandPaletteOpen,
    setCommandPaletteOpen,
    themeModalOpen,
    setThemeModalOpen,
  };

  return <NovaStateContext.Provider value={value}>{children}</NovaStateContext.Provider>;
};

export const useNova = () => {
  const ctx = useContext(NovaStateContext);
  if (!ctx) throw new Error('useNova must be used within NovaStateProvider');
  return ctx;
};
