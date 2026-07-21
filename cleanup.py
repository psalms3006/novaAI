"""
cleanup.py — Project NOVA safe cleanup
Run from project root:  python cleanup.py

What it does (all safe — nothing permanently deleted):
  1. Creates /archive folder
  2. Moves duplicate and backup files there
  3. Moves audio artifact files there
  4. Rotates nova.log (keeps last 500 lines in nova.log, archives rest)
  5. Clears __pycache__
  6. Reports final structure
"""

import shutil
import os
from pathlib import Path
from datetime import datetime

ROOT = Path(".")
ARCHIVE = ROOT / "archive"
ARCHIVE.mkdir(exist_ok=True)


def move_to_archive(src: Path, reason: str):
    if not src.exists():
        return
    dest = ARCHIVE / src.name
    # avoid clobbering if name already exists in archive
    if dest.exists():
        stem = src.stem
        suffix = src.suffix
        dest = ARCHIVE / f"{stem}_{int(__import__('time').time())}{suffix}"
    shutil.move(str(src), str(dest))
    print(f"  ↗ {src.name} → archive/ ({reason})")


def rotate_log(log_file: Path, keep_lines: int = 500):
    if not log_file.exists():
        return
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) <= keep_lines:
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archived_log = ARCHIVE / f"nova_{ts}.log"
    archived_log.write_text("\n".join(lines[:-keep_lines]), encoding="utf-8")
    log_file.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
    print(f"  📋 nova.log rotated — kept last {keep_lines} lines, archived {len(lines)-keep_lines} lines.")


def clear_pycache():
    for cache_dir in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for pyc in ROOT.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)
    print("  🗑  __pycache__ cleared.")


print("═" * 60)
print("Project NOVA — Safe Cleanup")
print("═" * 60)

# 1. Duplicate nova_agents
move_to_archive(ROOT / "nova_agents (1).py", "duplicate")

# 2. Duplicate nova_apply_patches
move_to_archive(ROOT / "nova_apply_patches (1).py", "duplicate")

# 3. Extra nova_apply_patches copies (keep the one with newest mtime)
patches_files = sorted(ROOT.glob("nova_apply_patches*.py"), key=lambda p: p.stat().st_mtime, reverse=True)
for dup in patches_files[1:]:
    move_to_archive(dup, "duplicate")

# 4. Backup .bak files
for bak in ROOT.glob("nova.py.bak.*"):
    move_to_archive(bak, "backup")

# 5. Audio artifacts
for audio_artifact in ["nova_response.mp3", "input.wav"]:
    move_to_archive(ROOT / audio_artifact, "audio artifact")

# 6. Test WAVs (keep wake.wav — used by nova_wake.py)
for wav in ["test_output.wav", "test.wav"]:
    move_to_archive(ROOT / wav, "test artifact")

# 7. Rotate nova.log
rotate_log(ROOT / "nova.log", keep_lines=500)

# 8. Clear pycache
clear_pycache()

# 9. Report
print()
print("Remaining root files:")
for f in sorted(ROOT.iterdir()):
    if f.name.startswith(".") or f.name == "archive":
        continue
    marker = "📁" if f.is_dir() else "📄"
    print(f"  {marker} {f.name}")

print()
print("Archived files:")
for f in sorted(ARCHIVE.iterdir()):
    print(f"  🗄  {f.name}")

print()
print("✅ Cleanup complete.")
