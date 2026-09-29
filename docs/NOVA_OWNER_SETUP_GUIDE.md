# NOVA Backend — Owner Setup Guide

This guide is for you, the owner of NOVA. It assumes no backend experience.
Follow it top to bottom once; after that, releasing a new version is the short
routine in **Part 5**.

Everything the code needs already exists in the repository. What it cannot do
on its own is create accounts in your name, buy a domain, or hold your secret
keys. Those are the steps below.

---

## Part 0 — What you are setting up

```text
 People's PCs                         Your server (AWS EC2)            Services you sign up for
 ────────────                         ─────────────────────            ────────────────────────
 NOVA.exe  ──HTTPS──▶  api.yourdomain.com  (Caddy + NOVA Cloud)  ──▶  Supabase  (database)
                                                                   ──▶  Google Gemini (AI)
                                                                   ──▶  Resend   (emails)
 NOVA.exe  ──HTTPS──▶  GitHub Releases (installer downloads, for updates)
```

| Piece | What it does, in plain English | Costs |
|---|---|---|
| **EC2 server** (you already have it) | The always-on computer that answers NOVA's requests: sign-in, the AI gateway, updates, the admin dashboard. | Free for your first year (t3.micro). |
| **Domain name** | A human name for your server, e.g. `api.yourdomain.com`. Needed for HTTPS. | ~$10–15/year, or free if you already own one (e.g. `omniel.com.ng`). |
| **Supabase** | The database that keeps every account safe even if the server dies. | Free tier. |
| **Gemini API key** | Lets your server talk to Google's AI for every signed-in user. | Pay-as-you-go at Google; you set daily limits per user in NOVA. |
| **Resend** | Sends the "confirm your email" and "reset your password" emails. | Free up to 3,000 emails/month. |
| **Release-signing key** | A secret file only you hold. It proves an update really came from you. | Free. |
| **GitHub Releases** | Where the installer files are downloaded from. | Free. |

Total to start: about **the price of a domain name**.

---

## Part 1 — Do this first: close the old server door

Your EC2 server is currently running an **old copy of NOVA from August**
(`nova.py --phone`) that anyone on the internet can reach at
`http://13.61.150.81:5050`, without encryption. It holds a Gemini key.

1. Open the **AWS Console** → **EC2** → **Instances** → click your instance.
2. Open the **Security** tab → click the **security group** name.
3. **Edit inbound rules**:
   * **Delete** the rule for port **5050**.
   * Keep **SSH (22)** but change *Source* to **My IP** (not `0.0.0.0/0`).
   * **Add** rule: **HTTP**, port **80**, source **Anywhere-IPv4**.
   * **Add** rule: **HTTPS**, port **443**, source **Anywhere-IPv4**.
   * Save.
4. On your PC, open a terminal and connect to the server:
   ```
   ssh -i %USERPROFILE%\.ssh\Omniel-server.pem ubuntu@13.61.150.81
   ```
