export type SettingsSectionId =
  | 'general'
  | 'appearance'
  | 'conversation'
  | 'voice'
  | 'intelligence'
  | 'memory'
  | 'personality'
  | 'identity'
  | 'permissions'
  | 'tools'
  | 'connections'
  | 'diagnostics'
  | 'about';

export interface SettingsGroup {
  id: string;
  name: string;
  sections: {
    id: SettingsSectionId;
    label: string;
    icon: string;
    description: string;
    /** Extra words the settings search should match. */
    keywords?: string;
  }[];
}

export type PermissionValue = 'allow' | 'ask' | 'deny';

/** desk/settings.py `_DEFAULTS` -- every key the backend persists. */
export interface BackendSettings {
  user_name: string;
  user_name_pronunciation: string;
  user_role: string;
  mic_device: string;
  voice_language: string;
  user_system_prompt: string;
  streaming: boolean;
  response_style: 'concise' | 'balanced' | 'detailed';
  memory_enabled: boolean;
  embedding_backend: 'auto' | 'onnx' | 'cloud' | 'lexical';
  history_turns: number;
  show_tool_activity: boolean;
  developer_mode: boolean;
  voice_enabled: boolean;
  voice_responses: boolean;
  continuous_conversation: boolean;
  auto_listen: boolean;
  barge_in: boolean;
  permissions: Record<string, PermissionValue>;
  launch_on_startup: boolean;
  start_minimized: boolean;
  remember_window: boolean;
  workspace_dir: string;
  offline_fallback: boolean;
  local_model: string;
  online_preferred: boolean;
  auto_download_model: boolean;
  local_model_timeout: number;
  auth_mode: string;
  onboarded: boolean;
}

export type SaveState = 'idle' | 'saving' | 'saved' | 'error';
