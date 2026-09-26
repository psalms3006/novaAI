import React from 'react';
import { useNova } from '../context/NovaStateContext';
import { NOVA_THEMES } from '../theme/themes';
import { ThemeId } from '../types/nova';

export const ThemeSelectorModal: React.FC = () => {
  const { themeModalOpen, setThemeModalOpen, themeId, setThemeId, glassOpacity, setGlassOpacity, addToast, theme } = useNova();

  if (!themeModalOpen) return null;

  const handleSelect = (id: ThemeId) => {
    setThemeId(id);
    addToast('Theme changed', NOVA_THEMES[id].name, 'info');
  };

  const themeList = Object.values(NOVA_THEMES);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-xl animate-fade-in">
      <div
        className="w-full max-w-2xl rounded-3xl border shadow-2xl overflow-hidden transition-all duration-300 flex flex-col"
        style={{
          backgroundColor: theme.palette.bgElevated,
          borderColor: theme.palette.glassBorder,
          boxShadow: `0 25px 50px -12px rgba(0, 0, 0, 0.5), inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
        }}
      >
        {/* Header */}
        <div className="px-6 py-4 border-b flex items-center justify-between" style={{ borderColor: theme.palette.glassBorder }}>
          <div>
            <span className="text-[11px] font-mono tracking-widest uppercase opacity-60" style={{ color: theme.palette.accent }}>
              Environmental Architecture
            </span>
            <h2 className="text-lg font-medium tracking-tight mt-0.5" style={{ color: theme.palette.textPrimary }}>
              NOVA Product Identity Themes
            </h2>
          </div>
          <button
            onClick={() => setThemeModalOpen(false)}
            className="w-8 h-8 rounded-full flex items-center justify-center opacity-60 hover:opacity-100 transition-opacity cursor-pointer"
            style={{ backgroundColor: 'rgba(255,255,255,0.05)' }}
          >
            <i className="fa-solid fa-xmark text-sm" />
          </button>
        </div>

        {/* Content: Theme grid */}
        <div className="p-6 space-y-4 max-h-[65vh] overflow-y-auto">
          <p className="text-xs leading-relaxed" style={{ color: theme.palette.textSecondary }}>
            Choose an overarching environmental palette. Each theme re-calibrates lighting, physical glass material transmission, typography contrast, and 3D particulate luminescence.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5 pt-1">
            {themeList.map((t) => {
              const isSelected = themeId === t.id;
              return (
                <div
                  key={t.id}
                  onClick={() => handleSelect(t.id)}
                  className={`p-4 rounded-2xl border transition-all cursor-pointer group flex flex-col justify-between relative overflow-hidden ${
                    isSelected ? 'ring-1 scale-[1.01]' : 'hover:scale-[1.008]'
                  }`}
                  style={{
                    backgroundColor: t.palette.bgElevated,
                    borderColor: isSelected ? t.palette.accent : t.palette.glassBorder,
                    boxShadow: isSelected
                      ? `0 10px 25px -5px ${t.palette.glow}, inset 0 1px 1px 0 ${t.palette.glassHighlight}`
                      : 'none',
                  }}
                >
                  <div>
                    {/* Top row: Name & badge */}
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <div
                          className="w-3.5 h-3.5 rounded-full border flex items-center justify-center"
                          style={{ borderColor: isSelected ? t.palette.accent : t.palette.glassBorder }}
                        >
                          {isSelected && (
                            <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: t.palette.accent }} />
                          )}
                        </div>
                        <span className="text-sm font-medium font-sans" style={{ color: t.palette.textPrimary }}>
                          {t.name}
                        </span>
                      </div>
                      <span
                        className="text-[9px] font-mono px-2 py-0.5 rounded-full border"
                        style={{
                          backgroundColor: t.palette.glassSurface,
                          borderColor: t.palette.glassBorder,
                          color: t.palette.textSecondary,
                        }}
                      >
                        {t.isDark ? 'Dark Spec' : 'Liquid Pearl'}
                      </span>
                    </div>

                    <p className="text-[11px] leading-snug mb-3.5" style={{ color: t.palette.textSecondary }}>
                      {t.tagline}
                    </p>
                  </div>

                  {/* Swatch strip */}
                  <div className="flex items-center gap-1.5 pt-2 border-t" style={{ borderColor: t.palette.glassBorder }}>
                    <span className="text-[9px] font-mono opacity-50 mr-1" style={{ color: t.palette.textSecondary }}>
                      Base
                    </span>
                    <span className="w-4 h-4 rounded-md border" style={{ backgroundColor: t.palette.bgBase, borderColor: t.palette.glassBorder }} />
                    <span className="w-4 h-4 rounded-md border" style={{ backgroundColor: t.palette.bgElevated, borderColor: t.palette.glassBorder }} />
                    <span className="w-4 h-4 rounded-md border" style={{ backgroundColor: t.palette.glassSurface, borderColor: t.palette.glassBorder }} />
                    <span className="w-4 h-4 rounded-md border" style={{ backgroundColor: t.palette.accent, borderColor: t.palette.glassBorder }} />
                    <span className="w-4 h-4 rounded-md border" style={{ backgroundColor: t.palette.orbParticlePrimary, borderColor: t.palette.glassBorder }} />
                  </div>
                </div>
              );
            })}
          </div>

          {/* Quick Glass Material slider */}
          <div
            className="p-4 rounded-2xl border space-y-2 mt-4"
            style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder }}
          >
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium" style={{ color: theme.palette.textPrimary }}>
                Acrylic Material Density
              </span>
              <span className="font-mono text-xs font-semibold" style={{ color: theme.palette.accent }}>
                {glassOpacity}% transmission
              </span>
            </div>
            <input
              type="range"
              min="30"
              max="95"
              value={glassOpacity}
              onChange={(e) => setGlassOpacity(Number(e.target.value))}
              className="w-full h-1.5 rounded-full appearance-none cursor-pointer accent-slate-400 bg-white/10"
            />
            <div className="flex justify-between text-[9px] font-mono opacity-60" style={{ color: theme.palette.textMuted }}>
              <span>30% Ethereal (Liquid)</span>
              <span>Subsurface Glass Transmission</span>
              <span>95% Dense Material</span>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div
          className="px-6 py-3.5 border-t flex items-center justify-between text-xs"
          style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}
        >
          <span className="text-[10px] font-mono opacity-50" style={{ color: theme.palette.textSecondary }}>
            Current: {theme.name} · saved with NOVA
          </span>
          <button
            onClick={() => setThemeModalOpen(false)}
            className="px-4 py-1.5 rounded-xl font-medium text-xs transition-all cursor-pointer border"
            style={{
              backgroundColor: theme.palette.accent,
              color: theme.isDark ? '#0a0b10' : '#ffffff',
              borderColor: theme.palette.accentBorder,
            }}
          >
            Apply &amp; Return
          </button>
        </div>
      </div>
    </div>
  );
};
