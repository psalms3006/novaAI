# NOVA — Current Architecture (audit, 2026-09-28)

What exists today, verified against the code on branch `feat/new-nova-ui`
(commit 7bae680) and against the running systems where I could reach them.
Citations are `file:line`. Nothing here is aspirational; see
`NOVA_PRODUCTION_ARCHITECTURE.md` for the target.

---

## 1. The pieces

```text
┌──────────────────────── Windows PC ────────────────────────┐
│ NOVA.exe  (PyInstaller, nova_desktop_app.py)               │
│   ├─ desk.creds.bootstrap()        credentials, device.json│
│   ├─ Flask bridge  127.0.0.1:8765  desk/bridge.py          │
│   │     └─ React UI (desk/ui, Vite) in a pywebview window  │
│   ├─ NOVA core   nova.py + *_extra.py + nova_core/*        │
│   │     tools, planner, task manager, agents, memory, RAG  │
│   ├─ Gemini Live session  desk/live_session.py (voice)     │
│   ├─ Whisper STT / Piper TTS / Ollama (offline)            │
│   └─ nova_account.py  → NOVA Cloud (only if NOVA_CLOUD_URL)│
│ Data: %APPDATA%\NOVA\  (one folder per Windows user)       │
└────────────────────────────────────────────────────────────┘
            │ HTTPS (optional, off by default)
            ▼
┌──────────── nova_cloud (Flask, not deployed anywhere) ─────┐
│ /v1/auth  /v1/devices  /v1/sync  /v1/telemetry  /admin     │
│ SQLite (~/.nova/cloud) or Postgres via DATABASE_URL        │
└────────────────────────────────────────────────────────────┘

┌──────────── AWS EC2 13.61.150.81 (eu-north-1a) ────────────┐
│ t3.micro · 2 vCPU · 908 MB RAM · 2 GB swap · 28 GB disk     │
│ Ubuntu 26.04 · up 5+ weeks                                 │
│ Runs an AUGUST copy of NOVA: `nova.py --phone`             │
│   listening on 0.0.0.0:5050, plain HTTP, reachable publicly│
│ nova_cloud is NOT running there. No Docker, no TLS proxy.  │
└────────────────────────────────────────────────────────────┘
```

## 2. Desktop application

| Area | What exists | Where |
|---|---|---|
| Entry point | `nova_desktop_app.py` → creds bootstrap (:147) → Flask (:164) → brain init (:180) → pywebview window | |
| UI | React/Vite in `desk/ui`, served by the bridge; `App.tsx` always mounts `<Onboarding/>` and `<AccountGate/>` as overlays | App.tsx:122-123 |
| App data | `%APPDATA%\NOVA` (`app_data_dir()`); in development, the working directory | desk/settings.py:151-158, nova_paths.py |
| Settings | `settings.json` — flags `onboarded`, `auth_mode`, `cloud_url`, `permissions`, `launch_on_startup`, `start_minimized`, … | desk/settings.py:73-161 |
| Conversations | SQLite `nova_desktop.db` | desk/store.py:18 |
| Memory | `memory_texts.json` + index, `living_memory.json`, `nova_memories/` | nova.py:331-346, nova_memory.py |
| Tasks | `nova_tasks.json`, agents, orchestration (task_manager.py, agent_activity.py) | |
| Packaging | PyInstaller spec → `dist/NOVADesktop2`; Inno Setup `NOVA-Setup.iss` (per-user, `{autopf}\NOVA`); portable zip | packaging/ |

### 2.1 First run and onboarding
* One screen, not a wizard: name (+ pronunciation), then one of **Gemini key
  (BYOK)**, **NOVA Cloud URL**, or **offline** (Onboarding.tsx).
* Shown only when `/api/status.auth` says `onboarded === false` **and**
  `has_credential === false` (onboarding.ts:23-31). A `GEMINI_API_KEY` in the
  environment therefore skips onboarding entirely.
* Not version-tied: clearing the key does not reset `onboarded`
  (creds.py:474-479). It returns only if settings.json is lost.
