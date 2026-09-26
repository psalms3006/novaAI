// Everything NOVA keeps for the user, in one place: past conversations, the
// document library she answers from, projects, and the workspace folder.
// All of it is the existing backend (desk/store.py, desk/projects.py,
// nova_core/rag, the workspace routes); this screen only presents it.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, getJSON, postJSON, uploadFile, TOKEN } from '../nova/api';
import { useRuntime } from '../nova/runtime';
import { useNova } from '../context/NovaStateContext';

type Tab = 'conversations' | 'documents' | 'projects' | 'files';

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: 'conversations', label: 'Conversations', icon: 'fa-comments' },
  { id: 'documents', label: 'Documents', icon: 'fa-book-open' },
  { id: 'projects', label: 'Projects', icon: 'fa-layer-group' },
  { id: 'files', label: 'Files', icon: 'fa-folder-tree' },
];

function bytes(n?: number): string {
  if (!n) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function when(ts?: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString() ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString();
}

async function del(path: string): Promise<void> {
  const r = await api(path, { method: 'DELETE' });
  const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string };
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
}

/** Shared look for the library's lists. */
function useStyles() {
  const { theme } = useNova();
  const p = theme.palette;
  return {
    p,
    theme,
    row: { borderColor: p.glassBorder, backgroundColor: p.glassSurface } as React.CSSProperties,
    field: { borderColor: p.glassBorder, color: p.textPrimary, backgroundColor: p.glassSurface } as React.CSSProperties,
    btn: 'px-3 py-1.5 rounded-xl border text-[11px] font-medium cursor-pointer transition-all hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed',
    plain: { borderColor: p.glassBorder, color: p.textSecondary, backgroundColor: p.bgElevated } as React.CSSProperties,
    accent: { borderColor: p.accentBorder, color: theme.isDark ? p.bgBase : '#fff', backgroundColor: p.accent } as React.CSSProperties,
    danger: { borderColor: 'rgba(239,68,68,0.35)', color: '#ef4444', backgroundColor: 'rgba(239,68,68,0.1)' } as React.CSSProperties,
  };
}

/** A two-step delete: the first click asks, the second does it. No window.confirm (unreliable in the desktop webview). */
const ConfirmDelete: React.FC<{ onConfirm: () => void; label?: string }> = ({ onConfirm, label = 'Delete' }) => {
  const s = useStyles();
  const [asking, setAsking] = useState(false);
  return asking ? (
    <span className="flex gap-1.5">
      <button className={s.btn} style={s.plain} onClick={() => setAsking(false)}>
        Keep
      </button>
      <button
        className={s.btn}
        style={s.danger}
        onClick={() => {
          setAsking(false);
          onConfirm();
        }}
      >
        Yes, {label.toLowerCase()}
      </button>
    </span>
  ) : (
    <button className={s.btn} style={s.plain} onClick={() => setAsking(true)} aria-label={label}>
      <i className="fa-solid fa-trash-can text-[10px]" />
    </button>
  );
};

const Empty: React.FC<{ children: React.ReactNode; error?: boolean }> = ({ children, error }) => {
  const s = useStyles();
  return (
    <div className="text-[11px] py-6 text-center" style={{ color: error ? '#ef4444' : s.p.textMuted }}>
      {children}
    </div>
  );
};

// ── conversations ─────────────────────────────────────────────────────────────

interface Conversation {
  id: string;
  title: string;
  project_id?: string | null;
  updated: number;
  n: number;
}

