@echo off
rem NOVA Desktop app launcher (double-click to run, no console window)
rem Uses system Python (not Hermes venv) - Hermes-free build
cd /d "%~dp0"
rem Try pythonw.exe from PATH first, fall back to python.exe
where pythonw.exe >nul 2>&1
if %errorlevel% equ 0 (
    start "" pythonw.exe "%~dp0nova_desktop_app.py" > "%~dp0nova_desktop.log" 2>&1
) else (
    where python.exe >nul 2>&1
    if %errorlevel% equ 0 (
        start "" python.exe "%~dp0nova_desktop_app.py" > "%~dp0nova_desktop.log" 2>&1
    ) else (
        echo ERROR: Python not found in PATH. Please install Python 3.11+.
        pause
    )
)
