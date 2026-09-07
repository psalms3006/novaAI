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
    # mind map (ES modules served as static files, not imported by Python)
    # core modules
    "core", "core.event_bus", "core.boot", "core.verification_engine",
    # orchestrator modules (used by desk.chat for trust-verified tool execution)
    "orchestrator", "orchestrator.orchestrator", "orchestrator.plan",
    # capability registry
    "capabilities", "capabilities.registry", "capabilities.contracts",
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
        # first-run key template (users copy it next to NOVA.exe as ".env")
        (os.path.join(ROOT, ".env.template"), ".env.template"),
        # runtime config template (model selection etc.) — no secrets inside
        (os.path.join(ROOT, "nova_config.toml"), "nova_config.toml"),
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