const ConversationsTab: React.FC = () => {
  const s = useStyles();
  const { addToast, setCurrentScreen } = useNova();
  const rt = useRuntime();
  const [q, setQ] = useState('');
  const [list, setList] = useState<Conversation[] | null>(null);
  const [error, setError] = useState('');
  const [renaming, setRenaming] = useState<string | null>(null);
  const [title, setTitle] = useState('');

  const load = useCallback(async () => {
    try {
      const path = q.trim() ? `/api/conversations/search?q=${encodeURIComponent(q.trim())}` : '/api/conversations';
      setList((await getJSON<{ conversations: Conversation[] }>(path, 15000)).conversations || []);
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, [q]);

  useEffect(() => {
    const t = setTimeout(load, q ? 250 : 0);
    return () => clearTimeout(t);
  }, [load, q]);

  const open = async (c: Conversation) => {
    try {
      await rt.openConversation(c.id);
      setCurrentScreen('substrate');
      addToast('Conversation opened', 'What you type next continues it.', 'info');
    } catch (e) {
      addToast('Could not open it', (e as Error).message, 'warning');
    }
  };

  const rename = async (c: Conversation) => {
    setRenaming(null);
    if (!title.trim() || title.trim() === c.title) return;
    try {
      const r = await api(`/api/conversations/${encodeURIComponent(c.id)}`, { method: 'PATCH', body: JSON.stringify({ title: title.trim() }) });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      load();
    } catch (e) {
      addToast('Could not rename it', (e as Error).message, 'warning');
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search conversations…" aria-label="Search conversations" className="flex-1 text-xs p-2.5 rounded-xl border outline-none select-text" style={s.field} />
        <button
          className={s.btn}
          style={s.accent}
          onClick={() => {
            rt.newConversation();
            setCurrentScreen('substrate');
          }}
        >
          <i className="fa-solid fa-plus text-[10px] mr-1" /> New conversation
        </button>
      </div>
      {error ? (
        <Empty error>Could not load conversations: {error}</Empty>
      ) : !list ? (
        <Empty>Loading…</Empty>
      ) : list.length === 0 ? (
        <Empty>{q ? 'Nothing matches.' : 'No typed conversations yet. Conversations you type to NOVA are kept here.'}</Empty>
      ) : (
        list.map((c) => (
          <div key={c.id} className="p-3 rounded-xl border flex items-center justify-between gap-3" style={{ ...s.row, borderColor: c.id === rt.conversationId ? s.p.accentBorder : s.p.glassBorder }}>
            <div className="min-w-0 flex-1">
              {renaming === c.id ? (
                <input
                  autoFocus
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  onBlur={() => rename(c)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') rename(c);
                    if (e.key === 'Escape') setRenaming(null);
                  }}
                  aria-label="Conversation title"
                  className="w-full text-xs p-1.5 rounded-lg border outline-none select-text"
                  style={s.field}
                />
              ) : (
                <button className="text-xs font-medium truncate text-left w-full cursor-pointer" style={{ color: s.p.textPrimary }} onClick={() => open(c)} title="Open">
                  {c.title || 'Untitled'}
                </button>
              )}
              <div className="text-[10px] font-mono opacity-60 mt-0.5" style={{ color: s.p.textMuted }}>
                {c.n} messages · {when(c.updated)}
                {c.id === rt.conversationId ? ' · open now' : ''}
              </div>
            </div>
            <div className="flex gap-1.5 shrink-0">
              <button className={s.btn} style={s.plain} onClick={() => open(c)}>
                Open
              </button>
              <button
                className={s.btn}
                style={s.plain}
                aria-label="Rename"
                onClick={() => {
                  setRenaming(c.id);
                  setTitle(c.title || '');
                }}
              >
                <i className="fa-solid fa-pen text-[10px]" />
              </button>
              <ConfirmDelete
                onConfirm={async () => {
                  try {
                    await del(`/api/conversations/${encodeURIComponent(c.id)}`);
                    if (c.id === rt.conversationId) rt.newConversation();
                    load();
                  } catch (e) {
                    addToast('Could not delete it', (e as Error).message, 'warning');
                  }
                }}
              />
            </div>
          </div>
        ))
      )}
    </div>
  );
};

// ── documents ────────────────────────────────────────────────────────────────

interface Doc {
  id: string;
  filename: string;
  title: string;
  kind: string;
  chunks: number;
  bytes: number;
  pages?: number;
  warnings?: string[];
}

interface EmbeddingChoice {
  id: string;
  label: string;
  description: string;
  available: boolean;
  status: string;
}

const DocumentsTab: React.FC = () => {
  const s = useStyles();
  const { addToast } = useNova();
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [stats, setStats] = useState<{ semantic?: boolean; backend_note?: string }>({});
  const [error, setError] = useState('');
  const [emb, setEmb] = useState<{ chosen: string; note: string; honoured: boolean; choices: EmbeddingChoice[] } | null>(null);
  const [uploading, setUploading] = useState('');
  const [over, setOver] = useState(false);
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<{ text: string; citation: string; how: string }[] | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const j = await getJSON<{ documents: Doc[]; stats: typeof stats }>('/api/documents', 15000);
      setDocs(j.documents || []);
      setStats(j.stats || {});
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
    try {
      setEmb(await getJSON('/api/documents/embeddings', 15000));
    } catch {
      setEmb(null);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const add = async (file: File) => {
    setUploading(file.name);
    try {
      const j = await uploadFile<{ message?: string; warnings?: string[] }>('/api/documents', file);
      addToast('Added to the library', [j.message, ...(j.warnings || [])].filter(Boolean).join(' '), 'success');
      load();
    } catch (e) {
      addToast(`Could not add ${file.name}`, (e as Error).message, 'warning');
    } finally {
      setUploading('');
    }
  };

  const search = async () => {
    if (!q.trim()) return setHits(null);
    try {
      setHits((await getJSON<{ hits: { text: string; citation: string; how: string }[] }>(`/api/documents/search?q=${encodeURIComponent(q.trim())}&limit=8`, 20000)).hits || []);
    } catch (e) {
      addToast('Search failed', (e as Error).message, 'warning');
    }
  };

  const choose = async (id: string) => {
    try {
      const j = await postJSON<{ note: string; honoured: boolean }>('/api/documents/embeddings', { choice: id });
      addToast(j.honoured ? 'Search method changed' : 'Not available yet', j.note, j.honoured ? 'success' : 'warning');
      load();
    } catch (e) {
      addToast('Could not change it', (e as Error).message, 'warning');
    }
  };

  return (
    <div className="space-y-4">
      <div
        onClick={() => picker.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const f = e.dataTransfer.files?.[0];
          if (f) add(f);
        }}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && picker.current?.click()}
        className="p-6 rounded-2xl border border-dashed text-center cursor-pointer transition-all"
        style={{ borderColor: over ? s.p.accent : s.p.glassBorder, backgroundColor: over ? s.p.glassSurface : 'transparent' }}
      >
        <i className="fa-solid fa-file-arrow-up text-lg" style={{ color: s.p.accent }} />
        <div className="text-xs mt-2" style={{ color: s.p.textPrimary }}>
          {uploading ? `Reading ${uploading}…` : 'Drop a document here, or click to choose'}
        </div>
        <div className="text-[10px] mt-1" style={{ color: s.p.textMuted }}>
          NOVA answers from it, citing the page or section it came from. PDF, Word, text, Markdown and more.
        </div>
        <input
          ref={picker}
          type="file"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) add(f);
            e.target.value = '';
          }}
        />
      </div>

      {stats.semantic === false && (
        <div className="text-[11px] p-2.5 rounded-xl border" style={{ ...s.row, color: '#f59e0b' }}>
          {stats.backend_note || 'Keyword search only.'}
        </div>
      )}

      <div className="flex gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && search()} placeholder="Search inside your documents…" aria-label="Search documents" className="flex-1 text-xs p-2.5 rounded-xl border outline-none select-text" style={s.field} />
        <button className={s.btn} style={s.plain} onClick={search}>
          Search
        </button>
      </div>
      {hits &&
        (hits.length === 0 ? (
          <Empty>No passage matches.</Empty>
        ) : (
          <div className="space-y-2">
            {hits.map((h, i) => (
              <div key={i} className="p-3 rounded-xl border text-xs leading-relaxed select-text" style={{ ...s.row, color: s.p.textSecondary }}>
                {h.text.length > 360 ? `${h.text.slice(0, 360)}…` : h.text}
                <div className="text-[10px] font-mono mt-1.5" style={{ color: s.p.accent }}>
                  {h.citation} · {h.how}
                </div>
              </div>
            ))}
          </div>
        ))}

      {error ? (
        <Empty error>The document library is not available: {error}</Empty>
      ) : !docs ? (
        <Empty>Loading…</Empty>
      ) : docs.length === 0 ? (
        <Empty>No documents yet.</Empty>
      ) : (
        docs.map((d) => (
          <div key={d.id} className="p-3 rounded-xl border" style={s.row}>
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-xs font-medium truncate" style={{ color: s.p.textPrimary }}>
                  {d.title || d.filename}
                </div>
                <div className="text-[10px] font-mono opacity-60" style={{ color: s.p.textMuted }}>
                  {d.kind} · {bytes(d.bytes)} · {d.chunks} passages{d.pages ? ` · ${d.pages} pages` : ''}
                </div>
              </div>
              <ConfirmDelete
                label="Remove"
                onConfirm={async () => {
                  try {
                    await del(`/api/documents/${encodeURIComponent(d.id)}`);
                    load();
                  } catch (e) {
                    addToast('Could not remove it', (e as Error).message, 'warning');
                  }
                }}
              />
            </div>
            {(d.warnings || []).map((w) => (
              <div key={w} className="text-[10px] mt-1" style={{ color: '#f59e0b' }}>
                {w}
              </div>
            ))}
          </div>
        ))
      )}

      {emb && (
        <div className="pt-2 space-y-2">
          <div className="text-[11px] font-mono uppercase tracking-wider opacity-70" style={{ color: s.p.accent }}>
            How NOVA searches your documents
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            {emb.choices.map((c) => {
              const on = emb.chosen === c.id;
              return (
                <button key={c.id} onClick={() => choose(c.id)} className="p-3 rounded-xl border text-left cursor-pointer" style={{ ...s.row, borderColor: on ? s.p.accent : s.p.glassBorder }} aria-pressed={on}>
                  <div className="text-xs font-medium" style={{ color: s.p.textPrimary }}>
                    {c.label}
                  </div>
                  <div className="text-[10px] mt-0.5 opacity-70" style={{ color: s.p.textSecondary }}>
                    {c.description}
                  </div>
                  <div className="text-[10px] mt-1 font-mono" style={{ color: c.available ? '#10b981' : '#f59e0b' }}>
                    {c.available ? 'Ready' : c.status}
                  </div>
                </button>
              );
            })}
          </div>
          {emb.note && (
            <div className="text-[11px]" style={{ color: emb.honoured ? s.p.textSecondary : '#f59e0b' }}>
              {emb.note}
            </div>
          )}
          {emb.choices.some((c) => c.id === 'onnx' && !c.available) && (
            <button
              className={s.btn}
              style={s.plain}
              onClick={async () => {
                try {
                  await postJSON('/api/documents/embeddings/download', {});
                  addToast('Downloading the local model', 'About 90 MB, in the background. Choose “On this computer” once it finishes.', 'info');
                } catch (e) {
                  addToast('Download failed', (e as Error).message, 'warning');
                }
              }}
            >
              Download the local model (~90 MB)
            </button>
          )}
        </div>
      )}
    </div>
  );
};

