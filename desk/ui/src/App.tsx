import { useEffect } from 'react';
import { NovaStateProvider, useNova } from './context/NovaStateContext';
import { NovaSettingsProvider } from './context/NovaSettingsContext';
import { NovaRuntimeProvider, useRuntime } from './nova/runtime';
import { VERSION } from './nova/api';
import { TopHud } from './components/TopHud';
import { NavRail } from './components/NavRail';
import { VoicePill } from './components/VoicePill';
import { CommandPalette } from './components/CommandPalette';
import { ThemeSelectorModal } from './components/ThemeSelectorModal';
import { NotificationToasts } from './components/NotificationToasts';
import { ConfirmDialog } from './components/ConfirmDialog';
import { AmbientView } from './components/AmbientView';
import { Onboarding } from './components/Onboarding';
import { FirstRun } from './components/FirstRun';
import { PresenceScreen } from './screens/PresenceScreen';
import { SettingsScreen } from './screens/settings/SettingsScreen';
import { SynapticMapScreen } from './screens/SynapticMapScreen';
import { LibraryScreen } from './screens/LibraryScreen';

/** The desktop launcher opens a second, 44 px window at /?mode=ambient. */
const AMBIENT = new URLSearchParams(window.location.search).get('mode') === 'ambient';

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  const tag = el?.tagName?.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || Boolean(el?.isContentEditable);
}

function AppContent() {
  const { currentScreen, setCurrentScreen, theme, setCommandPaletteOpen, commandPaletteOpen, themeModalOpen } = useNova();
  const rt = useRuntime();

  // Keyboard: Ctrl+K commands · 1/2/3/4 screens · Ctrl+, settings ·
  // Space microphone (start voice, or mute/unmute) · Esc interrupt her.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setCommandPaletteOpen(!commandPaletteOpen);
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === ',') {
        e.preventDefault();
        setCurrentScreen('runtime');
        return;
      }
      if (isTyping(e.target) || commandPaletteOpen || themeModalOpen || rt.pendingConfirm) return;
      if (e.key === 'Escape' && rt.phase === 'speaking') {
        e.preventDefault();
        rt.interrupt();
      } else if (e.key === '1') setCurrentScreen('substrate');
      else if (e.key === '2') setCurrentScreen('synaptic');
      else if (e.key === '3') setCurrentScreen('runtime');
      else if (e.key === '4') setCurrentScreen('library');
      else if (e.code === 'Space' && !e.repeat && rt.backendReachable) {
        e.preventDefault();
        if (rt.voiceRunning) rt.setMuted(!rt.muted);
        else rt.startVoice();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setCurrentScreen, setCommandPaletteOpen, commandPaletteOpen, themeModalOpen, rt]);

  return (
    <div
      className="h-screen max-h-screen w-screen overflow-hidden select-none flex flex-col justify-between relative transition-colors duration-500 font-sans"
      style={{ backgroundColor: theme.palette.bgBase, color: theme.palette.textPrimary }}
    >
      <div className="absolute inset-0 pointer-events-none z-0 overflow-hidden" aria-hidden>
        <div
          className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[720px] h-[640px] rounded-full blur-[140px] pointer-events-none transition-all duration-700 opacity-60"
          style={{ backgroundColor: `${theme.palette.accent}0d` }}
        />
        {currentScreen === 'synaptic' && (
          <div
            className="absolute inset-0 pointer-events-none transition-opacity duration-500"
            style={{ backgroundImage: `radial-gradient(${theme.palette.canvasGridColor} 1px, transparent 1px)`, backgroundSize: '32px 32px' }}
          />
        )}
      </div>

      <TopHud />

      {!rt.backendReachable && (
        <div
          role="alert"
          className="absolute top-12 left-1/2 -translate-x-1/2 z-40 px-4 py-2 rounded-full border text-xs flex items-center gap-2 backdrop-blur-2xl"
          style={{ backgroundColor: 'rgba(239,68,68,0.12)', borderColor: 'rgba(239,68,68,0.35)', color: '#ef4444' }}
        >
          <i className="fa-solid fa-plug-circle-xmark" />
          NOVA's backend is not responding. Retrying…
        </div>
      )}

      <main className="flex-1 relative w-full h-full min-h-0 overflow-hidden flex items-center justify-center z-10">
        <div className="absolute left-6 top-6 bottom-8 pointer-events-auto z-20 flex">
          <NavRail />
        </div>
        {currentScreen === 'substrate' && <PresenceScreen />}
        {currentScreen === 'runtime' && <SettingsScreen />}
        {currentScreen === 'synaptic' && <SynapticMapScreen />}
        {currentScreen === 'library' && <LibraryScreen />}
        {currentScreen === 'substrate' && <VoicePill />}
      </main>

      <footer
        className="h-6 px-6 flex items-center justify-between text-[10px] font-mono opacity-50 z-20 pointer-events-none border-t transition-colors duration-300"
        style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textMuted, backgroundColor: theme.palette.glassSurface }}
      >
        <span>
          NOVA {rt.status?.version || VERSION} · {rt.status ? (rt.status.online ? 'online' : 'offline — local intelligence') : 'connecting'}
        </span>
        <span>OMNIEL</span>
      </footer>

      <CommandPalette />
      <ThemeSelectorModal />
      <NotificationToasts />
      <ConfirmDialog />
      <Onboarding />
      <FirstRun />
    </div>
  );
}

function AmbientContent() {
  return <AmbientView />;
}

export default function App() {
  return (
    <NovaStateProvider>
      <NovaRuntimeProvider ambient={AMBIENT}>
        {AMBIENT ? (
          <AmbientContent />
        ) : (
          <NovaSettingsProvider>
            <AppContent />
          </NovaSettingsProvider>
        )}
      </NovaRuntimeProvider>
    </NovaStateProvider>
  );
}
