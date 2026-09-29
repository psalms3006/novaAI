"""desk.updater — keep NOVA current without asking, and never leave it broken.

    check      GET {cloud}/v1/updates/check -> manifest + Ed25519 signature,
               verified with the public key compiled into this build
               (packaging/update_public_key.txt). No key -> updates report
               themselves as not configured; nothing unverified is ever run.
    download   background, resumable, into %LOCALAPPDATA%\\NOVA\\updates\\<v>\\,
               then SHA-256 and size checked against the signed manifest.
    apply      when NOVA quits (or when idle, or at once for an update the
               server marks required). A helper script outside the install
               directory waits for NOVA to exit, copies the working install
               aside, runs the installer silently (per-user, no UAC; user data
               in %APPDATA% is never touched), then runs the new NOVA with
               --health-check-only. No health marker -> the copy is restored.
    record     updates\\history.json, and /v1/updates/report when signed in.

Applying on quit rather than at launch is deliberate: an install at launch
means seconds with no window, which is exactly the "NOVA started but nothing
appeared" failure this project has had before.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("nova.desk.updater")

CHECK_INTERVAL_S = 6 * 3600
HEALTH_TIMEOUT_S = 120

_lock = threading.Lock()
_status: dict = {"state": "idle", "current": "", "latest": None, "required": False,
                 "staged": None, "progress": None, "error": "", "last_check": 0.0}


# -- places and keys -----------------------------------------------------------

def update_dir() -> Path:
    override = os.getenv("NOVA_UPDATE_DIR", "").strip()
    if override:
        d = Path(override)
    else:
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        d = Path(base) / "NOVA" / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def public_key() -> str:
    """The release-signing public key compiled into this build."""
    if not getattr(sys, "frozen", False):
        env = os.getenv("NOVA_UPDATE_PUBLIC_KEY", "").strip()   # development only
        if env:
            return env
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    for candidate in (here / "update_public_key.txt",
                      here / "packaging" / "update_public_key.txt"):
        try:
            key = candidate.read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            continue
    return ""


def install_dir() -> Path | None:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else None


def current_version() -> str:
    from nova_version import APP_VERSION
    return APP_VERSION


def _device_id() -> str:
    try:
        import nova_lifecycle
        return str(nova_lifecycle.load().get("installation_id") or "")
    except Exception:
        return ""


def _set(**kw) -> None:
    with _lock:
        _status.update(kw)


def status() -> dict:
    with _lock:
        s = dict(_status)
    s["configured"] = bool(public_key())
    s["current"] = current_version()
    return s


# -- verification ----------------------------------------------------------------

def parse_version(v: str) -> tuple:
    parts = [int(x) for x in str(v).strip().lstrip("v").split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def verify(manifest: str, signature_b64: str, key_b64: str) -> dict:
    """The manifest as a dict if the signature is valid for it; raises otherwise."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not key_b64:
        raise ValueError("this build has no update key")
    Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64)).verify(
        base64.b64decode(signature_b64), manifest.encode("utf-8"))
    data = json.loads(manifest)
    for k in ("version", "url", "sha256", "size"):
        if not data.get(k):
            raise ValueError(f"manifest lacks {k}")
    url = str(data["url"])
    if not url.startswith("https://") and not os.getenv("NOVA_UPDATE_ALLOW_HTTP"):
        raise ValueError("update URL is not https")
    return data


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- check and download ------------------------------------------------------------

def check(base_url: str | None = None) -> dict | None:
    """Ask the server; return a verified manifest if there is something newer."""
    import requests
    from nova_version import cloud_base_url
    base = (base_url or cloud_base_url()).rstrip("/")
    key = public_key()
    if not base or not key:
        _set(state="not_configured", error="" if key else "no update key in this build")
        return None
    _set(state="checking", error="")
    r = requests.get(base + "/v1/updates/check",
                     params={"version": current_version(), "device_id": _device_id()},
                     timeout=15)
    j = r.json()
    _set(last_check=time.time(), latest=j.get("latest"), required=bool(j.get("required")))
    offer = j.get("update")
    if not offer:
        _set(state="up_to_date")
        return None
    data = verify(offer["manifest"], offer["signature"], key)
    if parse_version(data["version"]) <= parse_version(current_version()):
        _set(state="up_to_date")
        return None
    data["_required"] = bool(j.get("required"))
    return data


