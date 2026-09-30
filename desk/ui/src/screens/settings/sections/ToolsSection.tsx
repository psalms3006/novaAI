import React from 'react';
import { getJSON } from '../../../nova/api';
import { useNova } from '../../../context/NovaStateContext';
import { Card, Loading, NotConnected, SectionHeader, Stat, useResource } from '../primitives';

interface ToolInfo {
  available: boolean;
  label: string;
  source: string;
  category: string;
  permission: 'allow' | 'ask' | 'deny';
}

interface McpStatus {
  enabled: boolean;
  connected_servers: number;
  servers: { name: string }[];
  tools: { name: string }[];
  message: string;
}

interface TaskStep {
  tool: string;
  status: string;
}

interface Task {
  id: string;
  title: string;
  status: string;
  progress?: number;
  steps: TaskStep[];
  created?: number;
  reason_for_stop?: string;
}

const PERM_COLOR = { allow: '#10b981', ask: '#f59e0b', deny: '#ef4444' } as const;

export const ToolsSection: React.FC = () => {
  const { theme } = useNova();
  const tools = useResource(() => getJSON<{ tools: Record<string, ToolInfo>; mcp: McpStatus }>('/api/tools', 10000));
  const tasks = useResource(() => getJSON<{ tasks: Task[]; status: string }>('/api/tasks', 10000), [], 5000);

  const list = Object.entries(tools.data?.tools || {}).sort((a, b) => Number(b[1].available) - Number(a[1].available) || a[0].localeCompare(b[0]));
  const mcp = tools.data?.mcp;

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <SectionHeader title="Tools & Tasks" subtitle="Everything NOVA can use, whether it is available now, and what she is working on in the background." />

      <Card title="Background tasks">
        {!tasks.data ? (
          <Loading what="tasks" error={tasks.error} />
        ) : tasks.data.tasks.length === 0 ? (
          <div className="text-[11px]" style={{ color: theme.palette.textMuted }}>
            No background tasks. {tasks.data.status !== 'ok' ? tasks.data.status : ''}
          </div>
        ) : (
          <div className="space-y-2">
            {tasks.data.tasks.slice(0, 20).map((t) => (
              <div key={t.id} className="p-2.5 rounded-xl border" style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}>
                <div className="flex justify-between gap-3 text-xs">
                  <span className="truncate" style={{ color: theme.palette.textPrimary }}>
                    {t.title}
                  </span>
                  <span className="font-mono text-[10px] shrink-0" style={{ color: theme.palette.accent }}>
                    {t.status}
                  </span>
                </div>
                <div className="text-[10px] font-mono opacity-50 mt-0.5" style={{ color: theme.palette.textMuted }}>
                  {t.steps.length} steps{t.progress != null ? ` · ${Math.round(t.progress)}%` : ''}
                  {t.reason_for_stop ? ` · ${t.reason_for_stop}` : ''}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Tools" right={tools.data && <span className="text-[10px] font-mono opacity-60" style={{ color: theme.palette.textMuted }}>{list.filter(([, t]) => t.available).length} of {list.length} available</span>}>
        {!tools.data ? (
          <Loading what="tools" error={tools.error} />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {list.map(([name, t]) => (
              <div
                key={name}
                className="p-2.5 rounded-xl border flex items-center justify-between gap-2"
                style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface, opacity: t.available ? 1 : 0.5 }}
              >
                <div className="min-w-0">
                  <div className="text-xs truncate" style={{ color: theme.palette.textPrimary }}>
                    {t.label || name}
                  </div>
                  <div className="text-[10px] font-mono opacity-50 truncate" style={{ color: theme.palette.textMuted }}>
                    {name} · {t.category}
                    {t.available ? '' : ' · unavailable'}
                  </div>
                </div>
                <span className="text-[10px] font-mono shrink-0" style={{ color: PERM_COLOR[t.permission] || theme.palette.textMuted }} title="Change in Permissions">
                  {t.permission}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="MCP connectors">
        {!mcp ? (
          <Loading what="MCP" error={tools.error} />
        ) : (
          <>
            <div className="grid grid-cols-3 gap-2">
              <Stat label="MCP" value={mcp.enabled ? 'On' : 'Off'} tone={mcp.enabled ? 'ok' : 'muted'} />
              <Stat label="Servers" value={mcp.connected_servers} />
              <Stat label="Tools" value={mcp.tools.length} />
            </div>
            {mcp.message && (
              <div className="text-[11px]" style={{ color: theme.palette.textSecondary }}>
                {mcp.message}
              </div>
            )}
            {mcp.servers.length > 0 && (
              <div className="text-[11px] font-mono" style={{ color: theme.palette.textSecondary }}>
                {mcp.servers.map((s) => s.name).join(' · ')}
              </div>
            )}
          </>
        )}
      </Card>

      <NotConnected
        items={[
          { label: 'Routines and automations', why: 'NOVA has no scheduler; ask her by voice and she plans the work as a background task above.' },
          { label: 'Turning single tools off', why: 'use Permissions to set a whole category to “Never”.' },
        ]}
      />
    </div>
  );
};
