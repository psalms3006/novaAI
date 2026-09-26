// NOVA's persisted settings (desk/settings.py through /api/settings), for the
// Settings screen. Only keys the backend actually acts on are edited here;
// the sections say plainly when a control has no backend yet.

import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { getJSON, postJSON } from '../nova/api';
import type { BackendSettings, PermissionValue, SaveState, SettingsGroup, SettingsSectionId } from '../types/settings';

export const SETTINGS_GROUPS: SettingsGroup[] = [
  {
    id: 'system',
    name: 'System & Core',
    sections: [
      { id: 'general', label: 'General', icon: 'fa-sliders', description: 'Start screen, clock and workspace folder', keywords: 'startup workspace folder clock time' },
      { id: 'appearance', label: 'Appearance', icon: 'fa-palette', description: 'Themes, liquid glass, motion and quality', keywords: 'theme glass blur motion quality performance scale' },
      { id: 'identity', label: 'Identity', icon: 'fa-user-astronaut', description: 'Your name, how to say it, and who you are to NOVA', keywords: 'name pronunciation role profile' },
    ],
  },
  {
    id: 'voice-conversation',
    name: 'Voice & Conversation',
    sections: [
      { id: 'voice', label: 'Voice Session', icon: 'fa-microphone', description: 'Live voice, microphone, language and interruption', keywords: 'mic microphone language barge interrupt speaker' },
      { id: 'conversation', label: 'Conversation', icon: 'fa-comments', description: 'Response style, streaming and how much context is sent', keywords: 'style concise detailed streaming history context' },
      { id: 'personality', label: 'Custom Instructions', icon: 'fa-masks-theater', description: 'Your own instructions to NOVA, sent with every turn', keywords: 'personality prompt instructions system' },
    ],
  },
  {
    id: 'intelligence-memory',
    name: 'Intelligence & Memory',
    sections: [
      { id: 'intelligence', label: 'Models & Offline', icon: 'fa-microchip', description: 'Which model is answering, local models and offline mode', keywords: 'model ollama local offline cloud gemini embedding' },
      { id: 'memory', label: 'Memory', icon: 'fa-database', description: 'What NOVA remembers, and forgetting it', keywords: 'memory remember forget facts clear' },
    ],
  },
  {
    id: 'control',
    name: 'Control & Security',
    sections: [
      { id: 'permissions', label: 'Permissions', icon: 'fa-shield-halved', description: 'What NOVA may do on her own, and stopping her', keywords: 'halt stop permission allow ask deny safety confirm' },
      { id: 'tools', label: 'Tools & Tasks', icon: 'fa-toolbox', description: 'Her tools, MCP connectors and background tasks', keywords: 'tools mcp tasks automation capabilities' },
      { id: 'account', label: 'Account & Access', icon: 'fa-key', description: 'Your Gemini key, offline mode and NOVA Cloud account', keywords: 'key api gemini byok cloud sign in account offline devices' },
      { id: 'connections', label: 'Connections', icon: 'fa-plug', description: 'Network, sign-in mode and connected accounts', keywords: 'network gmail account integrations cloud byok offline knowledge' },
    ],
  },
  {
    id: 'engine',
    name: 'Engine',
    sections: [
      { id: 'diagnostics', label: 'Diagnostics', icon: 'fa-heart-pulse', description: 'Live health, vitals, self-test and event stream', keywords: 'health cpu memory diagnostics developer events performance' },
      { id: 'about', label: 'About NOVA', icon: 'fa-circle-info', description: 'Version and build', keywords: 'version build about' },
    ],
  },
];

interface NovaSettingsContextType {
  activeSection: SettingsSectionId;
  setActiveSection: (section: SettingsSectionId) => void;
  searchQuery: string;
  setSearchQuery: (query: string) => void;

  settings: BackendSettings | null;
  loadError: string;
  saveState: SaveState;
  /** Change one setting. Text is saved once typing pauses; everything else at once. */
  update: <K extends keyof BackendSettings>(key: K, value: BackendSettings[K]) => void;
  setPermission: (category: string, value: PermissionValue) => void;
  reload: () => Promise<void>;
}

const NovaSettingsContext = createContext<NovaSettingsContextType | null>(null);

export const NovaSettingsProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [activeSection, setActiveSection] = useState<SettingsSectionId>('general');
  const [searchQuery, setSearchQuery] = useState('');
  const [settings, setSettings] = useState<BackendSettings | null>(null);
  const [loadError, setLoadError] = useState('');
  const [saveState, setSaveState] = useState<SaveState>('idle');

  const reload = useCallback(async () => {
    try {
      const j = await getJSON<{ settings: BackendSettings }>('/api/settings', 8000);
      setSettings(j.settings);
      setLoadError('');
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const pending = useRef<Partial<BackendSettings>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = useCallback(async () => {
    timer.current = null;
    const patch = pending.current;
    pending.current = {};
    if (!Object.keys(patch).length) return;
    setSaveState('saving');
    try {
      const j = await postJSON<{ settings: BackendSettings }>('/api/settings', patch);
      // The backend's answer is the truth: it drops what it does not accept.
      setSettings((cur) => ({ ...(cur || j.settings), ...j.settings, ...pending.current }));
      setSaveState('saved');
    } catch {
      setSaveState('error');
      reload();
    }
  }, [reload]);

  const update = useCallback(
    <K extends keyof BackendSettings>(key: K, value: BackendSettings[K]) => {
      setSettings((cur) => (cur ? { ...cur, [key]: value } : cur));
      pending.current = { ...pending.current, [key]: value };
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(flush, typeof value === 'string' ? 700 : 150);
    },
    [flush],
  );

  const setPermission = useCallback(
    (category: string, value: PermissionValue) => {
      setSettings((cur) => (cur ? { ...cur, permissions: { ...cur.permissions, [category]: value } } : cur));
      pending.current = {
        ...pending.current,
        permissions: { ...(pending.current.permissions || {}), [category]: value },
      };
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(flush, 150);
    },
    [flush],
  );

  useEffect(
    () => () => {
      if (timer.current) {
        clearTimeout(timer.current);
        flush();
      }
    },
    [flush],
  );

  return (
    <NovaSettingsContext.Provider
      value={{ activeSection, setActiveSection, searchQuery, setSearchQuery, settings, loadError, saveState, update, setPermission, reload }}
    >
      {children}
    </NovaSettingsContext.Provider>
  );
};

export const useNovaSettings = () => {
  const ctx = useContext(NovaSettingsContext);
  if (!ctx) throw new Error('useNovaSettings must be used within NovaSettingsProvider');
  return ctx;
};
