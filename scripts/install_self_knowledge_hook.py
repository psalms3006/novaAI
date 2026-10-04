"""Install the self-knowledge pre-commit hook.

    python scripts/install_self_knowledge_hook.py           # install
    python scripts/install_self_knowledge_hook.py --force    # overwrite a foreign hook

Idempotent: running it twice does not duplicate the hook body. Refuses to
overwrite an existing .git/hooks/pre-commit that was not installed by this
script, unless --force is given.
"""
from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / ".git" / "hooks"
HOOK_PATH = HOOKS_DIR / "pre-commit"

MARKER = "# nova-self-knowledge-hook v1"

HOOK_BODY = f"""#!/bin/sh
{MARKER}
# Refreshes context/self/nova.md from the current codebase and stages it
# if it changed, so the doc committed always matches the code committed.
# Entirely local: no network calls, so this never blocks a commit on
# something slow or unavailable.

python -m nova_self_knowledge.generate --refresh
status=$?
if [ $status -ne 0 ]; then
    echo "nova_self_knowledge: refresh failed (exit $status) -- committing without it"
    exit 0
fi
git add context/self/nova.md
exit 0
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing pre-commit hook this script did not install")
    args = p.parse_args(argv)

    if not HOOKS_DIR.exists():
        print(f"{HOOKS_DIR} does not exist -- is this a git repository?", file=sys.stderr)
        return 1

    if HOOK_PATH.exists():
        existing = HOOK_PATH.read_text(encoding="utf-8", errors="replace")
        if MARKER in existing:
            if existing == HOOK_BODY:
                print(f"{HOOK_PATH}: already installed, unchanged")
                return 0
            print(f"{HOOK_PATH}: already installed by this script; updating")
        elif not args.force:
            print(f"{HOOK_PATH} already exists and was not installed by this "
                 "script. Re-run with --force to overwrite it (this discards "
                 "whatever it currently does).", file=sys.stderr)
            return 1
        else:
            print(f"{HOOK_PATH}: overwriting a foreign hook (--force given)")

    HOOK_PATH.write_text(HOOK_BODY, encoding="utf-8")
    mode = HOOK_PATH.stat().st_mode
    HOOK_PATH.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"{HOOK_PATH}: installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