def _staged_path() -> Path:
    return update_dir() / "staged.json"


def staged() -> dict | None:
    try:
        s = json.loads(_staged_path().read_text(encoding="utf-8"))
        p = Path(s["installer"])
        if (p.exists() and p.stat().st_size == int(s["size"])
                and parse_version(s["version"]) > parse_version(current_version())):
            return s
    except Exception:
        pass
    return None


def download(manifest: dict) -> dict:
    """Fetch (resuming a partial file) and stage the installer after checks."""
    import requests
    version = manifest["version"]
    folder = update_dir() / version
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"NOVA-Setup-{version}.exe"
    part = target.with_suffix(".part")
    size = int(manifest["size"])
    if not (target.exists() and target.stat().st_size == size):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        _set(state="downloading", progress={"done": have, "total": size})
        with requests.get(manifest["url"], headers=headers, stream=True, timeout=(15, 120)) as r:
            if r.status_code == 200 and have:
                have = 0                                  # server ignored Range
            elif r.status_code not in (200, 206):
                raise RuntimeError(f"download failed ({r.status_code})")
            with part.open("ab" if have else "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    have += len(chunk)
                    _set(progress={"done": have, "total": size})
        os.replace(part, target)
    if target.stat().st_size != size:
        target.unlink(missing_ok=True)
        raise RuntimeError("downloaded size does not match the signed manifest")
    digest = sha256_file(target)
    if digest != manifest["sha256"].lower():
        target.unlink(missing_ok=True)
        raise RuntimeError("downloaded file does not match the signed SHA-256")
    s = {"version": version, "installer": str(target), "size": size, "sha256": digest,
         "installer_args": manifest.get("installer_args")
         or ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
         "required": bool(manifest.get("_required")), "staged_at": time.time()}
    _staged_path().write_text(json.dumps(s, indent=2), encoding="utf-8")
    _set(state="staged", staged=version, progress=None)
    return s


# -- apply -------------------------------------------------------------------

HELPER_PS1 = r'''
param([int]$NovaPid, [string]$Installer, [string]$InstallerArgs, [string]$InstallDir,
      [string]$AppExe, [string]$Version, [string]$FromVersion, [string]$UpdateDir,
      [int]$TimeoutSec, [string]$Relaunch, [string]$RelaunchArgs)
$ErrorActionPreference = 'Continue'
$LogFile = Join-Path $UpdateDir 'update.log'
$Marker = Join-Path $UpdateDir ("health-" + $Version + ".ok")
$Rollback = Join-Path $UpdateDir ("rollback\" + $FromVersion)
$ResultFile = Join-Path $UpdateDir 'result.json'
function Log($m) { Add-Content -Path $LogFile -Value ((Get-Date -Format o) + ' ' + $m) }
function Result($r, $e) {
  @{version=$Version; from=$FromVersion; result=$r; error=$e; at=(Get-Date -Format o)} |
    ConvertTo-Json | Set-Content -Path $ResultFile -Encoding UTF8
}
function Restore($why) {
  Log ("restoring " + $FromVersion + ": " + $why)
  Get-Process -Name NOVA -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
  robocopy $Rollback $InstallDir /MIR /R:3 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  Result 'rolled_back' $why
  if ($Relaunch -eq '1') { Start-Process -FilePath $AppExe -ArgumentList ('--update-failed ' + $Version) }
}
Log ("update " + $FromVersion + " -> " + $Version + " starting")
if ($NovaPid -gt 0) { Wait-Process -Id $NovaPid -Timeout 90 -ErrorAction SilentlyContinue }
Remove-Item -Recurse -Force $Rollback -ErrorAction SilentlyContinue
robocopy $InstallDir $Rollback /MIR /R:3 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Log 'could not copy the working install aside; not updating'; Result 'failed' 'rollback copy failed'; if ($Relaunch -eq '1') { Start-Process -FilePath $AppExe }; exit }
Remove-Item $Marker -ErrorAction SilentlyContinue
$p = Start-Process -FilePath $Installer -ArgumentList $InstallerArgs -Wait -PassThru
if ($p.ExitCode -ne 0) { Restore ('installer exit code ' + $p.ExitCode); exit }
$env:NOVA_DESK_PORT = '8799'
$hc = Start-Process -FilePath $AppExe -ArgumentList ('--health-check-only --post-update ' + $Version) -PassThru
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline -and -not (Test-Path $Marker)) { Start-Sleep -Seconds 2 }
if (-not $hc.HasExited) { Stop-Process -Id $hc.Id -Force -ErrorAction SilentlyContinue }
Remove-Item Env:NOVA_DESK_PORT -ErrorAction SilentlyContinue
if (-not (Test-Path $Marker)) { Restore 'the new version did not start'; exit }
Log ('installed ' + $Version)
Result 'installed' ''
Remove-Item -Recurse -Force $Rollback -ErrorAction SilentlyContinue
if ($Relaunch -eq '1') { Start-Process -FilePath $AppExe -ArgumentList $RelaunchArgs }
'''


def apply_staged(*, relaunch: bool, relaunch_args: str = "", wait_pid: int | None = None) -> bool:
    """Hand the staged update to the helper. The caller must then exit."""
    s = staged()
    inst = install_dir()
    if not s or inst is None:
        return False
    helper = update_dir() / "apply_update.ps1"
    helper.write_text(HELPER_PS1, encoding="utf-8")
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(helper),
            "-NovaPid", str(wait_pid if wait_pid is not None else os.getpid()),
            "-Installer", s["installer"], "-InstallerArgs", " ".join(s["installer_args"]),
            "-InstallDir", str(inst), "-AppExe", str(inst / "NOVA.exe"),
            "-Version", s["version"], "-FromVersion", current_version(),
            "-UpdateDir", str(update_dir()), "-TimeoutSec", str(HEALTH_TIMEOUT_S),
            "-Relaunch", "1" if relaunch else "0", "-RelaunchArgs", relaunch_args or " "]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(args, close_fds=True, creationflags=flags)
    _set(state="applying")
    log.info("[UPDATE] handing %s to the installer helper", s["version"])
    return True


