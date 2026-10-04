import os
import shutil
import json
from pathlib import Path
import logging

log = logging.getLogger(__name__)

#: Windows known-folder GUIDs. Asking the shell is the only correct way to
#: find these: `Path.home() / "Documents"` is wrong on any machine where the
#: folder has been redirected, which since OneDrive is a great many of them —
#: the real Documents is then under ~/OneDrive/Documents and the naive guess
#: silently creates a second, empty one the user never looks in.
_KNOWN_FOLDERS = {
    "desktop":   "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures":  "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "videos":    "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "music":     "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}

#: Where each falls back to when the shell cannot be asked (non-Windows, or a
#: locked-down machine). Still never a hard-coded user name.
_FALLBACKS = {
    "desktop": "Desktop", "documents": "Documents", "downloads": "Downloads",
    "pictures": "Pictures", "videos": "Videos", "music": "Music",
}

_folder_cache: dict = {}


def user_folder(name: str) -> Path:
    """The user's real Documents/Downloads/Desktop/... folder.

    Resolved through SHGetKnownFolderPath on Windows so redirected folders —
    OneDrive most commonly — are found where they actually are.
    """
    name = name.lower()
    if name in _folder_cache:
        return _folder_cache[name]

    path = None
    guid = _KNOWN_FOLDERS.get(name)
    if guid and os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            buf = ctypes.c_wchar_p()
            gid = GUID()
            if ctypes.windll.ole32.IIDFromString(guid, ctypes.byref(gid)) == 0:
                hr = ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(gid), 0, None, ctypes.byref(buf))
                if hr == 0 and buf.value:
                    path = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
        except Exception as e:
            log.debug("known folder lookup failed for %s: %s", name, e)

    if path is None:
        path = Path.home() / _FALLBACKS.get(name, name.capitalize())
    _folder_cache[name] = path
    return path


def _resolve_path(path_str: str) -> Path:
    """Turn what the user said into a real path on this machine.

    Accepts a bare folder name ("documents"), a name plus a file
    ("documents/notes.txt", "my documents folder") or a full path.
    """
    if not path_str:
        return Path.home()
    raw = str(path_str).strip().strip('"')
    lowered = raw.lower().replace("\\", "/")

    # "my documents", "the downloads folder", "Documents/report.pdf"
    head, _, rest = lowered.partition("/")
    head = head.replace("my ", "").replace("the ", "").replace(" folder", "").strip()
    if head == "home":
        base = Path.home()
    elif head in _KNOWN_FOLDERS:
        base = user_folder(head)
    else:
        return Path(raw).expanduser()
    # Preserve the original casing of anything after the folder name.
    tail = raw.replace("\\", "/").split("/", 1)[1] if "/" in raw else ""
    return (base / tail) if tail else base


#: Kept for callers that still read it; each entry resolves for real.
SHORTCUTS = {name: user_folder(name) for name in _FALLBACKS}
SHORTCUTS["home"] = Path.home()


def _verify_written(path: Path) -> str:
    """Confirm a file is really there before saying so.

    NOVA must never report a save she did not perform. A write that raised is
    obvious; a write that silently went somewhere unexpected is not, and
    "I've saved it to Documents" is exactly the sentence a user will not
    check.
    """
    try:
        if not path.exists():
            return f"FAILED: nothing was written to {path}"
        return f"Saved to {path} ({path.stat().st_size} bytes)"
    except Exception as e:
        return f"FAILED: could not confirm {path} exists ({e})"


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

        elif action in ("write", "create_file"):
            content = args.get("content", "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return _verify_written(path)

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
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(path, dest)
            return _verify_written(dest)

        elif action == "move":
            dest = _resolve_path(args.get("destination", ""))
            if not path.exists():
                return f"Source not found: {path}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            moved = _verify_written(dest)
            if path.exists():
                return f"{moved} (warning: {path} is still there)"
            return moved

        else:
            return f"Unknown action: {action}"

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        log.error(f"File operation failed: {e}")
        return f"Error: {str(e)}"
