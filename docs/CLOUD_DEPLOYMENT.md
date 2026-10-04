# Deploying NOVA Cloud

Everything needed to take the backend from "runs on my laptop" to something
real users' NOVA installations talk to. Written to be followed step by step.

Nothing here has been executed on your behalf: no DNS record has been changed,
no account created, no service provisioned. These are the exact steps and
values for you to apply.

---

## What you are standing up

| Piece | Choice | Why |
|---|---|---|
| Database | **Supabase** (managed Postgres) | Managed daily backups and point-in-time recovery, which is the one thing a self-hosted database will not give you for free. It is plain Postgres, so leaving is a `pg_dump`. |
| API + admin console | **NOVA Cloud** (`nova_cloud/`), on Render / Railway / Fly | Owns device identity, sync, telemetry and admin RBAC — none of which any managed auth product models. |
| Email | **Resend** | Verification, password reset and new-device notices. Swappable: the same code speaks SMTP. |
| Desktop | NOVA installer / EXE / ZIP | Points at the API via `NOVA_CLOUD_URL`. |

Identity stays in the NOVA API rather than moving to Supabase Auth. NOVA's
model is device-scoped — device secrets, epoch revocation, an admin plane on a
separate signing key — and splitting that across two systems would mean
getting revocation right twice. Supabase is used for what it is genuinely best
at: running and backing up the database.

---

## 1. Supabase

1. Create a project at supabase.com. Choose the region closest to your users
   (`eu-west-2` or `eu-central-1` for Nigeria/UK; check latency before
   committing — the region cannot be changed later).
2. Set a strong database password and store it in your password manager. It is
   shown once.
3. **Project Settings → Database → Connection string → URI**.

Take the **Connection pooler** string (port `6543`), not the direct one, then
hand it to NOVA:

```bash
python -m nova_cloud.manage set-db
```

It prompts for the URI without echoing it, and writes a gitignored `.env`.
Do that rather than exporting a shell variable, because three things go wrong
otherwise and all of them look like a wrong password:

* A shell variable lives only in the window that set it. Reopen the terminal
  and it is gone.
* **PowerShell interpolates `$` inside double quotes.** A password containing
  `$74` is silently truncated to something shorter. Single quotes are
  required, and it is easy to forget.
* `&`, `$`, `@`, `#` and `/` must be percent-encoded inside a URI. Supabase
  generates passwords containing them routinely. `set-db` does the encoding
  and verifies the password round-trips before writing anything.

It also upgrades a bare `postgresql://` to `postgresql+psycopg://`; without
that, SQLAlchemy looks for psycopg2, which NOVA does not install. Use the
pooler (6543) for the API; the direct host (5432) is for migrations.

### Creating the tables

```bash
DATABASE_URL="postgresql+psycopg://..." \
  python -c "from nova_cloud.db import init_db; init_db(); print('schema created')"
```

`init_db()` is idempotent — it creates what is missing and leaves the rest
alone. It does **not** perform migrations: once you have real users, an
altered column needs a deliberate migration, not `create_all`.

### Backups (section 59)

Supabase's free tier keeps daily backups for 7 days. That is not enough for a
database holding every NOVA account. Before you have real users:

* Upgrade to a plan with **point-in-time recovery**, and
* Take an independent copy on a schedule you control, so a mistake in the
  Supabase account itself is survivable:

```bash
pg_dump "postgresql://postgres.PROJECTREF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres" \
  --no-owner --format=custom --file "nova-$(date +%F).dump"
```

Test the restore at least once. An untested backup is a hypothesis.

---

## 2. The API

Deploy `nova_cloud/` to any host that runs Python 3.11+. Render is the least
work.

```
Build command:  pip install -r nova_cloud/requirements.txt
Start command:  gunicorn -w 4 -b 0.0.0.0:$PORT "nova_cloud.app:create_app()"
Health check:   /health
```

Set the environment variables from `nova_cloud/.env.example`. The ones that
must be right:

```
NOVA_ENV=production
NOVA_SECRET_KEY=<48 random bytes>
NOVA_ADMIN_SECRET_KEY=<a DIFFERENT 48 random bytes>
DATABASE_URL=<the Supabase pooler URI>
APP_BASE_URL=https://api.nova.omniel.com.ng
```

Generate the keys with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The app **refuses to start** in production without both keys, and refuses if
they are equal. That is deliberate: a generated key would differ between
workers and reset on every restart, silently logging everyone out.

Store them in the host's secret manager, never in the repository.

---

## 3. Email (Resend)

1. Create a Resend account and add the domain `omniel.com.ng`.
2. Resend gives you DNS records to add. **You add these — I have not touched
   your DNS.** They are additive; they do not affect the existing OMNIEL
   website.

Typically:

| Type | Name | Value | Purpose |
|---|---|---|---|
| TXT | `send.omniel.com.ng` | `v=spf1 include:amazonses.com ~all` | SPF — authorises sending |
| TXT | `resend._domainkey.omniel.com.ng` | (long key from Resend) | DKIM — signs your mail |
| MX | `send.omniel.com.ng` | `feedback-smtp.<region>.amazonses.com` (priority 10) | bounce handling |

Use the exact values Resend shows you; the table above is the shape, not the
content.

Once verified, add a DMARC record so receivers know what to do with anything
that fails the checks:

| Type | Name | Value |
|---|---|---|
| TXT | `_dmarc.omniel.com.ng` | `v=DMARC1; p=none; rua=mailto:security@omniel.com.ng` |

Start at `p=none` (monitor only). Move to `p=quarantine` after a couple of
weeks of clean reports. Going straight to `p=reject` can silently drop your own
legitimate mail.

3. Set on the API:

```
EMAIL_PROVIDER=resend
RESEND_API_KEY=<from Resend>
EMAIL_FROM=NOVA <hello@omniel.com.ng>
SUPPORT_EMAIL=support@omniel.com.ng
ADMIN_EMAIL=admin@omniel.com.ng
APP_BASE_URL=https://nova.omniel.com.ng
```

4. Verify it actually sends before turning verification on:

```bash
python -m nova_cloud.manage test-email --to you@omniel.com.ng
```

5. Only then:

```
NOVA_REQUIRE_EMAIL_VERIFICATION=1
```

Turning that on before email works would leave every new user unable to
finish signing up.

### If you prefer SMTP instead

```
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.zoho.com
SMTP_PORT=587
SMTP_USER=hello@omniel.com.ng
SMTP_PASSWORD=<app password>
```

Same templates, same flows. Nothing else changes.

---

## 4. DNS for NOVA

Subdomains keep NOVA isolated from the company website. An outage or
misconfiguration in NOVA must not be able to take `omniel.com.ng` down
(section 58).

| Record | Name | Points to | Purpose |
|---|---|---|---|
| CNAME | `api.nova` | your API host | NOVA desktop clients |
| CNAME | `admin.nova` | your API host | admin console (same app, `/admin`) |

Leave the apex `omniel.com.ng` and its existing `www`, MX and TXT records
alone. Adding subdomains does not affect them.

**Do not point NOVA at the apex domain.** If NOVA and the website share a
hostname, a bad deploy of one takes out the other.

Optionally restrict the admin console further at the host or proxy level —
IP allow-listing, or an additional access proxy. The application already
requires an admin account with MFA; this is depth, not a substitute.

---

## 5. The first administrator (section 10)

There is no default administrator and no default password. Provision the first
one deliberately, from a machine with the production `DATABASE_URL`:

```bash
export NOVA_ADMIN_PASSWORD='<a long unique passphrase from your password manager>'
python -m nova_cloud.manage create-admin \
  --email admin@omniel.com.ng --role SUPER_ADMIN
unset NOVA_ADMIN_PASSWORD
```

The password is read from a prompt or that variable, never from a command-line
argument — arguments land in shell history and in the process list, where
other users on the machine can read them.

