"""The phone's built-in terminal: CMD or PowerShell commands typed on the phone, run on this PC.

Each phone keeps its own working folder ("cd" works as in a real window). Typing a command and pressing Run
is the user's own decision, so ordinary commands run at once; PowerShell, installs, admin rights and
destructive commands are sent back for a second "yes" first (the same rule as the assistant).
"""

from __future__ import annotations

import os
import threading
from typing import Any

import files
import shell
from i18n import t as tr

MARK = "@@WR_CWD@@"
TIMEOUT = 120
OUTPUT_LIMIT = 30_000

_lock = threading.Lock()
_cwd: dict[str, str] = {}


def cwd(device: str) -> str:
    with _lock:
        here = _cwd.get(device) or str(files.HOME)
    return here if os.path.isdir(here) else str(files.HOME)


def set_cwd(device: str, path: str) -> str:
    """The browse view moved to another folder (relative paths are from the current one)."""
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    target = os.path.normpath(os.path.join(cwd(device), path)) if path else cwd(device)
    if not os.path.isdir(target):
        raise ValueError(tr(f"المجلد غير موجود: {target}", f"No such folder: {target}"))
    with _lock:
        _cwd[device] = target
    return target


def needs_confirm(command: str, sh: str) -> dict[str, Any] | None:
    danger = shell.is_dangerous(command)
    if danger:
        return {"danger": True, "reason": tr("أمر قد يحذف أو يغيّر أشياء مهمة", "This command may delete or change important things")}
    if shell.needs_ok(command, sh):
        return {"danger": False, "reason": tr("PowerShell أو تثبيت أو صلاحيات مسؤول: يحتاج موافقتك",
                                              "PowerShell, an install or admin rights: needs your OK")}
    return None


def _wrapped(command: str, sh: str) -> str:
    """The command, then a marker and the folder it ended in (so "cd" is remembered)."""
    if not shell.IS_WIN:
        return f"{command}\necho {MARK}; pwd"
    if sh == "cmd":
        return f"{command} & echo {MARK} & cd"
    return (f"try {{ Invoke-Expression @'\n{command}\n'@ }} catch {{ Write-Output $_ }}; "
            f"Write-Output '{MARK}'; (Get-Location).Path")


def run(device: str, command: str, sh: str = "cmd", confirm: bool = False, window: bool = False) -> dict[str, Any]:
    command = (command or "").strip()
    sh = "cmd" if str(sh).lower() in ("cmd", "cmd.exe") else "powershell"
    here = cwd(device)
    if not command:
        return {"ok": False, "error": tr("اكتب أمراً", "Type a command"), "cwd": here}
    need = needs_confirm(command, sh)
    if need and not confirm:
        return {"ok": False, "confirm": True, **need, "cwd": here, "shell": sh}
    if window:  # interactive programs / servers: a real console window on the PC
        res = shell.run(command, sh, here, visible=True)
        return {"ok": True, "output": res.get("output", ""), "code": 0, "cwd": here, "shell": sh}
    res = shell.run(_wrapped(command, sh), sh, here, timeout=TIMEOUT, limit=OUTPUT_LIMIT)
    out = res.get("output", "")
    if MARK in out:
        out, _, tail = out.rpartition(MARK)
        lines = [ln.strip() for ln in tail.strip().splitlines() if ln.strip()]
        if lines and os.path.isdir(lines[-1]):
            with _lock:
                _cwd[device] = lines[-1]
    out = out.rstrip()
    if out == tr("(تم بدون مخرجات)", "(done, no output)"):
        out = ""
    return {"ok": True, "output": out, "code": res.get("code", 0), "cwd": cwd(device), "shell": sh}


def listing(device: str, limit: int = 300) -> dict[str, Any]:
    """The browse view: folders first, then files, with sizes."""
    here = cwd(device)
    items = []
    try:
        with os.scandir(here) as it:
            for e in it:
                try:
                    d = e.is_dir()
                    size = 0 if d else e.stat().st_size
                except OSError:
                    d, size = False, 0
                items.append({"name": e.name, "kind": "folder" if d else files.file_type(e.name), "size": size})
    except OSError as exc:
        return {"ok": False, "error": str(exc), "cwd": here, "items": []}
    items.sort(key=lambda i: (i["kind"] != "folder", i["name"].lower()))
    parent = os.path.dirname(here.rstrip("\\/")) or here
    return {"ok": True, "cwd": here, "parent": parent if parent != here else "", "items": items[:limit],
            "more": max(0, len(items) - limit)}
