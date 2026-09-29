import React from 'react';
import { useNovaSettings, SETTINGS_GROUPS } from '../../context/NovaSettingsContext';
import { useNova } from '../../context/NovaStateContext';
import { SettingsSectionId } from '../../types/settings';
import { VERSION } from '../../nova/api';
import { useRuntime } from '../../nova/runtime';
import { GeneralSection } from './sections/GeneralSection';
import { AppearanceSection } from './sections/AppearanceSection';
import { ConversationSection } from './sections/ConversationSection';
import { VoiceSection } from './sections/VoiceSection';
import { IntelligenceSection } from './sections/IntelligenceSection';
import { MemorySection } from './sections/MemorySection';
import { PersonalitySection } from './sections/PersonalitySection';
import { IdentitySection } from './sections/IdentitySection';
import { PermissionsSection } from './sections/PermissionsSection';
import { ToolsSection } from './sections/ToolsSection';
import { SkillsSection } from './sections/SkillsSection';
import { ConnectionsSection } from './sections/ConnectionsSection';
import { AccountSection } from './sections/AccountSection';
import { DiagnosticsSection } from './sections/DiagnosticsSection';
import { AboutSection } from './sections/AboutSection';

const SAVE_LABEL = { idle: 'SAVED WITH NOVA', saving: 'SAVING…', saved: 'SAVED', error: 'NOT SAVED — BACKEND REFUSED' } as const;