* `/api/onboarding/complete` exists (bridge.py:2192) but the UI never calls it.
* No steps for account, profile, permissions, offline model, or startup.

### 2.2 Account and authentication
* **Guest mode is the default.** With no `NOVA_CLOUD_URL` env var the account
  layer is off ("no NOVA Cloud configured; running local-only",
  bridge.py:2573) and `AccountGate` renders nothing.
* Two unrelated cloud clients:
  * `nova_account.py` — the user account (signup/login/refresh/logout,
    devices, preference sync, telemetry). Base URL **only** from
    `NOVA_CLOUD_URL` (:140). Session stored in `nova_secure_store`
    (Windows Credential Manager, fallback file encrypted with a home-made
    SHA-256 XOR stream + HMAC). Lazy refresh (:307-340); 30-day offline grace.
  * `desk/creds.py NovaCloudClient` — an AI gateway client (`/v1/devices/register`,
    `/v1/sessions`, `/v1/live/token`, `/gateway/...`). Base URL from env **or**
    the `cloud_url` setting. **None of those endpoints exist in nova_cloud.**
* Consequence: typing a cloud URL in onboarding configures the gateway client,
  but the account gate never appears, because `nova_account` ignores that setting.
* Logout clears the session and resets `user_name`; memory and conversations stay.

### 2.3 Identity: three unrelated IDs
| ID | Where | Used by |
|---|---|---|
| `device.json` `{device_id:"nova-…", secret}` (DPAPI) | creds.py:238-259 | gateway client |
| `installation_identity` `{installation_id}` | nova_account.py:55-67 | account client |
| `device_identities[email]` `{device_id, secret}` | nova_account.py:74-88 | account client |

There is no **instance** concept anywhere, desktop or cloud.

### 2.4 Per-user isolation — none
All local state is global per Windows user: settings, conversations, memory,
tasks, audit logs. Signing out and signing in as someone else shows the
first person's memories and conversations.

### 2.5 Permissions
Seven categories — `web network screen mic files computer exec` — each
`allow / ask / deny`, stored in settings.json (settings.py:18-26), editable in
Settings → Permissions, enforced at tool dispatch through `_ui_safety_gate`
(confirm.py:142-193, nova.py:1823-1838). Gaps: no read/write split for files,
no read/interact split for browser/screen; memory tools map to a category
that does not exist and fall back to "ask"; the `mic` category does not gate
voice capture itself.

### 2.6 Start with Windows
`launch_on_startup` / `start_minimized` are stored and **read by nothing**.
The installer's optional "autostart" task writes a Startup-folder shortcut.
An LLM tool `autostart` writes a `.bat` that launches `python nova.py` —
the wrong program for an installed copy.

### 2.7 Gemini credentials
* BYOK: `store_byok` **always writes the key in plaintext** to
  `%APPDATA%\NOVA\api_keys.json`, then also a DPAPI copy (creds.py:121-142);
  if DPAPI fails, the DPAPI copy is deleted and plaintext is used.
* No backend-managed key path exists in practice (the gateway endpoints are
  not implemented server-side).

### 2.8 Offline models
* Ollama: Settings → Intelligence "Download" is a blocking call with no
  progress (progress_fn ignored, local_model_manager.py:285-293), no checksum.
* ONNX embedder: Library → background `hf_hub_download`, no progress UI,
  completion only in the log.
* Whisper `tiny` and the Piper voice ship inside the bundle.

### 2.9 Version and updates
* Two disagreeing constants: `APP_VERSION = "1.0.0"` (desk/bridge.py:55,
  shown in the UI) and `"0.1.0"` (nova_account.py:35, sent to the cloud).
  The installer says `1.0.0`; nova_cloud `/health` says `0.1.0`.
* **No updater.** About screen: "NOVA does not check for updates on its own."

## 3. Backend (`nova_cloud/`, ~4,100 lines, never deployed)

