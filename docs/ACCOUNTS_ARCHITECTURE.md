# NOVA accounts, cloud and control plane

How identity works in NOVA, what crosses the network, what does not, and where
the trust boundaries are. Written to be read instead of the code.

---

## 1. The shape of the system

```
                        NOVA CLOUD  (nova_cloud/)
              identity · devices · sync · telemetry · admin
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
   NOVA Windows              NOVA macOS                NOVA Linux
        │                         │                         │
   local runtime            local runtime             local runtime
   voice · agents · tools · models · files · knowledge
```

An **account** is the identity. A **device** is one installation belonging to
that account. One computer can host several devices, because two people
sharing a machine each get their own.

The cloud connects a person's NOVA instances. It does not run them. Every
capability that matters — voice, tools, files, local models, ZIM knowledge,
maps — executes on the machine and keeps working when the backend is
unreachable.

### Deployment shapes

| Configuration | Behaviour |
|---|---|
| No `NOVA_CLOUD_URL` | Fully local. No account, no sign-in screen, no telemetry. This is the default. |
| `NOVA_CLOUD_URL` set, signed out | Sign-in gate appears once at launch. |
| `NOVA_CLOUD_URL` set, signed in | Session restored from the OS keystore; straight to voice-ready. |
| Signed in, network gone | Everything local continues. Cloud-only features report offline. |

---

## 2. Components

| Path | Role |
|---|---|
| `nova_cloud/` | The backend: Flask + SQLAlchemy. SQLite by default, Postgres via `DATABASE_URL`. |
| `nova_cloud/static/admin.*` | The admin control plane, served at `/admin`. |
| `nova_cloud/manage.py` | Operator CLI: create admins, enrol MFA, retention, serve. |
| `nova_account.py` | The desktop's account client. Owns session state, sync and telemetry. |
| `nova_secure_store.py` | Cross-platform secret storage. |
| `desk/account_api.py` | Desktop HTTP surface (`/api/account/*`). |
| `desk/static/account.js` | Sign-in gate and account panel in the SPA. |

Flask and SQLAlchemy were chosen because the repository already runs on them.
One service, one database. There is no queue, no cache tier and no second
process, because nothing here needs one yet.

---

## 3. Identity

```
user_id     immutable UUID          the identity
email       mutable attribute       can change without breaking anything
device_id   immutable UUID          one installation, one account
```

`user_id` is never derived from the email, and `device_id` is never derived
from hostname, OS username, MAC address or disk serial. All of those change —
a new network card, a renamed machine, a cloned VM — and none of them is
secret, so none can authenticate anything.

A device proves itself with a secret it generated locally. The server stores
only `sha256(secret)`. Knowing a `device_id` is therefore not enough to
impersonate an installation.

### Two accounts on one computer

`installation_identity` is shared by the machine. Each account gets its own
entry in `device_identities`, keyed by email. Consequences, all intended:

* Neither account can see the other's devices or preferences.
* Revoking one person's device does not sign the other out.
* Signing out and back in reuses the same device row rather than accumulating
  a new one on every sign-in.

---

## 4. Authentication

### Passwords

Argon2id (`time_cost=3`, `memory_cost=64 MiB`, `parallelism=2`). Plaintext
passwords are never stored, logged, or returned by any endpoint, and no admin
endpoint exposes a password hash. Policy is a 10-character minimum plus a
small list of obvious passwords; there is deliberately no "must contain a
symbol" rule, which pushes people towards predictable substitutions without
adding entropy.

### Tokens

| Token | Form | Lifetime | Revocation |
|---|---|---|---|
| Access | Signed JWT (HS256) | 15 min | Immediate via `token_epoch`; also on device revoke |
| Refresh | Opaque, stored as SHA-256 | 60 days | Rotated on every use; family killed on reuse |
| Admin | Signed JWT, **separate key** | 30 min | Database-checked on every request |

The access token is stateless so the hot path costs a signature check rather
than a database round trip. It carries the user's `token_epoch`; raising that
number invalidates every outstanding token at once, which is what makes
password reset and "sign out everywhere" immediate.

Refresh rotation gives leak detection: presenting an already-retired token
means it was copied, so the whole family for that device is revoked.

Admin tokens use a **different signing key and a different audience**. A user
token cannot validate as an admin token and vice versa — verified by test, not
just by routing.

### Offline grace

After a successful authentication the device records `last_verified`. While
the backend is unreachable the cached identity stays valid for
`offline_grace_s` (30 days by default). A failed refresh is *not* a sign-out;
only an explicit 401/403 from the backend clears the session. Losing your
connection must never lose your assistant.

---

## 5. What syncs and what does not

