"""Reading the volume must not kill NOVA a moment later.

`actions/_audio_win.py` handed out a `POINTER(IAudioEndpointVolume)` that no
caller released. comtypes then ran `IUnknown::Release` from the pointer's
finaliser, at whatever later allocation happened to trigger a garbage
collection -- in practice an ordinary `import shutil` -- by which point the COM
apartment could already be gone. The result is a `0xC0000005` access violation
that kills the interpreter outright: no Python exception, no traceback, and
nothing in NOVA's own log.

It reproduced in 3 of 8 runs before the fix, so this test runs the probe
repeatedly. A single green run proves nothing about a use-after-free.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows COM audio path"
)

RUNS = 6

PROBE = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, %(repo)r)
    from actions import computer_settings
    computer_settings.execute({"action": "get_volume"})
    # An innocent allocation, of the kind any turn does straight afterwards.
    import shutil
    from actions import file_controller
    print("SURVIVED")
    """
)


def _repo_root() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parents[1])


def test_reading_the_volume_leaves_the_process_alive():
    source = PROBE % {"repo": _repo_root()}
    crashes = []
    for attempt in range(RUNS):
        proc = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", source],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0 or "SURVIVED" not in proc.stdout:
            crashes.append((attempt, proc.returncode, proc.stderr[-400:]))

    assert not crashes, (
        f"{len(crashes)} of {RUNS} runs died after a volume read. "
        f"First: exit={crashes[0][1]}\n{crashes[0][2]}"
    )


def test_the_module_defines_each_helper_once():
    """`get_volume` was defined twice; the second silently shadowed the first."""
    import ast
    from pathlib import Path

    path = Path(_repo_root()) / "actions" / "_audio_win.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert duplicates == [], f"defined more than once: {duplicates}"
