# Building & Distributing NOVA Desktop (Windows)

Target: a normal user downloads **NOVASetup.exe**, installs, pastes their own
Gemini API key, and launches NOVA. No Python, Git, Node, or terminal required.

## Architecture (why this shape)

NOVA Desktop = **local hybrid**: the full NOVA intelligence core (Python) is
bundled and runs in-process on the user's machine; the polished SPA talks to it
over `http://127.0.0.1:<port>` inside a native WebView2 window.

* **A. bundle backend locally** ← chosen. Preserves every capability (computer
  control, screen vision, files, MCP, offline STT) because the brain runs where
  the mouse/mic/screen are.
* B. separate background service — extra install complexity, firewall prompts,
  no benefit for a single-user desktop product.
* C. remote server — impossible for computer-control/vision features.
* D. hybrid (this design, minus a second process): UI ↔ local HTTP bridge ↔
  in-process NOVA core.

## Prerequisites (build machine only)

* Windows 10/11 x64
* Python 3.11+ with the project's requirements installed (the interpreter that
  runs `nova_desktop_app.py` in development)
* [Inno Setup 6](https://jrsoftware.org/isinfo.php) (only for the installer step)

End-user machines need nothing preinstalled except the **WebView2 Runtime**,
which ships with Windows 10/11 by default.

## Step 1 — build the application bundle

```bat
cd C:\Users\Lenovo\project-nova
python -m pip install pyinstaller
python -m PyInstaller packaging\nova_desktop.spec --noconfirm --clean
copy dist\NOVADesktop2\_internal\.env.template dist\NOVADesktop2\.env.template
```

Output: **`dist\NOVADesktop2\`** — a folder containing `NOVA.exe` plus the
bundled Python runtime, the SPA, the local embedder model, `.env.template`,
and `nova_config.toml`.

Quick sanity check before packaging:

```bat
dist\NOVADesktop2\NOVA.exe
```

The NOVA window should open. First run creates `%APPDATA%\NOVA\`.

## Step 2 — build the installer

```bat
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\NOVA-Setup.iss
```

Output: **`packaging\out\NOVA-Setup.exe`** — the distributable installer.

> `packaging\NOVA.iss` is an older script pointing at a
> `dist\NOVADesktop\` folder the spec no longer produces. Build
> `NOVA-Setup.iss`; the other fails on a missing source directory.

## Portable alternative (no installer)

```bat
powershell -Command "Compress-Archive -Path dist\NOVADesktop2\* -DestinationPath packaging\out\NOVA-portable.zip -Force"
```

Output: `packaging\out\NOVA-portable.zip` — unzip anywhere, run `NOVA.exe`.

## What the END USER does

1. Download & run `NOVASetup.exe` (or unzip the portable zip).
2. Launch **NOVA** from the Start menu / desktop.
3. One-time setup — give NOVA its Gemini key (choose one):
   * Copy `.env.template` (in the install folder) to a file named `.env` in the
     same folder, paste your key from <https://aistudio.google.com/apikey>.
   * …or set the environment variable `GEMINI_API_KEY`.
4. Restart NOVA. Talk or type. Done.

Nothing else is required — no Python, no Git, no Node, no repository clone.

## Secrets policy

* API keys are **never** bundled into the installer or embedded in the binary.
* Keys are read at runtime from (in order): the `GEMINI_API_KEY` environment
  variable → a `.env` file found by `dotenv` (next to the exe works).
* The browser/SPA only ever receives the random per-run desk token — never the
  Gemini key (verified by the smoke test's secret-leak checks).

## Notes / caveats

* `console=False` in the spec: logs still land in `%APPDATA%\NOVA\`.
* If you add heavy optional deps later, add them to `hiddenimports` in
  `packaging/nova_desktop.spec`.
* Antivirus false positives are common with unsigned PyInstaller exes; sign the
  installer (e.g. with a code-signing cert via `signtool`) before wide release.
* macOS/Linux builds would need their own specs (pywebview supports both), but
  Windows is the current target.