export const SettingsScreen: React.FC = () => {
  const { activeSection, setActiveSection, searchQuery, setSearchQuery, saveState, loadError } = useNovaSettings();
  const { theme, setCurrentScreen } = useNova();
  const { status, backendReachable } = useRuntime();

  // Filter sections by search query
  const searchResults = React.useMemo(() => {
    if (!searchQuery.trim()) return null;
    const query = searchQuery.toLowerCase();
    const results: { sectionId: SettingsSectionId; label: string; groupName: string; desc: string }[] = [];

    SETTINGS_GROUPS.forEach((group) => {
      group.sections.forEach((sec) => {
        if (
          sec.label.toLowerCase().includes(query) ||
          sec.description.toLowerCase().includes(query) ||
          group.name.toLowerCase().includes(query) ||
          (sec.keywords || '').includes(query)
        ) {
          results.push({
            sectionId: sec.id,
            label: sec.label,
            groupName: group.name,
            desc: sec.description,
          });
        }
      });
    });
    return results;
  }, [searchQuery]);

  const renderSectionContent = () => {
    switch (activeSection) {
      case 'general':
        return <GeneralSection />;
      case 'appearance':
        return <AppearanceSection />;
      case 'conversation':
        return <ConversationSection />;
      case 'voice':
        return <VoiceSection />;
      case 'intelligence':
        return <IntelligenceSection />;
      case 'memory':
        return <MemorySection />;
      case 'personality':
        return <PersonalitySection />;
      case 'identity':
        return <IdentitySection />;
      case 'permissions':
        return <PermissionsSection />;
      case 'tools':
        return <ToolsSection />;
      case 'skills':
        return <SkillsSection />;
      case 'connections':
        return <ConnectionsSection />;
      case 'account':
        return <AccountSection />;
      case 'diagnostics':
        return <DiagnosticsSection />;
      case 'about':
        return <AboutSection />;
      default:
        return <GeneralSection />;
    }
  };

  const activeGroup = SETTINGS_GROUPS.find((g) => g.sections.some((s) => s.id === activeSection));
  const activeSectionMeta = activeGroup?.sections.find((s) => s.id === activeSection);

  return (
    <div className="relative w-full h-full flex items-center justify-center p-6 overflow-hidden select-none font-sans">
      <div
        className="w-full max-w-6xl h-[calc(100vh-6rem)] rounded-3xl border shadow-2xl backdrop-blur-3xl overflow-hidden flex flex-col md:flex-row transition-all duration-300"
        style={{
          backgroundColor: theme.palette.glassSurface,
          borderColor: theme.palette.glassBorder,
          boxShadow: `0 25px 60px -15px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}`,
        }}
      >
        {/* ================= LEFT SETTINGS SIDEBAR ================= */}
        <aside
          className="w-full md:w-72 border-r flex flex-col shrink-0 overflow-hidden"
          style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.bgElevated }}
        >
          {/* Sidebar Top: Back link & Title */}
          <div className="p-4 border-b space-y-3" style={{ borderColor: theme.palette.glassBorder }}>
            <div className="flex items-center justify-between">
              <button
                onClick={() => setCurrentScreen('substrate')}
                className="flex items-center gap-1.5 text-xs transition-colors hover:opacity-100 cursor-pointer font-medium"
                style={{ color: theme.palette.accent }}
              >
                <i className="fa-solid fa-chevron-left text-[10px]" />
                <span>Back to NOVA</span>
              </button>
              <span className="text-[10px] font-mono opacity-50" style={{ color: theme.palette.textMuted }}>
                SETTINGS
              </span>
            </div>

            {/* Settings Search Field */}
            <div
              className="flex items-center gap-2 px-3 py-1.5 rounded-xl border text-xs"
              style={{ backgroundColor: theme.palette.glassSurface, borderColor: theme.palette.glassBorder }}
            >
              <i className="fa-solid fa-magnifying-glass text-[11px] opacity-50" style={{ color: theme.palette.accent }} />
              <input
                type="text"
                placeholder="Search settings..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full bg-transparent outline-none text-xs font-sans placeholder:opacity-40"
                style={{ color: theme.palette.textPrimary }}
              />
              {searchQuery && (
                <button onClick={() => setSearchQuery('')} className="text-slate-400 hover:text-white text-[10px]">
                  <i className="fa-solid fa-xmark" />
                </button>
              )}
            </div>
          </div>

          {/* Grouped Navigation List */}
          <div className="flex-1 overflow-y-auto p-3 space-y-4">
            {SETTINGS_GROUPS.map((group) => (
              <div key={group.id} className="space-y-1">
                <div
                  className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-wider opacity-60"
                  style={{ color: theme.palette.textMuted }}
                >
                  {group.name}
                </div>
                {group.sections.map((section) => {
                  const isActive = activeSection === section.id;
                  return (
                    <button
                      key={section.id}
                      onClick={() => {
                        setActiveSection(section.id);
                        setSearchQuery('');
                      }}
                      className={`w-full flex items-center justify-between px-2.5 py-2 rounded-xl text-xs transition-all cursor-pointer border text-left ${
                        isActive ? 'font-medium shadow-sm ring-1 ring-white/10' : 'opacity-70 hover:opacity-100 hover:bg-white/[0.02]'
                      }`}
                      style={{
                        backgroundColor: isActive ? theme.palette.glassSurface : 'transparent',
                        borderColor: isActive ? theme.palette.accentBorder : 'transparent',
                        color: isActive ? theme.palette.textPrimary : theme.palette.textSecondary,
                      }}
                    >
                      <div className="flex items-center gap-2.5 truncate">
                        <i className={`fa-solid ${section.icon} text-xs w-4 text-center opacity-70`} style={{ color: isActive ? theme.palette.accent : undefined }} />
                        <span className="truncate">{section.label}</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>

          {/* Sidebar Footer info */}
          <div
            className="p-3 border-t text-[10px] font-mono opacity-50 flex items-center justify-between"
            style={{ borderColor: theme.palette.glassBorder, color: theme.palette.textMuted }}
          >
            <span>NOVA {status?.version || VERSION}</span>
            <span style={{ color: backendReachable ? undefined : '#ef4444' }}>{backendReachable ? 'BACKEND CONNECTED' : 'BACKEND UNREACHABLE'}</span>
          </div>
        </aside>

        {/* ================= RIGHT MAIN CONTENT AREA ================= */}
        <section className="flex-1 flex flex-col min-w-0 overflow-hidden">
          {/* Breadcrumb Header */}
          <div
            className="h-12 px-6 border-b flex items-center justify-between shrink-0"
            style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}
          >
            <div className="flex items-center space-x-2 text-xs">
              <span className="opacity-50" style={{ color: theme.palette.textSecondary }}>
                Settings
              </span>
              <span className="opacity-30">/</span>
              <span className="opacity-60" style={{ color: theme.palette.textSecondary }}>
                {activeGroup?.name || 'Preferences'}
              </span>
              <span className="opacity-30">/</span>
              <span className="font-medium" style={{ color: theme.palette.textPrimary }}>
                {activeSectionMeta?.label || 'General'}
              </span>
            </div>

            <div className="flex items-center gap-2">
              <span
                className="text-[10px] font-mono opacity-60"
                style={{ color: saveState === 'error' || loadError ? '#ef4444' : theme.palette.textMuted }}
                role="status"
              >
                {loadError ? `SETTINGS NOT LOADED — ${loadError}` : SAVE_LABEL[saveState]}
              </span>
            </div>
          </div>

          {/* Section Body */}
          <div className="flex-1 overflow-y-auto p-6 md:p-8">
            {searchResults ? (
              <div className="space-y-4 max-w-3xl animate-fade-in">
                <div className="flex items-center justify-between pb-2 border-b" style={{ borderColor: theme.palette.glassBorder }}>
                  <h3 className="text-sm font-semibold" style={{ color: theme.palette.textPrimary }}>
                    Search Results for &quot;{searchQuery}&quot;
                  </h3>
                  <span className="text-xs opacity-60 font-mono" style={{ color: theme.palette.textSecondary }}>
                    {searchResults.length} matches
                  </span>
                </div>

                {searchResults.length === 0 ? (
                  <div className="py-12 text-center text-xs opacity-50 font-sans" style={{ color: theme.palette.textSecondary }}>
                    No settings matching &quot;{searchQuery}&quot;. Try &quot;microphone&quot;, &quot;theme&quot;, &quot;memory&quot;, or &quot;permission&quot;.
                  </div>
                ) : (
                  <div className="space-y-2">
                    {searchResults.map((item) => (
                      <div
                        key={item.sectionId}
                        onClick={() => {
                          setActiveSection(item.sectionId);
                          setSearchQuery('');
                        }}
                        className="p-3.5 rounded-2xl border transition-all cursor-pointer flex items-center justify-between group hover:scale-[1.008]"
                        style={{ backgroundColor: theme.palette.bgElevated, borderColor: theme.palette.glassBorder }}
                      >
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-semibold" style={{ color: theme.palette.textPrimary }}>
                              {item.label}
                            </span>
                            <span className="text-[10px] opacity-40 font-mono">
                              in {item.groupName}
                            </span>
                          </div>
                          <div className="text-[11px] opacity-60 mt-0.5" style={{ color: theme.palette.textSecondary }}>
                            {item.desc}
                          </div>
                        </div>
                        <i className="fa-solid fa-chevron-right text-xs opacity-40 group-hover:opacity-100 transition-opacity" style={{ color: theme.palette.accent }} />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              renderSectionContent()
            )}
          </div>
        </section>
      </div>
    </div>
  );
};
