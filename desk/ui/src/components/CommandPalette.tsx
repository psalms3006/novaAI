import React, { useEffect, useMemo, useState } from 'react';
import { useNova } from '../context/NovaStateContext';
import { useNovaSettings } from '../context/NovaSettingsContext';
import { useRuntime } from '../nova/runtime';
import { NOVA_THEMES } from '../theme/themes';
import type { SettingsSectionId } from '../types/settings';
import type { ThemeId } from '../types/nova';

interface Command {
  id: string;
  category: string;
  title: string;
  icon: string;
  run: () => void;
  disabled?: boolean;
}

/** Ctrl+K. Every command here does something real; typing anything else asks NOVA. */
export const CommandPalette: React.FC = () => {
  const { commandPaletteOpen, setCommandPaletteOpen, setCurrentScreen, setThemeId, theme, addToast } = useNova();
  const { setActiveSection } = useNovaSettings();
  const rt = useRuntime();
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);

  useEffect(() => {
    if (commandPaletteOpen) {
      setQuery('');
      setCursor(0);
    }
  }, [commandPaletteOpen]);

  const close = () => setCommandPaletteOpen(false);
  const settings = (id: SettingsSectionId) => () => {
    setActiveSection(id);
    setCurrentScreen('runtime');
  };

  const commands: Command[] = useMemo(
    () => [
      { id: 'voice', category: 'Voice', title: rt.voiceRunning ? 'Stop voice' : 'Start voice', icon: 'fa-power-off', run: rt.toggleVoice },
      { id: 'mute', category: 'Voice', title: rt.muted ? 'Unmute microphone' : 'Mute microphone', icon: rt.muted ? 'fa-microphone' : 'fa-microphone-slash', run: () => rt.setMuted(!rt.muted), disabled: !rt.voiceRunning },
      { id: 'interrupt', category: 'Voice', title: 'Interrupt her', icon: 'fa-stop', run: rt.interrupt, disabled: rt.phase !== 'speaking' },
      {
        id: 'halt',
        category: 'Control',
        title: 'Stop NOVA now (speech, reply, microphone)',
        icon: 'fa-hand',
        run: async () => {
          await rt.halt();
          addToast('NOVA stopped', 'Speech and replies stopped; microphone muted.', 'warning');
        },
      },
      { id: 'nav-presence', category: 'Go to', title: 'Presence', icon: 'fa-shapes', run: () => setCurrentScreen('substrate') },
      { id: 'nav-map', category: 'Go to', title: 'Memory map', icon: 'fa-diagram-project', run: () => setCurrentScreen('synaptic') },
      { id: 'nav-library', category: 'Go to', title: 'Library — conversations, documents, projects, files', icon: 'fa-book-open', run: () => setCurrentScreen('library') },
      { id: 'set-account', category: 'Settings', title: 'Account & access (API key, offline, NOVA Cloud)', icon: 'fa-key', run: settings('account') },
      { id: 'set-permissions', category: 'Settings', title: 'Permissions', icon: 'fa-shield-halved', run: settings('permissions') },
      { id: 'set-memory', category: 'Settings', title: 'Memory', icon: 'fa-database', run: settings('memory') },
      { id: 'set-voice', category: 'Settings', title: 'Voice session', icon: 'fa-microphone', run: settings('voice') },
      { id: 'set-models', category: 'Settings', title: 'Models & offline', icon: 'fa-microchip', run: settings('intelligence') },
      { id: 'set-tools', category: 'Settings', title: 'Tools & tasks', icon: 'fa-toolbox', run: settings('tools') },
      { id: 'set-skills', category: 'Settings', title: 'Skills NOVA has learned', icon: 'fa-graduation-cap', run: settings('skills') },
      { id: 'set-knowledge', category: 'Settings', title: 'Knowledge NOVA has learned', icon: 'fa-book-open', run: settings('knowledge') },
      { id: 'set-diag', category: 'Settings', title: 'Diagnostics & event stream', icon: 'fa-heart-pulse', run: settings('diagnostics') },
      { id: 'set-appearance', category: 'Settings', title: 'Appearance', icon: 'fa-palette', run: settings('appearance') },
      ...Object.values(NOVA_THEMES).map((t) => ({
        id: `theme-${t.id}`,
        category: 'Theme',
        title: `${t.name} — ${t.tagline}`,
        icon: 'fa-palette',
        run: () => setThemeId(t.id as ThemeId),
      })),
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rt.voiceRunning, rt.muted, rt.phase],
  );

  const q = query.trim().toLowerCase();
  const filtered = commands.filter((c) => !c.disabled && (!q || c.title.toLowerCase().includes(q) || c.category.toLowerCase().includes(q)));
  const asks = q.length > 0;
  const items: Command[] = asks
    ? [...filtered, { id: 'ask', category: 'Ask NOVA', title: `Ask NOVA: “${query.trim()}”`, icon: 'fa-paper-plane', run: () => rt.sendText(query.trim()) }]
    : filtered;

  if (!commandPaletteOpen) return null;

  const pick = (c: Command | undefined) => {
    if (!c) return;
    close();
    c.run();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((i) => Math.min(items.length - 1, i + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((i) => Math.max(0, i - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      pick(items[Math.min(cursor, items.length - 1)]);
    } else if (e.key === 'Escape') {
      close();
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 backdrop-blur-xl animate-fade-in" style={{ backgroundColor: 'rgba(0,0,0,0.55)' }} onClick={close}>
      <div
        className="w-full max-w-lg rounded-2xl border shadow-2xl overflow-hidden flex flex-col font-sans transition-all duration-300"
        style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder, boxShadow: `0 25px 50px -12px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}` }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Commands"
      >
        <div className="flex items-center px-4 py-3.5 border-b gap-3" style={{ borderColor: theme.palette.glassBorder }}>
          <i className="fa-solid fa-magnifying-glass text-xs opacity-50" style={{ color: theme.palette.accent }} />
          <input
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setCursor(0);
            }}
            onKeyDown={onKey}
            autoFocus
            placeholder="Type a command, or anything to ask NOVA…"
            aria-label="Command"
            className="bg-transparent outline-none w-full text-xs font-sans placeholder:opacity-40 select-text"
            style={{ color: theme.palette.textPrimary }}
          />
          <kbd className="px-1.5 py-0.5 rounded text-[10px] border font-mono opacity-50" style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder, color: theme.palette.textSecondary }}>
            ESC
          </kbd>
        </div>

        <div className="max-h-80 overflow-y-auto p-2 space-y-1" role="listbox">
          {items.map((cmd, i) => {
            const active = i === Math.min(cursor, items.length - 1);
            return (
              <div
                key={cmd.id}
                role="option"
                aria-selected={active}
                onClick={() => pick(cmd)}
                onMouseEnter={() => setCursor(i)}
                className="flex items-center justify-between p-2.5 rounded-xl cursor-pointer transition-all"
                style={{ backgroundColor: active ? theme.palette.glassSurface : 'transparent', color: active ? theme.palette.textPrimary : theme.palette.textSecondary }}
              >
                <div className="flex items-center gap-3 min-w-0">
                  <div className="w-6 h-6 rounded-lg border flex items-center justify-center text-xs shrink-0" style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder }}>
                    <i className={`fa-solid ${cmd.icon} text-[10px]`} />
                  </div>
                  <span className="text-xs font-medium truncate">{cmd.title}</span>
                </div>
                <span className="text-[9px] font-mono uppercase tracking-wider opacity-40 shrink-0">{cmd.category}</span>
              </div>
            );
          })}
        </div>

        <div className="px-4 py-2.5 border-t flex items-center justify-between text-[10px] font-mono opacity-50" style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textMuted }}>
          <span>↑↓ choose · Enter run</span>
          <span>1 · 2 · 3 · 4 switch screens</span>
        </div>
      </div>
    </div>
  );
};