5. Stop the old program for good:
   ```
   sudo systemctl stop nova-phone ; sudo systemctl disable nova-phone
   ```
   (If it says the service doesn't exist: `sudo pkill -f "nova.py --phone"`.)
6. **Replace the Gemini key that was on that server**: in Google AI Studio
   (Part 2, step 3) delete the old key and create a new one. Assume the old one
   was seen.

**How you know it worked:** visiting `http://13.61.150.81:5050` in a browser no
longer loads.

---

## Part 2 — Create the accounts and collect the values

Keep a **private note** (a password manager is ideal) while you do this. You
will copy these values into one file on the server in Part 3.

### 1. Domain (≈10 minutes, then up to an hour to take effect)

1. Log in where you bought your domain (e.g. Namecheap, GoDaddy, or your `.ng`
   registrar).
2. Open **DNS settings** for the domain.
3. **Add a record**:
   * Type: **A**
   * Host / Name: **api** (this makes `api.yourdomain.com`)
   * Value / Points to: **13.61.150.81**
   * TTL: automatic
4. Write down: `NOVA_DOMAIN = api.yourdomain.com`

**How you know it worked:** on your PC, `nslookup api.yourdomain.com` shows
`13.61.150.81`.

> Tip: in AWS, give the server an **Elastic IP** (EC2 → Elastic IPs → Allocate
> → Associate with your instance) so its address never changes after a reboot.
> If you do, use that address in the A record instead.

### 2. Supabase database (≈10 minutes)

The Supabase project NOVA used before **no longer exists** (the connection in
the repository's `.env` is rejected with "tenant not found"), so create a new
one.

1. Go to **supabase.com** → sign in → **New project**.
2. Name: `nova`. **Database password**: click *Generate*, and save it in your
   private note. Region: the one closest to Stockholm (e.g. *Central EU
   (Frankfurt)*).
3. When it's ready: **Project Settings** → **Database** → **Connection string**
   → **URI** → choose **Transaction pooler** (port **6543**).
4. Copy it, replace `[YOUR-PASSWORD]` with the password from step 2, and change
   the start from `postgresql://` to `postgresql+psycopg://`.
5. Write down: `DATABASE_URL = postgresql+psycopg://postgres.xxxx:PASSWORD@aws-0-....pooler.supabase.com:6543/postgres`

NOVA creates its own tables the first time it starts. You never have to touch
the database directly.

### 3. Gemini API key (≈3 minutes)

1. Go to **aistudio.google.com/apikey** → **Create API key**.
2. Put it on a Google Cloud project with **billing enabled** (the free tier's
   limits are too small for several users).
3. Write down: `NOVA_GEMINI_API_KEY = ...`

This key lives **only on your server**. It is never inside NOVA.exe. Each user
gets short-lived voice passes and a daily limit instead.

### 4. Email with Resend (≈15 minutes)

1. Go to **resend.com** → sign up.
2. **Domains** → **Add domain** → your domain → Resend shows 2–3 DNS records.
   Add each one at your domain's DNS settings (same place as step 1), exactly as
   shown. Click **Verify** in Resend once they're added.
3. **API Keys** → **Create API key** → *Sending access*.
4. Write down: `RESEND_API_KEY = re_...` and choose your addresses, e.g.
   `EMAIL_FROM = NOVA <hello@yourdomain.com>`.

### 5. Two server secrets (1 minute)

On your PC, run this twice and save both results (they must be different):
```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
Write down: `NOVA_SECRET_KEY = (first)` and `NOVA_ADMIN_SECRET_KEY = (second)`.

### 6. Your release-signing key (2 minutes; before building v1.0)

This is the key that makes automatic updates safe. Every installed NOVA will
only accept updates signed with it, **for as long as it is installed**, so:

1. In the NOVA folder on your PC, run (it asks you to choose a passphrase):
   ```
   python tools\release.py keygen --out C:\NOVA-release-key
   ```
2. It writes `packaging\update_public_key.txt` (safe to share — commit it) and
   prints `NOVA_UPDATE_PUBLIC_KEY=...` (write it down).
3. **Back up** `C:\NOVA-release-key\nova_release_private.pem` **and** its
   passphrase somewhere safe and offline (e.g. an encrypted USB stick). If you
   lose it, installed copies of NOVA can never be updated automatically again;
   everyone would have to reinstall by hand.

---

## Part 3 — Put NOVA Cloud on the server

Connect to the server (Part 1, step 4), then paste these one block at a time.

**1. Install Docker (once):**
```
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker ubuntu && newgrp docker
```

**2. Get the code:**
```
git clone <your NOVA repository address> ~/project-nova
cd ~/project-nova/deploy/nova-cloud
```
(If the repository is private, GitHub asks you to sign in; use a *personal
access token* as the password.)

**3. Fill in the settings file:**
```
cp env.production.example .env
nano .env
```
Paste each value from your private note next to its name. Save with
**Ctrl+O, Enter**, exit with **Ctrl+X**. Then lock it so only you can read it:
```
chmod 600 .env
```

**4. Start it:**
```
docker compose up -d --build
```
The first build takes a few minutes. Caddy then fetches the HTTPS certificate
automatically (your domain from Part 2 must already point here).

**5. Create your admin login (once):**
```
docker compose exec -e NOVA_ADMIN_PASSWORD='choose-a-long-password' api \
    python -m nova_cloud.manage create-admin --email you@yourdomain.com --role SUPER_ADMIN
docker compose exec api python -m nova_cloud.manage enrol-mfa --email you@yourdomain.com
```
The second command shows a code to add to an authenticator app (Google
Authenticator, 1Password, Authy…). You need that app every time you sign in to
the admin dashboard.

### How you know it is working

| Check | What you should see |
|---|---|
| Open `https://api.yourdomain.com/health` | `"status": "healthy"`, with `database` and `email` both `healthy` |
| Open `https://api.yourdomain.com/admin` | The admin sign-in page (padlock in the address bar) |
| Sign in to admin with password + authenticator code | The dashboard; **Instances & updates** shows 0 instances |
| `docker compose ps` on the server | `api` and `caddy` both running |

If `/health` says `degraded`, the `checks` part names the piece that is wrong
(usually a typo in `DATABASE_URL` or the email domain not yet verified).

---

## Part 4 — Point NOVA at your server and build v1.0

1. In the NOVA folder, open **`nova_version.py`** and set:
   ```python
   DEFAULT_CLOUD_URL = "https://api.yourdomain.com"
   ```
   This single line is what makes NOVA require sign-in and use your server.
   Leave it empty and NOVA runs locally with no accounts (development only).
2. Make sure `packaging\update_public_key.txt` contains your public key (Part 2,
   step 6).
3. Build (about 10 minutes):
   ```
   python -m PyInstaller packaging\nova_desktop.spec --noconfirm --clean
   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\NOVA-Setup.iss
   ```
   The installer appears at `packaging\out\NOVA-Setup.exe`.
4. Install it on a PC, create an account, and confirm the email arrives.

**How you know it is working:** after sign-up, the admin dashboard's
*Instances & updates* shows 1 instance, your app version, and model usage once
you talk to NOVA.

---

## Part 5 — Releasing an update (every time)

Everyone who has NOVA installed receives it automatically: downloaded in the
background, checked, installed when they next quit NOVA (or while it sits idle),
and rolled back by itself if the new version fails to start.

1. Raise the version in **`nova_version.py`** (e.g. `APP_VERSION = "1.0.1"`)
   and in **`packaging\NOVA-Setup.iss`** (`#define MyAppVersion "1.0.1"`).
2. Build (Part 4, step 3).
3. On GitHub: your repository → **Releases** → **Draft a new release** → tag
   `v1.0.1` → attach `packaging\out\NOVA-Setup.exe` → **Publish**. Right-click the
   attached file → *Copy link address*. That is your **download URL**.
4. Sign it on your PC:
   ```
   python tools\release.py sign --installer packaging\out\NOVA-Setup.exe ^
       --version 1.0.1 --url <download URL> --key C:\NOVA-release-key\nova_release_private.pem
   ```
   This writes `NOVA-Setup.exe.manifest.json` and `.manifest.sig` next to the
   installer.
5. Copy those two small files to the server and publish:
   ```
   scp -i %USERPROFILE%\.ssh\Omniel-server.pem packaging\out\NOVA-Setup.exe.manifest.* ubuntu@13.61.150.81:~/
   ssh -i %USERPROFILE%\.ssh\Omniel-server.pem ubuntu@13.61.150.81
   cd ~/project-nova/deploy/nova-cloud
   docker compose cp ~/NOVA-Setup.exe.manifest.json api:/tmp/m.json
   docker compose cp ~/NOVA-Setup.exe.manifest.sig api:/tmp/m.sig
   docker compose exec api python -m nova_cloud.manage publish-release \
       --manifest /tmp/m.json --signature /tmp/m.sig --rollout 100
   ```
   Want to be careful? Use `--rollout 20` first (one in five PCs), check the
   dashboard's *Update results* for failures, then publish again with `100`.

**Forcing everyone onto a version** (e.g. after a security fix): add
`--min-supported 1.0.1` when signing in step 4. Older copies then show
*"This version of NOVA is no longer supported. Updating now…"* and update
immediately.

**Testing on your own PC first:** find its device ID in the admin dashboard
(*Devices*), run `python -m nova_cloud.manage set-channel --device-id <id>
--channel beta` on the server, then publish with `--channel beta`.

---

## Part 6 — What must never be shared

Never paste these into chat, email, screenshots, GitHub, or a support ticket:

| Secret | Where it lives |
|---|---|
| `NOVA_SECRET_KEY`, `NOVA_ADMIN_SECRET_KEY` | server `.env` only |
| Supabase database password / `DATABASE_URL` | server `.env` only |
| `NOVA_GEMINI_API_KEY` | server `.env` only |
| `RESEND_API_KEY` | server `.env` only |
| `nova_release_private.pem` and its passphrase | your PC + offline backup only |
| `Omniel-server.pem` (SSH key) | your PC only |
| Admin password and authenticator | you only |
| AWS account password / access keys | you only |

Safe to share: `update_public_key.txt`, your domain, the installer, the
`.manifest.json`/`.sig` files.

If one of them leaks: change it at its source (Gemini/Resend/Supabase), put the
new value in `.env`, and run `docker compose up -d`. Changing
`NOVA_SECRET_KEY` signs everyone out (they simply sign in again).

---

## Part 7 — What is stored where

| On each person's PC | On your server / Supabase |
|---|---|
| Their memories, conversations, tasks, documents, files | Email, password (as an unreadable hash), name |
| Their permission choices and startup preference | Their NOVA profile (name, and the "about" text they chose to give) |
| The offline AI model | Devices and sign-in sessions (as unreadable hashes) |
| Their own Gemini key, if they chose to use one (encrypted) | Daily usage counts, app version, anonymous error counts |

NOVA does not upload people's files, memories or conversations. The admin
dashboard shows counts and account metadata, **never** conversation content or
the "about" text.

---

## Part 8 — Limits of the free setup, and when to grow

* The t3.micro (1 GB memory) comfortably serves early users: voice goes
  straight from each PC to Google, so the server only handles sign-in, text
  requests and updates. When the dashboard shows steady load, or the free year
  ends, move to a `t3.small` (Part 3 works unchanged on any Ubuntu server).
* Supabase's free tier pauses a project after a week with no activity; NOVA's
  normal traffic keeps it awake, but if it pauses, click **Restore** in the
  Supabase dashboard.
* Windows may show *"Unknown publisher"* the first time someone installs NOVA.
  Buying a code-signing certificate (~$200–400/year) removes it; updates are
  already verified by your own signature either way.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/health` → database `degraded` | Wrong `DATABASE_URL` or password | Re-copy the pooler URI (port 6543) from Supabase |
| `/health` → email `degraded` | Resend domain not verified | Finish the DNS records in Resend, click Verify |
| Browser says "not secure" | DNS not pointing at the server yet | Wait, check `nslookup`, then `docker compose restart caddy` |
| Users never get the confirmation email | Email provider not set | Set `EMAIL_PROVIDER=resend` and `RESEND_API_KEY`, then `docker compose up -d` |
| NOVA says the managed model is not configured | `NOVA_GEMINI_API_KEY` missing | Add it to `.env`, `docker compose up -d` |
| `publish-release` says *Refused: signature does not match* | Signed with a different key, or the file changed after signing | Sign again with your key; check `NOVA_UPDATE_PUBLIC_KEY` |
| Updates never arrive on a PC | That build was made without the public key inside | That build cannot self-update; install the new build by hand once |

To see what the server is doing: `docker compose logs -f api` (Ctrl+C to stop).
