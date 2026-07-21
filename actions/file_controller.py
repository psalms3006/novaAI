import os
import shutil
import json
from pathlib import Path
import logging

log = logging.getLogger(__name__)

SHORTCUTS = {
    "desktop": Path.home() / "Desktop",
    "downloads": Path.home() / "Downloads",
    "documents": Path.home() / "Documents",
    "home": Path.home()
}


def _resolve_path(path_str: str) -> Path:
    if not path_str:
        return Path.home()
    path_lower = path_str.lower()
    if path_lower in SHORTCUTS:
        return SHORTCUTS[path_lower]
    return Path(path_str).expanduser()


def execute(args: dict) -> str:
    action = args.get("action", "")
    path = _resolve_path(args.get("path", ""))

    try:
        if action == "list":
            if not path.exists():
                return f"Path not found: {path}"
            items = os.listdir(path)
            if not items:
                return f"Folder is empty: {path}"
            return "\n".join(items[:50])

        elif action == "read":
            if not path.exists():
                return f"File not found: {path}"
            if not path.is_file():
                return f"Not a file: {path}"
            content = path.read_text(encoding="utf-8", errors="replace")
            return content[:3000]  # Limit output

        elif action == "write":
            content = args.get("content", "")
            path.write_text(content, encoding="utf-8")
            return f"Written to {path}"

        elif action == "create_file":
            content = args.get("content", "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"Created file: {path}"

        elif action == "create_folder":
            path.mkdir(parents=True, exist_ok=True)
            return f"Created folder: {path}"

        elif action == "delete":
            if not path.exists():
                return f"Path not found: {path}"
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return f"Deleted: {path}"

        elif action == "find":
            name = args.get("name", "")
            if not name:
                return "Error: No search name provided."
            matches = []
            search_path = path if path.exists() else Path.home()
            for root, dirs, files in os.walk(search_path):
                for item in dirs + files:
                    if name.lower() in item.lower():
                        matches.append(os.path.join(root, item))
                if len(matches) > 20:
                    break
            if not matches:
                return f"No matches for '{name}'."
            return "\n".join(matches[:20])

        elif action == "info":
            if not path.exists():
                return f"Path not found: {path}"
            stat = path.stat()
            info = {
                "path": str(path),
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "is_file": path.is_file(),
                "is_dir": path.is_dir()
            }
            return json.dumps(info, indent=2)

        elif action == "copy":
            dest = _resolve_path(args.get("destination", ""))
            if not path.exists():
                return f"Source not found: {path}"
            if path.is_dir():
                shutil.copytree(path, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(path, dest)
            return f"Copied to {dest}"

        elif action == "move":
            dest = _resolve_path(args.get("destination", ""))
            if not path.exists():
                return f"Source not found: {path}"
            shutil.move(str(path), str(dest))
            return f"Moved to {dest}"

        else:
            return f"Unknown action: {action}"

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        log.error(f"File operation failed: {e}")
        return f"Error: {str(e)}"
