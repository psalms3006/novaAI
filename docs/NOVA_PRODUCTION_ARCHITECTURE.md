# NOVA — Production Architecture

Target design for installation, onboarding, accounts, per-user instances,
model access, and automatic updates. Builds on what exists
(`NOVA_CURRENT_ARCHITECTURE.md`); nothing working is thrown away.

Owner decisions (2026-09-28) this design is built on:

| Decision | Choice |
|---|---|
| Model access | Backend issues **short-lived Gemini tokens**; the master key stays on the server |
| Hosting | API on the **EC2** (Docker + Caddy HTTPS); database on **managed Postgres** (Supabase free); email via a free-tier provider |
| Update security | **Ed25519-signed manifest + SHA-256**, files on GitHub Releases |
| Existing local data | **First account to sign in on this PC adopts it**; later accounts start empty |

---

## 1. The concepts, kept separate

```text
INSTALLER ─ puts binaries on disk. Asks nothing about the person.
DEVICE    ─ this installation on this PC. id + secret, created on first run.
ACCOUNT   ─ the person's identity (email, password; later Google/OAuth).
INSTANCE  ─ the account's personal NOVA: profile, preferences, onboarding
            state, plan/quota, and (locally) memory, tasks, history.
SESSION   ─ current authenticated access: 15-min access JWT + rotating
            60-day refresh token, one family per device.
UPDATER   ─ keeps binaries current. Never touches account or instance data.
```

```text
ACCOUNT 1───1 INSTANCE
ACCOUNT 1───N DEVICE 1───N SESSION
```

## 2. System diagram

```text
┌──────────────────────── Windows PC ─────────────────────────────┐
│ %LOCALAPPDATA%\Programs\NOVA\  ← binaries only (replaced by      │
│                                   updates; never user data)     │
│ NOVA.exe                                                        │
│  ├─ Lifecycle  (installation / device / onboarding state)       │
│  ├─ Account client  → /v1/auth, /v1/instance, /v1/sync          │
│  ├─ Model client    → /v1/model/live-token, /v1/model/generate  │
│  ├─ Updater         → /v1/updates/check → GitHub Releases       │
│  ├─ Brain (NOVA core) — started only AFTER sign-in, bound to    │
│  │    the signed-in account's data folder                       │
│  └─ UI (React): Onboarding · Login · normal NOVA                │
│                                                                 │
│ %APPDATA%\NOVA\                  ← machine level                │
│   lifecycle.json, device identity, models\, data\ (ZIM), logs   │
│   accounts\<account_id>\         ← account level (isolated)     │
│      settings.json, permissions, memory, nova_desktop.db,       │
│      tasks, documents (rag\), connected accounts, BYOK key      │
│ %LOCALAPPDATA%\NOVA\updates\     ← downloads, rollback copy     │
└─────────────────────────────────────────────────────────────────┘
              │ HTTPS (TLS by Caddy, Let's Encrypt)
              ▼
┌──────── EC2 t3.micro (eu-north-1) ────────┐    ┌──────────────┐
│ Caddy :443 → nova_cloud (gunicorn, Docker) │───▶│ Supabase     │
│  /v1/auth /v1/instance /v1/devices         │    │ Postgres     │
│  /v1/sync /v1/telemetry                    │    └──────────────┘
│  /v1/model/*  (quota, then Gemini)         │───▶ Gemini API
│  /v1/updates/check (signed manifests)      │───▶ Email provider
│  /admin (dashboard, TOTP, RBAC, audit)     │
└────────────────────────────────────────────┘
        GitHub Releases ← installers (public, integrity by signature)
```

Why this split: the t3.micro has 908 MB RAM. It comfortably runs a small
API (two gunicorn workers ≈ 150 MB) and Caddy. It should not hold the only
copy of every account (a failed disk would lose them), so the database is
managed and backed up by Supabase. Voice never flows through it.

## 3. Local state (desktop)

`%APPDATA%\NOVA\lifecycle.json` — machine level, version-independent:

```json
{
  "schema": 1,
  "installation_id": "uuid",
  "installation_initialized_at": "iso8601",
  "device_setup_completed_at": null,
  "last_account_id": "uuid-or-null",
  "adopted_legacy_data_by": "account-id-or-null",
  "last_run_version": "1.0.0"
}
```

Separate facts, never one boolean:

| Fact | Lives | Meaning |
|---|---|---|
| installation initialized | lifecycle.json | first run happened on this PC |
| device setup completed | lifecycle.json | permissions / offline model / startup chosen on this PC |
| instance onboarding completed | **server**, on the instance | profile step done for this account (any device) |
| authenticated | secure store session | a refreshable session exists |
| account / instance / device ids | session + lifecycle | who, which NOVA, which PC |
| app version | binary | never used to decide onboarding |

Launch decision:

```text
no session ──────────────────────────────▶ LOGIN (or first-run WELCOME if
                                             installation never initialized)
session ─ refresh ok / offline within grace ─┬─ instance onboarding incomplete → PROFILE step
                                             ├─ device setup incomplete       → DEVICE steps
                                             └─ both complete                 → NOVA
refresh rejected (401/403) ─────────────────▶ "Please sign in again" → LOGIN
```

Logged-out ≠ new user: `installation initialized` stays true, so a logout
shows the login screen, never the welcome/onboarding.

## 4. First-run onboarding (one time)

```text
Welcome → Account (create / sign in; verify email) → Profile (skippable
fields) → Offline model (optional; real progress; SHA-256 verified) →
Permissions (granular) → Startup preference → Initialization (only real
steps, each reported from what actually happened) → NOVA
```