Then enrol the second factor. MFA is required by default, and an admin who has
not enrolled is refused rather than quietly given a weaker session:

```bash
python -m nova_cloud.manage enrol-mfa --email admin@omniel.com.ng
```

Scan the QR/secret into an authenticator app and enter the code to confirm.

`admin@omniel.com.ng` is an administrator because a `SUPER_ADMIN` row was
created for it here — **not** because of its domain. The address is a
configurable value; the role is granted server-side (section 35).

---

## 6. Pointing NOVA at it

The desktop reads one variable:

```
NOVA_CLOUD_URL=https://api.nova.omniel.com.ng
```

Set it in the installer's environment, in `.env` beside `NOVA.exe`, or bake it
into the build. **Unset means NOVA runs fully local** — no account, no
sign-in screen, no telemetry. That is a supported configuration, not a broken
one, and is what every current build does.

---

## 7. Verifying the deployment

Run these against the live URL, in order. Each one fails loudly if the step
before it is wrong.

```bash
API=https://api.nova.omniel.com.ng

# 1. the service and its database are actually up
curl -s $API/health | python -m json.tool
#    expect: "status": "healthy", database "healthy" with a real latency

# 2. an account can be created
curl -s -X POST $API/v1/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"you@omniel.com.ng","password":"a-long-passphrase",
       "display_name":"You","device_id":"test-1","device_secret":"s1",
       "platform":"windows","app_version":"0.1.0"}' | python -m json.tool
#    expect: access_token, refresh_token, and email_delivery.sent == true

# 3. the email arrived — check the inbox, then verify with the link

# 4. the admin console loads and shows that user
open $API/admin
```

Then confirm in the admin console that the account you just created appears
under **Users** with its device listed. If it does not, the client reached a
different backend than the console.

---

## 8. Operating it

**Retention** — run on a schedule (cron, or the host's scheduler):

```bash
python -m nova_cloud.manage retention --dry-run    # see what would go
python -m nova_cloud.manage retention              # then do it
```

**Readiness** — before and after any deploy:

```bash
python -m nova_cloud.manage check
```

It connects to the database, reports the email provider, warns if production
is still on SQLite, and fails if no administrator has enrolled in MFA (which
would mean nobody can sign in to the console). Database passwords are redacted
from its output.

**Health** — poll `/health`; it queries the database rather than assuming it.
The admin console's System health page separates what the backend *measured*
from what clients *reported*, so a green light always says where it came from.

**Watching for trouble** — the admin console's Errors page groups by code.
`EMAIL_DELIVERY_FAILED` appearing there means messages are not reaching users,
which is otherwise invisible until someone complains.

---

## 9. What is deliberately not here

* **No OAuth yet.** The schema does not assume password-only; adding an
  `identities(provider, subject, user_id)` table is the intended path for
  Google/Apple/Microsoft/GitHub.
* **No cloud memory sync.** Conversations and NOVA's memory stay on the
  device. There is no column for them in the schema and no endpoint returns
  them.
* **No automatic updates.** Clients report `app_version` on every device and
  event, so version-correlated debugging works today and an updater can be
  added without touching identity.
* **No row-level security policies.** The API is the only client of the
  database and connects as one role, so RLS as configured today would be
  enforced against a role that can bypass it — decoration rather than defence.
  If you later expose Supabase directly to any other client, add policies
  *then*, and make the API connect as a restricted role that sets
  `app.current_user_id` per transaction.

---

## 10. Cost at this stage

| Service | Free tier | When you outgrow it |
|---|---|---|
| Supabase | 500 MB, 7-day backups | ~$25/mo for PITR — worth it before real users |
| Render / Railway | limited hours, sleeps | ~$7–20/mo for an always-on instance |
| Resend | 3,000 emails/month | ~$20/mo beyond that |

Roughly $50/month for a properly backed-up, always-on deployment. The free
tiers are fine for testing, but a sleeping API means NOVA's first request
after idle waits for a cold start, and 7-day backups are not a recovery
strategy for account data.
