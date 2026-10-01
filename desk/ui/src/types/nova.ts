export type ScreenMode = 'substrate' | 'runtime' | 'synaptic' | 'library';


export type ThemeId = 'obsidian' | 'titanium' | 'warm-graphite' | 'deep-ocean' | 'pearl';

export type GlassQualityMode = 'quality' | 'balanced' | 'performance';

export interface ThemeDefinition {
  id: ThemeId;
  name: string;
  tagline: string;
  isDark: boolean;
  palette: {
    bgBase: string;
    bgElevated: string;
    glassSurface: string;
    glassBorder: string;
    glassHighlight: string;
    specularRim: string;
    specularInner: string;
    ambientShadow: string;
    textPrimary: string;
    textSecondary: string;
    textMuted: string;
    accent: string;
    accentMuted: string;
    accentBorder: string;
    glow: string;
    orbParticlePrimary: string;
    orbParticleSecondary: string;
    orbWireframe: string;
    canvasGridColor: string;
  };
}

export interface TrajectoryItem {
  id: string;
  title: string;
  category: string;
  status: 'idle' | 'running' | 'completed' | 'queued';
  latency?: string;
  details?: string;
}

export interface TransparencyLog {
  id: string;
  agent: string;
  source: string;
  timeOffset: string;
  message: string;
  accent: 'neutral' | 'accent' | 'success';
}

export interface SynapseNode {
  id: string;
  label: string;
  sublabel: string;
  category: 'core' | 'tech' | 'prefs' | 'mcp' | 'skills' | 'browser' | 'fleet';
  x: number;
  y: number;
  r: number;
  hash: string;
  confidence: string;
  badge: string;
  quote: string;
  decayRate: string;
  decayStatus: string;
  pinned?: boolean;
}

export interface NotificationToast {
  id: string;
  title: string;
  description?: string;
  type: 'info' | 'success' | 'warning';
  timestamp: string;
}