// ── projects ─────────────────────────────────────────────────────────────────

interface Project {
  id: string;
  name: string;
  description?: string;
  instructions?: string;
  color?: string;
  conversations?: number;
  updated?: number;
}

const ProjectsTab: React.FC = () => {
  const s = useStyles();
  const { addToast, setCurrentScreen } = useNova();
  const rt = useRuntime();
  const [list, setList] = useState<Project[] | null>(null);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<Project | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [recent, setRecent] = useState<Conversation[]>([]);

  const load = useCallback(async () => {
    try {
      setList((await getJSON<{ projects: Project[] }>('/api/projects', 15000)).projects || []);
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!openId) return setRecent([]);
    getJSON<{ project: { recent?: Conversation[] } }>(`/api/projects/${encodeURIComponent(openId)}`, 15000)
      .then((j) => setRecent(j.project.recent || []))
      .catch(() => setRecent([]));
  }, [openId]);

  const save = async () => {
    if (!editing || !editing.name.trim()) return;
    const body = { name: editing.name.trim(), description: editing.description || '', instructions: editing.instructions || '', color: editing.color || 'violet' };
    try {
      if (editing.id) {
        const r = await api(`/api/projects/${encodeURIComponent(editing.id)}`, { method: 'PATCH', body: JSON.stringify(body) });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
      } else {
        await postJSON('/api/projects', body);
      }
      setEditing(null);
      load();
    } catch (e) {
      addToast('Could not save the project', (e as Error).message, 'warning');
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex justify-between items-center">
        <div className="text-[11px]" style={{ color: s.p.textMuted }}>
          A project groups conversations and gives NOVA standing instructions for them.
        </div>
        <button className={s.btn} style={s.accent} onClick={() => setEditing({ id: '', name: '', description: '', instructions: '' })}>
          <i className="fa-solid fa-plus text-[10px] mr-1" /> New project
        </button>
      </div>

      {editing && (
        <div className="p-4 rounded-2xl border space-y-2" style={{ ...s.row, borderColor: s.p.accentBorder }}>
          <input autoFocus value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} placeholder="Project name" aria-label="Project name" maxLength={80} className="w-full text-xs p-2.5 rounded-xl border outline-none select-text" style={s.field} />
          <input value={editing.description || ''} onChange={(e) => setEditing({ ...editing, description: e.target.value })} placeholder="What it's about (optional)" aria-label="Project description" maxLength={300} className="w-full text-xs p-2.5 rounded-xl border outline-none select-text" style={s.field} />
          <textarea value={editing.instructions || ''} onChange={(e) => setEditing({ ...editing, instructions: e.target.value })} placeholder="Instructions NOVA follows in this project's conversations (optional)" aria-label="Project instructions" rows={3} maxLength={4000} className="w-full text-xs p-2.5 rounded-xl border outline-none resize-y select-text" style={s.field} />
          <div className="flex justify-end gap-2">
            <button className={s.btn} style={s.plain} onClick={() => setEditing(null)}>
              Cancel
            </button>
            <button className={s.btn} style={s.accent} onClick={save} disabled={!editing.name.trim()}>
              {editing.id ? 'Save' : 'Create'}
            </button>
          </div>
        </div>
      )}

      {error ? (
        <Empty error>Could not load projects: {error}</Empty>
      ) : !list ? (
        <Empty>Loading…</Empty>
      ) : list.length === 0 ? (
        <Empty>No projects yet.</Empty>
      ) : (
        list.map((pr) => (
          <div key={pr.id} className="p-3 rounded-xl border" style={s.row}>
            <div className="flex items-center justify-between gap-3">
              <button className="min-w-0 text-left cursor-pointer flex-1" onClick={() => setOpenId(openId === pr.id ? null : pr.id)} aria-expanded={openId === pr.id}>
                <div className="text-xs font-medium truncate" style={{ color: s.p.textPrimary }}>
                  {pr.name}
                </div>
                <div className="text-[10px] opacity-60 truncate" style={{ color: s.p.textMuted }}>
                  {pr.description || 'No description'} · {pr.conversations ?? 0} conversations
                </div>
              </button>
              <div className="flex gap-1.5 shrink-0">
                <button className={s.btn} style={s.plain} onClick={() => setEditing({ ...pr })} aria-label="Edit project">
                  <i className="fa-solid fa-pen text-[10px]" />
                </button>
                <ConfirmDelete
                  onConfirm={async () => {
                    try {
                      await del(`/api/projects/${encodeURIComponent(pr.id)}`);
                      load();
                    } catch (e) {
                      addToast('Could not delete it', (e as Error).message, 'warning');
                    }
                  }}
                />
              </div>
            </div>
            {openId === pr.id && (
              <div className="mt-2 pt-2 border-t space-y-1" style={{ borderColor: s.p.glassBorder }}>
                {pr.instructions && (
                  <div className="text-[11px] mb-1.5 select-text" style={{ color: s.p.textSecondary }}>
                    <span className="opacity-60">Instructions: </span>
                    {pr.instructions}
                  </div>
                )}
                {recent.length === 0 ? (
                  <div className="text-[11px]" style={{ color: s.p.textMuted }}>
                    No conversations in this project yet.
                  </div>
                ) : (
                  recent.map((c) => (
                    <button
                      key={c.id}
                      className="block w-full text-left text-[11px] truncate cursor-pointer hover:opacity-100 opacity-80"
                      style={{ color: s.p.textSecondary }}
                      onClick={async () => {
                        await rt.openConversation(c.id);
                        setCurrentScreen('substrate');
                      }}
                    >
                      <i className="fa-solid fa-comments text-[9px] mr-1.5" />
                      {c.title || 'Untitled'} <span className="opacity-50">· {when(c.updated)}</span>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
};

// ── files ────────────────────────────────────────────────────────────────────

interface WorkspaceFile {
  name: string;
  size: number;
  modified: number;
}

const TEXT_LIKE = /\.(txt|md|csv|json|py|js|ts|tsx|html|htm|css|log|xml|yaml|yml|ini|toml)$/i;
const IMAGE = /\.(png|jpe?g|gif|webp)$/i;

const FilesTab: React.FC = () => {
  const s = useStyles();
  const { addToast } = useNova();
  const [files, setFiles] = useState<WorkspaceFile[] | null>(null);
  const [workspace, setWorkspace] = useState('');
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<{ name: string; text?: string; image?: string } | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const j = await getJSON<{ files: WorkspaceFile[]; workspace: string }>('/api/files', 15000);
      setFiles((j.files || []).sort((a, b) => b.modified - a.modified));
      setWorkspace(j.workspace);
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(
    () => () => {
      if (preview?.image) URL.revokeObjectURL(preview.image);
    },
    [preview],
  );

  // Files are fetched with the run token and shown as text or an image --
  // never rendered as a page: workspace HTML is written by NOVA and the web,
  // and rendering it inside the app would run it next to the interface.
  const show = async (f: WorkspaceFile) => {
    if (!TEXT_LIKE.test(f.name) && !IMAGE.test(f.name)) {
      setPreview({ name: f.name, text: `No preview for this kind of file. It is at:\n${workspace}\\${f.name.replace(/\//g, '\\')}` });
      return;
    }
    try {
      const r = await fetch(`/api/files/${f.name.split('/').map(encodeURIComponent).join('/')}`, { headers: { 'X-NOVA-Desk': TOKEN } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      if (IMAGE.test(f.name)) setPreview({ name: f.name, image: URL.createObjectURL(await r.blob()) });
      else {
        const t = await r.text();
        setPreview({ name: f.name, text: t.length > 200000 ? `${t.slice(0, 200000)}\n… (truncated)` : t });
      }
    } catch (e) {
      addToast('Could not open it', (e as Error).message, 'warning');
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex justify-between items-center gap-3">
        <div className="text-[11px] font-mono truncate select-text" style={{ color: s.p.textMuted }} title={workspace}>
          {workspace || '—'}
        </div>
        <div className="flex gap-2 shrink-0">
          <button className={s.btn} style={s.plain} onClick={load} aria-label="Refresh">
            <i className="fa-solid fa-rotate text-[10px]" />
          </button>
          <button className={s.btn} style={s.accent} onClick={() => picker.current?.click()}>
            <i className="fa-solid fa-file-arrow-up text-[10px] mr-1" /> Add a file
          </button>
          <input
            ref={picker}
            type="file"
            hidden
            onChange={async (e) => {
              const f = e.target.files?.[0];
              e.target.value = '';
              if (!f) return;
              try {
                await uploadFile('/api/files/upload', f);
                addToast('Added to the workspace', `attachments/${f.name}`, 'success');
                load();
              } catch (err) {
                addToast(`Could not add ${f.name}`, (err as Error).message, 'warning');
              }
            }}
          />
        </div>
      </div>

      {preview && (
        <div className="p-3 rounded-2xl border space-y-2" style={{ ...s.row, borderColor: s.p.accentBorder }}>
          <div className="flex justify-between items-center">
            <span className="text-xs font-mono truncate" style={{ color: s.p.textPrimary }}>
              {preview.name}
            </span>
            <button className="text-[11px] cursor-pointer opacity-60 hover:opacity-100" style={{ color: s.p.textSecondary }} onClick={() => setPreview(null)} aria-label="Close preview">
              <i className="fa-solid fa-xmark" />
            </button>
          </div>
          {preview.image ? (
            <img src={preview.image} alt={preview.name} className="max-h-[50vh] rounded-xl mx-auto" />
          ) : (
            <pre className="text-[11px] font-mono whitespace-pre-wrap max-h-[50vh] overflow-auto select-text" style={{ color: s.p.textSecondary }}>
              {preview.text}
            </pre>
          )}
        </div>
      )}

      {error ? (
        <Empty error>Could not list the workspace: {error}</Empty>
      ) : !files ? (
        <Empty>Loading…</Empty>
      ) : files.length === 0 ? (
        <Empty>The workspace is empty. Files NOVA creates, and files you add, appear here.</Empty>
      ) : (
        files.slice(0, 300).map((f) => (
          <button key={f.name} onClick={() => show(f)} className="w-full p-2.5 rounded-xl border flex items-center justify-between gap-3 text-left cursor-pointer hover:brightness-110" style={s.row}>
            <span className="text-xs truncate" style={{ color: s.p.textPrimary }}>
              <i className={`fa-solid ${IMAGE.test(f.name) ? 'fa-image' : TEXT_LIKE.test(f.name) ? 'fa-file-lines' : 'fa-file'} text-[10px] mr-2 opacity-60`} />
              {f.name}
            </span>
            <span className="text-[10px] font-mono shrink-0 opacity-60" style={{ color: s.p.textMuted }}>
              {bytes(f.size)} · {when(f.modified)}
            </span>
          </button>
        ))
      )}
    </div>
  );
};

// ── screen ───────────────────────────────────────────────────────────────────

export const LibraryScreen: React.FC = () => {
  const { theme } = useNova();
  const [tab, setTab] = useState<Tab>('conversations');
  const p = theme.palette;

  return (
    <div className="relative w-full h-full flex items-center justify-center p-6 overflow-hidden select-none font-sans">
      <div
        className="w-full max-w-4xl h-[calc(100vh-6rem)] rounded-3xl border shadow-2xl backdrop-blur-3xl overflow-hidden flex flex-col transition-all duration-300"
        style={{ backgroundColor: p.glassSurface, borderColor: p.glassBorder, boxShadow: `0 25px 60px -15px ${p.ambientShadow}, inset 0 1px 1px 0 ${p.glassHighlight}` }}
      >
        <div className="h-12 px-5 border-b flex items-center gap-1 shrink-0" style={{ borderColor: p.glassBorder }} role="tablist" aria-label="Library">
          <span className="text-[11px] font-mono uppercase tracking-wider mr-4 opacity-70" style={{ color: p.accent }}>
            Library
          </span>
          {TABS.map((t) => {
            const on = tab === t.id;
            return (
              <button
                key={t.id}
                role="tab"
                aria-selected={on}
                onClick={() => setTab(t.id)}
                className="px-3 py-1.5 rounded-xl text-xs flex items-center gap-1.5 cursor-pointer transition-all"
                style={{ backgroundColor: on ? p.bgElevated : 'transparent', color: on ? p.textPrimary : p.textSecondary, boxShadow: on ? `inset 0 0 0 1px ${p.accentBorder}` : undefined }}
              >
                <i className={`fa-solid ${t.icon} text-[10px] opacity-70`} />
                {t.label}
              </button>
            );
          })}
        </div>
        <div className="flex-1 overflow-y-auto p-6" role="tabpanel">
          {tab === 'conversations' && <ConversationsTab />}
          {tab === 'documents' && <DocumentsTab />}
          {tab === 'projects' && <ProjectsTab />}
          {tab === 'files' && <FilesTab />}
        </div>
      </div>
    </div>
  );
};
