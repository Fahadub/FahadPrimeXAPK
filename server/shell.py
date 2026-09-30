"""Running CMD / PowerShell commands requested from the phone."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import threading
from typing import Callable

import i18n
from i18n import t as tr

IS_WIN = sys.platform == "win32"
MAX_OUTPUT = 6000

# Commands that delete, format, kill or reconfigure things get a red warning on the phone.
DANGER_PATTERNS = [
    r"\b(remove-item|rm|rmdir|rd|del|erase)\b",
    r"\bformat(-volume)?\b",
    r"\b(stop-process|taskkill|kill)\b",
    r"\b(shutdown|restart-computer|stop-computer)\b",
    r"\breg(\.exe)?\s+(delete|add)\b",
    r"\b(set-itemproperty|remove-itemproperty|new-itemproperty)\b.*\bhk(lm|cu)\b",
    r"\b(diskpart|bcdedit|cipher\s+/w|vssadmin|wmic)\b",
    r"\b(clear-disk|initialize-disk|remove-partition)\b",
    r"\bset-executionpolicy\b",
    r"\b(netsh\s+advfirewall|disable-netadapter)\b",
    r"\b(invoke-expression|iex)\b.*\b(downloadstring|invoke-webrequest|iwr|irm)\b",
    r">\s*[a-z]:\\",
    r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+.*--force)",
]
_DANGER = [re.compile(p, re.I) for p in DANGER_PATTERNS]


def is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in _DANGER)


# CMD commands that only look at things: they run at once from the phone, no "yes" needed.
_READ_ONLY = re.compile(r"^(?:dir|where|type|tree|echo|ver|vol|hostname|whoami|ipconfig|getmac|systeminfo|tasklist|"
                        r"driverquery|netstat|ping|tracert|pathping|nslookup|arp\s+-a|route\s+print|findstr|find|more|"
                        r"sort|cd|chdir|date\s+/t|time\s+/t|chcp|query\s+user|qwinsta)\b", re.I)
_CHANGES_ANYWAY = re.compile(r"ipconfig\s+/(?:release|renew|flushdns|registerdns|setclassid)|\bping\b.*\s-t\b", re.I)
# installs, admin rights and system settings: always ask, whatever the "ask before running" switch says
_NEEDS_OK = re.compile(r"\b(?:runas|winget|choco|scoop|msiexec|setx|schtasks|icacls|takeown|attrib|netsh|reg|sc|"
                       r"bcdedit|powercfg|dism|sfc|net\s+(?:user|localgroup|stop|start|share)|pip\s+install|"
                       r"npm\s+(?:i|install)\s+-g)\b|-verb\s+runas", re.I)


def is_read_only(command: str, shell: str) -> bool:
    """A CMD command that only shows information (no redirection, no chaining, nothing that changes the PC)."""
    if str(shell).lower() != "cmd" or is_dangerous(command) or _CHANGES_ANYWAY.search(command):
        return False
    if re.search(r"[<>&^]|\|\|", command):
        return False
    parts = [c.strip() for c in command.split("|")]  # "tasklist | findstr chrome" is fine
    return all(p and _READ_ONLY.match(p) for p in parts)


def needs_ok(command: str, shell: str) -> bool:
    """PowerShell, installs, admin rights or system settings: the user approves first."""
    return str(shell).lower() != "cmd" or bool(_NEEDS_OK.search(command))


def _clip(s: str, limit: int = MAX_OUTPUT) -> str:
    s = s.replace("\r\n", "\n").strip()
    if len(s) > limit:
        return s[:limit // 2] + "\n…\n" + s[-limit // 2:]
    return s


def _ps_encoded(command: str) -> str:
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; " + command
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def run(command: str, shell: str = "powershell", cwd: str | None = None, timeout: int = 60,
        visible: bool = False, limit: int = MAX_OUTPUT) -> dict:
    """Run a command. ``visible`` opens a console window on the PC and returns immediately."""
    shell = "cmd" if str(shell).lower() in ("cmd", "cmd.exe", "command prompt") else "powershell"
    workdir = cwd if cwd and os.path.isdir(os.path.expandvars(cwd)) else os.path.expanduser("~")
    workdir = os.path.expandvars(workdir)

    if not IS_WIN:  # development/test fallback
        if visible:
            return {"ok": True, "output": "(opened in a new window)", "code": 0}
        p = subprocess.run(command, shell=True, cwd=workdir, capture_output=True, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
        return {"ok": p.returncode == 0, "output": _clip(out, limit), "code": p.returncode}

    if visible:
        if shell == "cmd":
            subprocess.Popen(f'cmd /s /k "{command}"', cwd=workdir, creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            subprocess.Popen(["powershell", "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-EncodedCommand", _ps_encoded(command)],
                             cwd=workdir, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return {"ok": True, "output": tr("فُتح في نافذة جديدة على الكمبيوتر", "Opened in a new window on the PC"), "code": 0}

    if shell == "cmd":
        # /s strips the outer quotes and runs the rest verbatim; chcp 65001 gives UTF-8 output.
        args: str | list[str] = f'cmd /d /s /c "chcp 65001>nul & {command}"'
    else:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", _ps_encoded(command)]
    try:
        p = subprocess.run(args, cwd=workdir, capture_output=True, timeout=timeout,
                           creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-8", "replace")
        return {"ok": False, "output": _clip(partial + tr(f"\n(انتهت المهلة بعد {timeout} ثانية)", f"\n(timed out after {timeout} s)"), limit), "code": -1}
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    if err.strip().startswith("#< CLIXML"):
        err = ""  # PowerShell progress noise
    text = out + (("\n" + err) if err.strip() else "")
    return {"ok": p.returncode == 0, "output": _clip(text, limit) or tr("(تم بدون مخرجات)", "(done, no output)"),
            "code": p.returncode}


def run_background(command: str, shell: str, cwd: str | None, on_done: Callable[[dict], None],
                   timeout: int = 3 * 3600) -> None:
    """Run hidden in a thread (for builds/installs) and report the result when it finishes."""
    def work() -> None:
        try:
            res = run(command, shell, cwd, timeout)
        except Exception as exc:  # never lose the notification
            res = {"ok": False, "output": str(exc), "code": -1}
        on_done(res)

    threading.Thread(target=i18n.bound(work), daemon=True).start()