* **Tables** (models.py): users, profiles, devices, auth_sessions (one refresh
  family per device), email_tokens, preferences, activity_events, model_calls,
  agent_runs, error_events, feature_flags(+overrides), admin_users,
  admin_sessions, admin_audit_logs, rate_limits. USER 1–N DEVICE 1–N SESSION.
  **No INSTANCE.**
* **Auth**: Argon2id; 15-min HS256 access JWT (`sub, did, sid, ep`); opaque
  60-day refresh tokens, hashed, rotated, reuse ⇒ family revoked; `token_epoch`
  global revocation; email verification links (24 h) and password reset (1 h);
  DB fixed-window rate limits. No OAuth.
* **Sync**: scalar preferences only; `permissions` explicitly device-local;
  last-write-wins; profile get/patch; account deletion.
* **Admin**: password + TOTP, separate signing key and audience, 4 RBAC roles,
  audit log of every privileged action and every single-user read; dashboard,
  users, devices, activity, models, agents, errors, health, flags. Metadata
  only — no content or memory columns exist.
* **Telemetry**: allow-listed, scalar, short attributes; respects the user's
  `telemetry_enabled` preference.
* **Missing**: model proxy / quotas (no Gemini key server-side), instances,
  update metadata, min-version, migrations (only `create_all`).
* **Deployment artefacts**: Dockerfile (gunicorn 2×4), `render.yaml` (Render,
  Frankfurt, `api.nova.omniel.com.ng`). `deploy/` is for the **phone server**,
  not nova_cloud (systemd unit + Caddy for a duckdns name).
* **Tests**: ~190 across test_cloud_auth/security/admin/email,
  test_production_hardening, test_postgres_portability, test_db_config,
  test_account_client, test_desk_account_api.

## 4. Problems found

### Security
| Sev | Problem | Where |
|---|---|---|
| **High** | The EC2 server exposes an old NOVA phone server to the internet on plain HTTP :5050. Its `/chat` is protected only if `NOVA_TOKEN` is set in its `.env` (server_extra.py:88); I could not confirm whether it is. Anyone reaching it may be able to use the Gemini key stored on that server. | EC2, server_extra.py |
| **High** | Admin MFA can be replaced with the password alone: enrolment step 1 overwrites an enrolled admin's TOTP secret and disables MFA. | nova_cloud/api_admin.py:199-206 |
| **High** | BYOK Gemini key is always written in plaintext to `api_keys.json`. | desk/creds.py:121-142 |
| Med | Failed signup (device error) commits the user row — the email is then "taken". | nova_cloud/api_auth.py:236-237 |
| Med | Rate limits key on the client-controlled first `X-Forwarded-For`. | api_auth.py:41-45, api_admin.py:47-49 |
| Med | Email verification is never enforced after signup. | nova_cloud |
| Med | No per-account isolation of local memory/conversations. | desktop |
| Low | Revocation lags ≤10 s per worker after logout_all / reset. | auth_guard.py:30 |
| Low | GET `/verify` consumes the token, so mail link-scanners can burn it. | app.py:124-131 |
| Low | Console mail provider logs live token links; not blocked in production. | mailer.py:172-174 |

### Product / architecture
1. Login is optional; the product requires it.
2. Onboarding is one screen with the wrong questions (key/cloud/offline);
   none of account, profile, offline model, permissions, startup.
3. Two cloud clients, three device identities, two version numbers.
4. The gateway client talks to endpoints that do not exist.
5. No instance model; no per-account local data.
6. No update mechanism at all.
7. Startup preference stored but inert.
8. Offline model downloads have no progress, no checksum.

### What is good and should be kept
* nova_cloud's auth core (Argon2id, rotating refresh families, epochs, device
  binding, admin MFA/RBAC/audit, metadata-only admin, telemetry scrubbing) is
  sound and well tested. It is the foundation, not something to replace.
* The installer is per-user (no admin prompt) and already keeps user data in
  `%APPDATA%\NOVA`, separate from binaries — the precondition for safe updates.
* Permissions already exist end-to-end (storage, settings UI, enforcement).