**Cloud-synced** — things that describe the person:

`theme`, `accent`, `density`, `animations`, `response_style`, `history_turns`,
`show_tool_activity`, `voice_responses`, `continuous_conversation`,
`barge_in`, `memory_enabled`, `user_system_prompt`, `locale`, quiet hours,
`proactive_enabled`, and the privacy toggles.

**Device-local** — things that describe the machine:

`input_device`, `output_device`, `mic_gain`, `speaker_volume`, `ollama_url`,
`local_model`, `zim_path`, `maps_path`, `download_dir`, `permissions`,
`developer_mode`, window bounds, ambient orb position.

Syncing a microphone name or a `C:\` path to another machine would actively
break it. The split is enforced server-side: a device-local key sent to
`/v1/sync/preferences` is rejected and reported back, so a client is never
left believing it synced.

### Conversations and memory

Conversation history and NOVA's memory stay on the device. They are not
uploaded, there is no column for them anywhere in the schema, and no admin
endpoint returns them. Cloud memory sync is not implemented; if it ever is, it
will be an explicit, separately-consented feature rather than something that
silently starts happening.

### Conflicts

Last-write-wins on the client-supplied `updated_at`, with the writing device
recorded. An older write does not clobber a newer value; the server reports
the conflict and the newer value so the client can adopt the winner instead of
diverging silently. This is the right rule for scalar preferences — the newest
thing the human chose is what they want — and anything needing stronger
guarantees does not belong in that table.

---

## 6. Privacy

The rule the code enforces: **operational metadata in, private content never**.

Clients are not trusted to respect it. `nova_cloud/telemetry_sink.py` filters
every attribute through an allow-list, accepts only scalars, caps string
length, refuses anything structured, and drops keys matching
`transcript|prompt|completion|message|content|text|audio|recording|
conversation|utterance|query|password|token|secret|api_key`. Dropped, not
truncated or hashed.

What is collected:

```
Task type: web_search    Model: gemini-3.1-flash    Latency: 1.2s
Status: success          Device: windows            Version: 0.1.0
```

What is never collected: microphone audio, transcripts, prompts, completions,
file contents, conversation text.

IP addresses are stored as a keyed HMAC, never in the clear — enough to see
that logins came from the same place, not a record of where someone is.

Users can turn telemetry off. The server honours it: the batch is
acknowledged and discarded, so the control is real rather than an
honour-system flag on the client.

---

## 7. Trust boundaries

```
LOCAL DEVICE  →  NOVA RUNTIME  →  CLOUD BACKEND  →  ADMIN CONTROL PLANE
```

Rules, each backed by a test in `tests/test_cloud_security.py`:

1. Identity comes from the token, never the request body. A client sending
   `{"user_id": "..."}` is ignored.
2. A user token cannot reach any admin endpoint (different key, different
   audience).
3. An admin token cannot be used on the user API.
4. A revoked device loses access, and cannot refresh its way back.
5. A disabled account loses access on every device immediately.
6. Signing an admin out ends that session on the next request — admin sessions
   are checked against the database, not merely trusted from the signature.
7. Roles are re-read from the database on every request, so a demotion applies
   at once.

### Admin roles

| Permission | SUPER_ADMIN | ADMIN | SUPPORT | ANALYST |
|---|:--:|:--:|:--:|:--:|
| Dashboard, health, errors | ✓ | ✓ | ✓ | ✓ |
| Activity, agents, models | ✓ | ✓ | partial | ✓ |
| View users and devices | ✓ | ✓ | ✓ | — |
| Disable / enable accounts | ✓ | ✓ | — | — |
| Revoke sessions and devices | ✓ | ✓ | ✓ | — |
| Edit feature flags | ✓ | ✓ | — | — |
| View audit log | ✓ | ✓ | — | — |
| Manage administrators | ✓ | — | — | — |

Anything not listed is denied: adding an endpoint without granting it
explicitly fails closed. The console builds its navigation from the signed-in
role's permissions, so a section a role cannot use is absent rather than shown
and then failing.

### Administrators cannot read user content

There is no endpoint that returns conversations, transcripts or memory. The
user detail view says so on the page, because it is a deliberate limit and
should not be mistaken for an oversight. Opening an individual user record is
itself written to the audit log.

---

## 8. Never blocking NOVA

Two hard rules:

* **Telemetry is fire-and-forget.** `emit()` puts one item on a bounded queue
  and returns. The queue drops rather than growing. A background thread
  batches and posts.
* **Sync is never awaited by the UI.** A preference applies locally first and
  is pushed afterwards on a background thread.

Measured with a live backend, signed in:

| Operation | mean | p99 |
|---|---|---|
| `emit()` one event | 3.0 µs | 6.2 µs |
| `emit_model_call()` | 3.1 µs | 3.6 µs |
| `display_name` | 1.0 µs | 1.3 µs |
| cached token check | 1.0 µs | 2.0 µs |

Identical with the backend unreachable. The only call that can touch the
network on a user's behalf is `ensure_access_token()`, and only when the
cached token has actually expired.

On shutdown `nova_account.shutdown()` flushes the queue. Without it, every
event still inside the batch window is lost when NOVA closes — including
`NOVA_STOPPED`, which is by definition queued at exactly that moment.

---

## 9. Data model

```
users ──┬── profiles
        ├── devices ──── auth_sessions
        ├── preferences
        ├── email_tokens
        └── (activity_events, model_calls, agent_runs, error_events)

