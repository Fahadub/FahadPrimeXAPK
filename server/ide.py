"""Detect installed editors/IDEs, open files in them and talk to their AI chat panels."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import settings
import winapi
from i18n import t as tr

_LA = os.environ.get("LOCALAPPDATA", "")
_PF = os.environ.get("ProgramFiles", r"C:\Program Files")
_PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

# id -> label, window-title marker, candidate exe paths/globs, PATH names, argument style
CATALOG: dict[str, dict[str, Any]] = {
    "vscode": {"label": "Visual Studio Code", "title": "Visual Studio Code", "style": "vscode",
               "paths": [rf"{_LA}\Programs\Microsoft VS Code\Code.exe", rf"{_PF}\Microsoft VS Code\Code.exe"],
               "which": ["code"]},
    "vscode-insiders": {"label": "VS Code Insiders", "title": "Visual Studio Code - Insiders", "style": "vscode",
                        "paths": [rf"{_LA}\Programs\Microsoft VS Code Insiders\Code - Insiders.exe"],
                        "which": ["code-insiders"]},
    "cursor": {"label": "Cursor", "title": "Cursor", "style": "vscode",
               "paths": [rf"{_LA}\Programs\cursor\Cursor.exe"], "which": ["cursor"]},
    "windsurf": {"label": "Windsurf", "title": "Windsurf", "style": "vscode",
                 "paths": [rf"{_LA}\Programs\Windsurf\Windsurf.exe"], "which": ["windsurf"]},
    "trae": {"label": "Trae", "title": "Trae", "style": "vscode",
             "paths": [rf"{_LA}\Programs\Trae\Trae.exe"], "which": ["trae"]},
    "android-studio": {"label": "Android Studio", "title": "Android Studio", "style": "jetbrains",
                       "paths": [rf"{_PF}\Android\Android Studio\bin\studio64.exe"], "which": []},
    "pycharm": {"label": "PyCharm", "title": "PyCharm", "style": "jetbrains",
                "paths": [rf"{_PF}\JetBrains\PyCharm*\bin\pycharm64.exe", rf"{_LA}\Programs\PyCharm*\bin\pycharm64.exe"],
                "which": ["pycharm64", "pycharm"]},
    "intellij": {"label": "IntelliJ IDEA", "title": "IntelliJ IDEA", "style": "jetbrains",
                 "paths": [rf"{_PF}\JetBrains\IntelliJ IDEA*\bin\idea64.exe", rf"{_LA}\Programs\IntelliJ IDEA*\bin\idea64.exe"],
                 "which": ["idea64", "idea"]},
    "webstorm": {"label": "WebStorm", "title": "WebStorm", "style": "jetbrains",
                 "paths": [rf"{_PF}\JetBrains\WebStorm*\bin\webstorm64.exe"], "which": ["webstorm64"]},
    "visual-studio": {"label": "Visual Studio", "title": "Microsoft Visual Studio", "style": "devenv",
                      "paths": [rf"{_PF}\Microsoft Visual Studio\*\*\Common7\IDE\devenv.exe"], "which": ["devenv"]},
    "sublime": {"label": "Sublime Text", "title": "Sublime Text", "style": "sublime",
                "paths": [rf"{_PF}\Sublime Text\sublime_text.exe", rf"{_PF}\Sublime Text 3\sublime_text.exe"],
                "which": ["subl"]},
    "notepad++": {"label": "Notepad++", "title": "Notepad++", "style": "npp",
                  "paths": [rf"{_PF}\Notepad++\notepad++.exe", rf"{_PF86}\Notepad++\notepad++.exe"],
                  "which": ["notepad++"]},
    "notepad": {"label": "Notepad", "title": "Notepad", "style": "plain", "paths": [], "which": ["notepad"]},
}

_cache: dict[str, Any] = {"at": 0.0, "found": {}}
_cache_lock = threading.Lock()


def _exe_from_shim(shim: str) -> str:
    """`code.cmd` style launchers live in a bin folder; prefer the real .exe next to them."""
    p = Path(shim)
    if p.suffix.lower() not in (".cmd", ".bat"):
        return shim
    for parent in list(p.parents)[:4]:
        for exe in parent.glob("*.exe"):
            if exe.stem.lower().replace(" ", "") in ("code", "code-insiders", "cursor", "windsurf", "trae"):
                return str(exe)
    return shim


def detect(refresh: bool = False) -> dict[str, dict[str, str]]:
    with _cache_lock:
        if not refresh and _cache["found"] and time.time() - _cache["at"] < 300:
            return dict(_cache["found"])
        found: dict[str, dict[str, str]] = {}
        for ide_id, spec in CATALOG.items():
            exe = ""
            for pattern in spec["paths"]:
                hits = sorted(glob.glob(pattern), reverse=True)
                if hits:
                    exe = hits[0]
                    break
            if not exe:
                for name in spec["which"]:
                    w = shutil.which(name)
                    if w:
                        exe = _exe_from_shim(w)
                        break
            if exe:
                found[ide_id] = {"id": ide_id, "label": spec["label"], "exe": exe}
        _cache.update(at=time.time(), found=found)
        return dict(found)


def pick(ide: str | None) -> str | None:
    found = detect()
    wanted = (ide or "").strip().lower().replace(" ", "")
    aliases = {"code": "vscode", "vs": "vscode", "vscode": "vscode", "visualstudiocode": "vscode",
               "studio": "android-studio", "androidstudio": "android-studio", "idea": "intellij",
               "notepadplusplus": "notepad++", "npp": "notepad++"}
    wanted = aliases.get(wanted, wanted)
    if wanted and wanted in found:
        return wanted
    pref = settings.get().get("preferred_ide") or ""
    if pref in found:
        return pref
    for ide_id in ("vscode", "cursor", "windsurf", "trae", "vscode-insiders", "pycharm", "intellij",
                   "android-studio", "webstorm", "sublime", "notepad++", "notepad"):
        if ide_id in found:
            return ide_id
    return None


def _launch(args: list[str]) -> None:
    if not winapi.IS_WIN:
        winapi.SIM_LOG.append(("launch", args))
        return
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if args[0].lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c"] + args
        flags = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(args, creationflags=flags, close_fds=True)


def open_in(ide: str | None, path: str, line: int | None = None) -> str:
    ide_id = pick(ide)
    if not ide_id:
        raise RuntimeError(tr("لا يوجد محرر مثبت", "No code editor is installed"))
    exe = detect()[ide_id]["exe"]
    style = CATALOG[ide_id]["style"]
    p = str(path)
    is_file = os.path.isfile(p)
    if style == "vscode":
        args = [exe, "-g", f"{p}:{line}"] if (line and is_file) else [exe, p]
    elif style == "jetbrains":
        args = [exe, "--line", str(line), p] if (line and is_file) else [exe, p]
    elif style == "sublime":
        args = [exe, f"{p}:{line}"] if line else [exe, p]
    elif style == "npp":
        args = [exe, f"-n{line}", p] if line else [exe, p]
    elif style == "devenv":
        args = [exe, "/edit", p]
    else:
        args = [exe, p]
    _launch(args)
    return CATALOG[ide_id]["label"]


def _focus_title(marker: str, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not winapi.IS_WIN:
            winapi.SIM_LOG.append(("focus_title", marker))
            return True
        fg = winapi.foreground_title()
        if marker.lower() in fg.lower():
            return True
        hwnd = winapi.find_window(marker)
        if hwnd and winapi.focus_window(hwnd):
            return True
        time.sleep(0.3)
    return False


def paste_text(text: str, submit: bool = True) -> None:
    """Paste through the clipboard (fast, keeps Arabic intact), then restore the old clipboard."""
    try:
        old = winapi.get_clipboard()
    except OSError:
        old = None
    winapi.set_clipboard(text)
    time.sleep(0.05)
    winapi.hotkey(["ctrl", "v"])
    time.sleep(0.25)
    if submit:
        winapi.tap("enter")
    if old is not None:
        def restore() -> None:
            time.sleep(1.5)
            try:
                winapi.set_clipboard(old)
            except OSError:
                pass
        threading.Thread(target=restore, daemon=True).start()


def chat(ide: str | None, message: str, path: str | None = None, submit: bool = True) -> str:
    ide_id = pick(ide)
    if not ide_id:
        raise RuntimeError(tr("لا يوجد محرر مثبت", "No code editor is installed"))
    hot = (settings.get().get("ide_chat_hotkeys") or {}).get(ide_id)
    if not hot:
        raise RuntimeError(tr(f"لا أعرف اختصار الشات في {CATALOG[ide_id]['label']} — أضفه في settings.json",
                              f"Unknown chat hotkey for {CATALOG[ide_id]['label']} — add it in settings.json"))
    if path:
        open_in(ide_id, path)
        time.sleep(1.5)
    if not _focus_title(CATALOG[ide_id]["title"]):
        raise RuntimeError(tr(f"لم أجد نافذة {CATALOG[ide_id]['label']} مفتوحة", f"No open {CATALOG[ide_id]['label']} window found"))
    time.sleep(0.2)
    winapi.hotkey(hot)
    time.sleep(0.9)
    paste_text(message, submit)
    return CATALOG[ide_id]["label"]


def send_to_window(window: str, text: str, submit: bool = True) -> str:
    if not _focus_title(window, timeout=3.0):
        raise RuntimeError(tr(f"لم أجد نافذة عنوانها يحتوي: {window}", f"No window whose title contains: {window}"))
    time.sleep(0.2)
    paste_text(text, submit)
    return window


def summary() -> list[dict[str, str]]:
    return [{"id": v["id"], "label": v["label"]} for v in detect().values()]
