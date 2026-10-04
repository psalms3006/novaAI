"""desk.notify — a Windows notification, for things that must reach the person
even when NOVA's window is minimised and her voice is off: a reminder that is
due, an automation that ran.

No extra package: Windows PowerShell 5.1 can reach the WinRT toast API
directly. A toast needs an AppUserModelID that Windows knows; PowerShell's
built-in ID exists on every Windows 10/11, and the notification's own title
says "NOVA".
"""
from __future__ import annotations

import logging
import platform
import subprocess
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

#: Known to every Windows 10/11 install, so a toast always has somewhere to go.
_FALLBACK_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{text}</text></binding></visual></toast>
'@)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{aumid}').Show($toast)
"""


def toast(title: str, text: str, aumid: str = _FALLBACK_AUMID) -> bool:
    """Show a Windows notification. True if Windows accepted it."""
    if platform.system() != "Windows":
        return False
    script = (_SCRIPT.replace("{title}", escape(title[:120]))
              .replace("{text}", escape(text[:300]))
              .replace("{aumid}", aumid.replace("'", "")))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log.info("notification not shown: %s", e)
        return False
    if r.returncode != 0:
        log.info("notification not shown: %s", (r.stderr or "").strip()[:200])
        return False
    return True


__all__ = ["toast"]