feature_flags ── feature_flag_overrides
admin_users ──── admin_sessions
admin_audit_logs
rate_limits
```

Deleting an account removes the user, profile, devices, sessions and
preferences, and **de-identifies** the telemetry rows rather than dropping
them, so platform-level counts stay honest while nothing remains linked to the
person. Deletion requires the current password and an explicit
`{"confirm": "DELETE"}`.

### Retention

| Category | Default | Setting |
|---|---|---|
| Activity and model telemetry | 90 days | `NOVA_RET_TELEMETRY` |
| Error events | 90 days | `NOVA_RET_ERRORS` |
| Admin audit logs | 730 days | `NOVA_RET_AUDIT` |
| Auth events | 180 days | `NOVA_RET_AUTH` |

Run `python -m nova_cloud.manage retention` (add `--dry-run` first). It is a
scheduled job rather than a thread in the web process, so it is predictable
and can be rehearsed.

---

## 10. Secrets

| Secret | Where it lives |
|---|---|
| User password | Argon2id hash, server only |
| Refresh token | SHA-256 hash server-side; plaintext only in the client's OS keystore |
| Device secret | SHA-256 hash server-side; plaintext only in the client's OS keystore |
| Admin TOTP seed | Encrypted with the admin signing key |
| Signing keys | Environment (`NOVA_SECRET_KEY`, `NOVA_ADMIN_SECRET_KEY`) |
| Gemini / provider keys | `desk/creds.py`, DPAPI-sealed — a **separate** domain |

Account credentials and model-provider credentials are stored separately on
purpose. They protect different things and should not share a store.

The backend refuses to start in production without real signing keys rather
than generating one, which would differ between workers and reset on restart.
It also refuses if the two keys are equal.

**Client-side storage** (`nova_secure_store.py`): Windows Credential Manager,
macOS Keychain, Linux Secret Service, via `keyring`. The backend is probed
with a real round trip, because an importable keyring is not a working one —
on Linux without a secret service it fails only when used. Where none is
available there is an owner-only encrypted file fallback, and the UI reports
which backend is actually in use rather than implying everything is
hardware-backed.

---

## 11. Running it

```bash
export NOVA_SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export NOVA_ADMIN_SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export DATABASE_URL=postgresql+psycopg://user:pass@host/nova   # optional

python -m nova_cloud.manage create-admin --email you@example.com --role SUPER_ADMIN
python -m nova_cloud.manage enrol-mfa    --email you@example.com
python -m nova_cloud.manage serve --host 0.0.0.0 --port 8080
```

Point clients at it with `NOVA_CLOUD_URL=https://cloud.example.com`. The admin
console is at `/admin`.

MFA is required by default (`NOVA_ADMIN_REQUIRE_MFA`); an administrator who has
not enrolled is refused rather than quietly given a weaker session.

Deploy behind TLS. The app sets HSTS, `nosniff`, `DENY` framing, a
`'self'`-only CSP and `no-store` on every response, but it does not terminate
TLS itself.

---

## 12. Known limits

* **Email delivery is not implemented.** Verification and reset tokens are
  generated and honoured, but nothing sends them. Outside production the token
  is returned in the response so the flow can be completed; in production it
  never is, so the feature is inert until an operator wires up a mailer.
  `NOVA_REQUIRE_EMAIL_VERIFICATION` is therefore off by default.
* **OAuth is not implemented.** The schema does not assume password-only —
  `password_hash` is the only credential today, and adding an
  `identities(provider, subject, user_id)` table is the intended path.
* **Cloud memory sync is not implemented.** Deliberate; see §5.
* **Rate limiting is per-IP and per-account**, in the database. Behind a proxy
  it depends on `X-Forwarded-For` being set correctly.
* **Automatic updates are not implemented.** Clients report `app_version` on
  every device and event, so version-correlated debugging works now and an
  updater can be added without touching identity.