def mark_healthy(version: str) -> None:
    """Called by the new version once it has started (--health-check-only)."""
    (update_dir() / f"health-{version}.ok").write_text(str(time.time()), encoding="utf-8")


def _record(entry: dict) -> None:
    p = update_dir() / "history.json"
    try:
        hist = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        hist = []
    hist.append(entry)
    p.write_text(json.dumps(hist[-50:], indent=2), encoding="utf-8")


def collect_result() -> dict | None:
    """At launch: what happened to the last update, recorded and reported."""
    p = update_dir() / "result.json"
    try:
        res = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    p.unlink(missing_ok=True)
    _record(res)
    if res.get("result") == "installed":
        _staged_path().unlink(missing_ok=True)
    try:
        import nova_account
        acct = nova_account.account()
        tok = acct.ensure_access_token() if acct.signed_in else ""
        if tok:
            acct._request("POST", "/v1/updates/report", token=tok, body={
                "result": res.get("result"), "target": res.get("version"),
                "current": current_version(), "error": res.get("error") or ""})
    except Exception:
        pass
    return res


# -- background loop -------------------------------------------------------------

_started = False


def start_background(on_required=None, is_idle=None) -> None:
    """Check now-ish and every six hours; stage what verifies. A required update
    is applied at once (after on_required() has told the person why)."""
    global _started
    if _started or install_dir() is None:
        return
    _started = True

    def loop():
        time.sleep(45)                     # let NOVA finish starting first
        while True:
            try:
                if not staged():
                    m = check()
                    if m:
                        download(m)
                s = staged()
                if s and (s.get("required") or status().get("required")):
                    if on_required:
                        on_required(s["version"])
                    apply_staged(relaunch=True)
                    time.sleep(3)
                    os._exit(0)
                elif s and is_idle and is_idle():
                    log.info("[UPDATE] NOVA is idle; applying %s now", s["version"])
                    apply_staged(relaunch=True, relaunch_args="--background")
                    time.sleep(1)
                    os._exit(0)
            except Exception as e:
                _set(state="error", error=f"{type(e).__name__}: {e}")
                log.warning("[UPDATE] %s", e)
            time.sleep(CHECK_INTERVAL_S if not staged() else 600)

    threading.Thread(target=loop, name="nova-updater", daemon=True).start()


__all__ = ["check", "download", "staged", "apply_staged", "mark_healthy", "collect_result",
           "start_background", "status", "verify", "public_key", "update_dir"]
