# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the NOVA Desktop application (Windows).

Produces dist/NOVADesktop/ — a self-contained folder whose NOVA.exe boots the
full NOVA intelligence stack (nova.py) plus the WebView2 SPA shell.

Build:  python -m PyInstaller packaging/nova_desktop.spec --noconfirm --clean
"""
import os
import sys
from PyInstaller.utils.hooks import collect_submodules  # kept for future use if needed

block_cipher = None
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# Auto-detect Python shared library
_python_dll = os.path.join(os.path.dirname(sys.executable), "python311.dll")
if not os.path.exists(_python_dll):
    # Try uv-managed Python
    _python_dll = r'C:\Users\Lenovo\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python311.dll'
_binaries = []
if os.path.exists(_python_dll):
    _binaries = [(_python_dll, '.')]

hidden = [
    # web stack
    "flask", "flask.json", "jinja2", "werkzeug", "itsdangerous", "click",
    "requests", "urllib3",
    # TLS trust via the OS certificate store — without this, HTTPS fails on any
    # machine with antivirus HTTPS scanning or a corporate TLS proxy.
    "nova_tls", "truststore", "truststore._api", "truststore._windows",
    # one shared voice model + Core-owned proactive speech
    "nova_voice", "nova_core_voice", "nova_heartbeat",
    # account identity, cross-platform secret storage and telemetry. keyring
    # resolves its backend at runtime, so the platform backends have to be
    # named explicitly or the packaged app silently falls back to the file
    # store even where a real keystore exists.
    "nova_account", "nova_secure_store",
    # Runtime spine + document library. onnxruntime is ~15 MB and lets the
    # packaged app run the same embedding model that previously needed torch
    # (~2 GB) and was therefore excluded, degrading semantic search to
    # keywords in the EXE only. The model itself is downloaded to the user's
    # data directory, not bundled.
    "nova_core", "nova_core.permissions", "nova_core.trust",
    "nova_core.rag", "nova_core.rag.parsers", "nova_core.rag.chunking",
    "nova_core.rag.embeddings", "nova_core.rag.store",
    "nova_core.rag.library", "nova_core.rag.api",
    "onnxruntime", "tokenizers",
    "pypdf", "docx", "openpyxl", "pptx",
    "keyring", "keyring.backends", "keyring.backends.Windows",
    "keyring.backends.macOS", "keyring.backends.SecretService",
    "keyring.backends.chainer", "keyring.backends.fail",
    "keyring.backends.null",
    "_ssl", "ssl",
    # websocket support (Live voice)
    "flask_sock", "simple_websocket",
    # GUI shell (pywebview -> WebView2 via .NET)
    "webview", "webview.platforms.winforms", "webview.platforms.edgechromium",
    "clr", "pythonnet",
    # audio / stt
    "sounddevice",
    "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
    # tts (pyttsx3 SAPI5 driver)
    "pyttsx3", "pyttsx3.drivers", "pyttsx3.drivers.sapi5", "comtypes",
    # vision / math
    "PIL", "numpy",
    # gemini SDK (live + chat)
    "google.genai", "google.genai.types",
    "grpc", "proto",
    # nova_intelligence package
    "nova_intelligence", "nova_intelligence.connectivity",
    "nova_intelligence.provider", "nova_intelligence.ollama_provider",
    "nova_intelligence.gemini_provider", "nova_intelligence.router",
    "nova_intelligence.local_model_manager", "nova_intelligence.local_runtime",
    "nova_intelligence.offline_knowledge", "nova_intelligence.voice_provider",
    # desk modules
    "desk", "desk.bridge", "desk.chat", "desk.voice", "desk.store",
    "desk.settings", "desk.confirm", "desk.projects", "desk.live_session",
    "desk.win_overlay", "desk.creds",
    # Ambient screen awareness. mss resolves its platform backend at import
    # time, so naming it here is what stops the packaged build from reporting
    # "screen sharing unavailable" on a machine where it works fine in source.
    "desk.screen_share", "mss", "mss.windows", "mss.base",
    # Document generation. These are imported lazily inside the writers, so
    # static analysis does not see them and the packaged app would report
    # "a required library is missing" on a machine where it is not.
    "actions.generate_document", "docx", "reportlab",
    "reportlab.platypus", "reportlab.lib.styles", "reportlab.lib.pagesizes",
    "openpyxl", "pptx",
    # In-application control. pywinauto is imported inside the functions that
    # use it, so static analysis never sees it and the packaged app would say
    # "UI automation is unavailable on this machine" while working fine from
    # source. The UIA backend reaches Windows through comtypes, which builds
    # its COM wrappers into comtypes.gen at runtime.
    "pywinauto", "pywinauto.keyboard", "pywinauto.timings",
    "pywinauto.controls", "pywinauto.controls.uia_controls",
    "pywinauto.uia_defines", "pywinauto.uia_element_info",
    "comtypes.client", "comtypes.gen", "comtypes.stream",
    # mind map (ES modules served as static files, not imported by Python)
    # core modules
    "core", "core.event_bus", "core.boot", "core.verification_engine",
    # orchestrator modules (used by desk.chat for trust-verified tool execution)
    "orchestrator", "orchestrator.orchestrator", "orchestrator.plan",
    # capability registry
    "capabilities", "capabilities.registry", "capabilities.contracts",
    # search + offline knowledge — both resolved via importlib at call time,
    # so static analysis misses them
    "ddgs", "duckduckgo_search", "libzim",
    # misc runtime
    "psutil", "sqlite3",
]

# Tool modules and subsystems that are resolved dynamically (importlib /
# function-local imports), so PyInstaller's static analysis cannot see them.
# Without this, actions.vision, actions.file_processor, core.execution_engine
# and the memory.* layers were simply absent from the build and the tools
# reported themselves as missing at runtime.
for _pkg in ("actions", "capabilities", "core", "orchestrator", "memory",
             "trust", "integrations", "tools", "agent"):
    try:
        hidden += [m for m in collect_submodules(_pkg)
                   if not m.endswith(("._init_", ".__main__", "._smoke_test"))]
    except Exception:
        pass
# Only submodules that actually exist in the installed google-genai. Listing
# names that do not exist made PyInstaller emit "ERROR: Hidden import not
# found" for each, which hid real build failures in the noise.
hidden += [
    "google.genai", "google.genai.types", "google.genai._api_client",
    "google.genai._common",
    "google.genai.live", "google.genai.models",
    "google.genai.files", "google.genai.caches", "google.genai.batches",
    "google.genai.tokens",
    # legacy SDK — still imported lazily by actions/dev_agent.py, agent/executor.py
    # and agent/planner.py, so it has to be collected for those paths to work.
    "google.generativeai", "google.generativeai.models",
]

a = Analysis(
    [os.path.join(ROOT, "nova_desktop_app.py")],
    pathex=[ROOT],
    binaries=_binaries,    datas=[
        # the SPA + vendored js/css
        (os.path.join(ROOT, "desk", "static"), "desk/static"),
        # NOTE: nova_embedder is deliberately NOT bundled. Loading it needs
        # sentence-transformers, which needs torch + transformers — all three
        # are excluded below to keep the installer near 200 MB rather than
        # several GB. Shipping the 90 MB model without its runtime added dead
        # weight to every download and could never load. Memory still works in
        # the packaged app: facts persist, and retrieval falls back to the
        # lexical search in memory_extra._lexical_search / living_memory.search.
        # To re-enable semantic search, drop torch/transformers from `excludes`,
        # add "sentence_transformers" to `hidden`, and restore the line below.
        # (os.path.join(ROOT, "nova_embedder"), "nova_embedder"),
        # The second element is the destination DIRECTORY, not a filename.
        # Naming the file there produced _internal/nova_config.toml/ as a
        # folder with the file inside it, so the config loader -- which quite
        # correctly tests is_file() -- skipped it and the packaged app ran on
        # defaults for every setting the file exists to control. It only
        # looked fine in development because the working directory was the
        # repo, which has a real nova_config.toml in it.
        # first-run key template (users copy it next to NOVA.exe as ".env")
        (os.path.join(ROOT, ".env.template"), "."),
        # runtime config template (model selection etc.) — no secrets inside
        (os.path.join(ROOT, "nova_config.toml"), "."),
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "torchvision", "torchaudio",       # not required by faster-whisper
        "matplotlib", "IPython", "pytest", "tkinter",
        "PyQt5", "PySide2", "PySide6",
        "scipy", "sklearn", "transformers", "tensorflow", "onnxruntime",
        "av", "librosa", "soundfile", "numba",
        "playwright",
        "uvicorn", "starlette", "gunicorn",
        # NOTE: pydantic is REQUIRED by google.genai v2.x — must NOT be excluded.
        "rich", "pygments", "orjson",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NOVA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed app; logs land in %APPDATA%\NOVA
    icon=os.path.join(ROOT, "packaging", "nova.ico") if os.path.exists(
        os.path.join(ROOT, "packaging", "nova.ico")) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="NOVADesktop2",
)