* A second account on an already-set-up PC sees Account → Profile only.
* A second device for an existing account sees Account → device steps only
  (profile is already complete on the instance).
* An offline-model failure never blocks completion; retry lives in Settings.

## 5. Accounts and sessions

Reuses nova_cloud's auth core (Argon2id, rotating refresh families, token
epochs, device binding). Added / changed:

* `instances` table (one per account for now; the schema allows more later).
  JWT gains `iid`. Every user route resolves the instance **from the token**,
  never from anything the client sends.
* Email verification **enforced**: unverified accounts can sign in but only
  reach verify/resend/logout until verified.
* Providers table-ready for OAuth: `auth_identities (user_id, provider,
  subject)`; email/password is the first provider. Google can be added
  without changing sessions or instances.
* Offline: a valid local session is honoured for a grace period (30 days)
  without the network. Cloud-only features report themselves unavailable.

## 6. Model access

```text
Voice (Gemini Live):
  NOVA ──POST /v1/model/live-token──▶ backend: auth, plan, quota
       ◀── ephemeral token (≈30 min, few uses, model locked) ──
  NOVA ══ WebSocket audio directly to Gemini with that token ══▶

Text (planner, chat path, tools):
  NOVA ──POST /v1/model/generate──▶ backend: auth, quota, allow-listed model
                                    ──▶ Gemini with the server key
```

Ephemeral tokens are documented for the Live API only, so text goes through
the backend — small JSON, cheap for the t3.micro. Usage is counted per
instance per day (`model_usage`); plans define limits (`plan` on the user,
limits in server config). **Bring-your-own-key** remains available: the key
is stored DPAPI-encrypted in the account folder (never plaintext) and calls
go straight to Google.

## 7. Permissions

Granular scopes, stored per account in the account's settings, enforced at
tool dispatch (existing `_ui_safety_gate`), editable any time in Settings,
never reset by an update:

| Scope | Covers |
|---|---|
| `microphone` | voice capture, wake word |
| `screen_read` | screenshots, screen share, vision |
| `file_read` / `file_write` | reading vs creating/changing/deleting files |
| `browser_read` / `browser_interact` | research/reading vs clicking/typing in pages |
| `computer_control` | mouse, keyboard, app control |
| `exec` | commands/scripts |
| `network` / `web` | existing categories, kept |

Each is `allow / ask / deny`. Existing `files`, `screen`, `computer`, `mic`
values migrate to the new scopes (`files:allow` → read allow, write ask).

## 8. Automatic updates

```text
every start + every 6 h:
  GET /v1/updates/check?version=&channel=stable&device=
     → manifest {version, url, sha256, size, min_supported, rollout%, notes}
       + Ed25519 signature over the manifest bytes
  verify signature with the public key compiled into NOVA
  in rollout bucket? newer? ─ no → done
  download to %LOCALAPPDATA%\NOVA\updates\ (background, resumable)
  verify SHA-256 and size
  wait until idle (no voice turn, no running task) or next launch
  copy current install → updates\rollback\<old-version>\
  run installer /VERYSILENT /SUPPRESSMSGBOXES /NORESTART  (per-user,
      no UAC; data in %APPDATA% untouched)
  relaunch; new version writes a health marker within 90 s
  no marker → restore rollback copy, relaunch old version, report failure
```

* Channels: `stable` (default), `beta`, `dev` — chosen server-side per
  device; users never see non-stable unless enrolled.
* `min_supported`: an older client shows "This version of NOVA is no
  longer supported — updating now…" and updates before continuing.
* Update history: `%LOCALAPPDATA%\NOVA\updates\history.json` and telemetry.
* Never touched by updates: `%APPDATA%\NOVA\**` (accounts, sessions,
  permissions, models, memory, tasks). Onboarding state is not version-tied.

Signing: the release script signs the manifest with a private Ed25519 key
kept only by the owner (never in the repo, never on the server). The server
stores and serves the signature; it cannot forge one. A compromised server
can at worst withhold updates.

## 9. Admin dashboard

Extends the existing admin (TOTP, RBAC, audit). Adds: instances, devices
per user, **app-version distribution**, online/offline instances (last seen),
model usage and quota per plan, task counts (from telemetry), update rollout
status and failures. Still metadata only: no conversation, memory, file, or
prompt content exists server-side to show.

## 10. What is stored where

| Local only (this PC) | Cloud |
|---|---|
| memory, conversations, tasks, documents, files | account (email, password hash), profile, instance |
| permissions, startup preference | synced preferences (theme, style, …) |
| offline models, knowledge library | devices, sessions (hashed tokens) |
| BYOK key (DPAPI-encrypted) | usage counts, telemetry (scrubbed metadata) |
| session tokens (Windows Credential Manager) | update manifests, release metadata |

Nothing local is uploaded without an explicit feature asking for it.

## 11. Tradeoffs recorded

* **Memory stays local.** Simpler and more private; the cost is that a
  second device starts without the first device's memories. Sync can be
  added later as an opt-in, encrypted feature.
* **No Authenticode certificate yet.** Updates are still verified by our
  own signature; Windows may warn "unknown publisher" on the first manual
  install until a certificate is bought.
* **Rollback copies the install folder (~530 MB)** before each update. Disk
  cost for safety; the copy is removed after a healthy start.
* **Single small server.** Fine for early users; the Docker image moves to a
  larger instance or a managed platform unchanged when needed.
