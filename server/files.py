"""Finding folders, files, installed programs and games by (Arabic or English) name."""

from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import settings
from i18n import t as tr
from textnorm import match_score, norm, translit

IS_WIN = sys.platform == "win32"
HOME = Path.home()

# Arabic / English spoken names -> known folder key
FOLDER_ALIASES: dict[str, str] = {
    "desktop": "Desktop", "سطح المكتب": "Desktop", "سطح مكتب": "Desktop", "الديسكتوب": "Desktop",
    "ديسكتوب": "Desktop",
    "documents": "Documents", "المستندات": "Documents", "مستنداتي": "Documents", "الملفات": "Documents",
    "downloads": "Downloads", "التنزيلات": "Downloads", "التحميلات": "Downloads", "الداونلود": "Downloads",
    "download": "Downloads", "التنزيل": "Downloads",
    "pictures": "Pictures", "الصور": "Pictures", "صوري": "Pictures",
    "music": "Music", "الموسيقى": "Music", "الاغاني": "Music", "الصوتيات": "Music",
    "videos": "Videos", "الفيديو": "Videos", "الفيديوهات": "Videos", "مقاطع الفيديو": "Videos",
    "home": "Home", "المجلد الشخصي": "Home", "مجلدي": "Home",
}

# Windows known-folder ids (the real locations, even when moved to OneDrive or another drive)
_FOLDER_IDS = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "PublicDesktop": "C4AA340D-F20F-4863-AFEF-F87EF2E6BA25",
}

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env", ".gradle", ".idea", "build", "dist",
    "appdata", "$recycle.bin", "system volume information", "windows", "program files", "program files (x86)",
    "programdata", ".cache", ".npm", ".nuget", "site-packages", "target", ".next",
}

MIN_SCORE = 45


def _known_folder_path(key: str) -> str | None:
    if not IS_WIN or key not in _FOLDER_IDS:
        return None
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        guid = GUID.from_buffer_copy(uuid.UUID(_FOLDER_IDS[key]).bytes_le)
        ptr = ctypes.c_wchar_p()
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        fn.restype = ctypes.c_long
        if fn(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) != 0:
            return None
        path = ptr.value
        ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(ptr, ctypes.c_void_p))
        return path
    except Exception:
        return None


def folder_dirs(key: str) -> list[Path]:
    """Every existing folder for a known folder (Windows location, local, OneDrive, public desktop)."""
    if key == "Home":
        return [HOME]
    cands: list[str | None] = [_known_folder_path(key), str(HOME / key)]
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        cands.append(str(Path(onedrive) / key))
    if key == "Desktop":
        cands.append(_known_folder_path("PublicDesktop"))
        cands.append(os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop") if IS_WIN else None)
    out: list[Path] = []
    seen: set[str] = set()
    for c in cands:
        if c and os.path.isdir(c) and os.path.normcase(os.path.abspath(c)) not in seen:
            seen.add(os.path.normcase(os.path.abspath(c)))
            out.append(Path(c))
    return out


def known_folders() -> dict[str, str]:
    out: dict[str, str] = {"Home": str(HOME)}
    for key in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
        dirs = folder_dirs(key)
        if dirs:
            out[key] = str(dirs[0])
    return out


def folder_key(name: str) -> str | None:
    n = norm(name)
    for alias, key in FOLDER_ALIASES.items():
        a = norm(alias)
        if n == a or n in (f"مجلد {a}", f"ملف {a}", f"{a} folder"):
            return key
    return None


def resolve_known(name: str) -> str | None:
    key = folder_key(name)
    return known_folders().get(key) if key else None


def search_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"):
        roots += folder_dirs(key)
    for extra in settings.get().get("search_roots") or []:
        p = Path(os.path.expandvars(str(extra)))
        if p.is_dir():
            roots.append(p)
    for name in ("source", "repos", "projects", "Projects", "code", "dev", "workspace", "OneDrive"):
        p = HOME / name
        if p.is_dir():
            roots.append(p)
    roots.append(HOME)
    seen: set[str] = set()
    uniq = []
    for r in roots:
        k = str(r).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def _ext_ok(name: str, exts: list[str] | None) -> bool:
    return not exts or name.lower().endswith(tuple(exts))


def find_paths(query: str, kind: str = "any", limit: int = 12, budget: float = 3.0,
               roots: list[str] | list[Path] | None = None, exts: list[str] | None = None,
               use_index: bool = True) -> list[dict[str, Any]]:
    """Name search under the user's folders (breadth first), then the Windows search index.

    ``query`` may be empty when ``exts`` is given (e.g. "the pdf on the desktop").
    """
    q = str(query or "").strip().strip('"«»')
    exts = [e if e.startswith(".") else "." + e for e in (exts or [])] or None
    if not q and not exts:
        return []
    if q:
        direct = Path(os.path.expandvars(q))
        if direct.is_absolute() and direct.exists():
            return [_info(direct)]
        if not exts and not roots:
            known = resolve_known(q)
            if known and kind in ("any", "folder"):
                return [_info(Path(known))]
    found: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + budget
    root_paths = [Path(r) for r in roots] if roots else search_roots()
    queue: deque[tuple[Path, int]] = deque((r, 0) for r in root_paths)
    visited: set[str] = set()
    while queue and time.monotonic() < deadline:
        d, depth = queue.popleft()
        key = str(d).lower()
        if key in visited:
            continue
        visited.add(key)
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if e.name.startswith(".") and is_dir:
                continue
            wanted = kind == "any" or (kind == "folder") == is_dir
            if exts and is_dir:
                wanted = False
            if wanted and _ext_ok(e.name, exts):
                s = match_score(q, e.name) if q else 80
                if s >= MIN_SCORE:
                    s -= depth * 3
                    prev = found.get(e.path.lower())
                    if not prev or prev["score"] < s:
                        found[e.path.lower()] = {**_info(Path(e.path), is_dir), "score": s}
            if is_dir and depth < 6 and e.name.lower() not in SKIP_DIRS:
                queue.append((Path(e.path), depth + 1))
    if not found and use_index and q:
        for r in windows_search(q, exts, kind):
            found[r["path"].lower()] = r
    ranked = sorted(found.values(), key=lambda r: (-r["score"], -r.get("mtime", 0)))
    return ranked[:limit]


def _info(p: Path, is_dir: bool | None = None) -> dict[str, Any]:
    try:
        st = p.stat()
        mtime = st.st_mtime
        size = st.st_size
    except OSError:
        mtime, size = 0, 0
    d = p.is_dir() if is_dir is None else is_dir
    return {"path": str(p), "name": p.name or str(p), "kind": "folder" if d else "file", "mtime": mtime,
            "size": 0 if d else size}


def windows_search(query: str, exts: list[str] | None = None, kind: str = "any", limit: int = 25
                   ) -> list[dict[str, Any]]:
    """Whole-PC lookup through the Windows Search index (fast, covers every indexed drive)."""
    if not IS_WIN:
        return []
    import shell

    words = [w for w in re.sub(r"[%_\[\]'\"`$]", " ", query).split() if w]
    if not words and not exts:
        return []
    conds = []
    for w in words:
        alt = translit(w)
        like = f"System.FileName LIKE '%{w}%'"
        if alt != norm(w) and re.search(r"[a-z]", alt):
            like = f"({like} OR System.FileName LIKE '%{alt}%')"
        conds.append(like)
    if exts:
        conds.append("(" + " OR ".join(f"System.FileExtension = '{e}'" for e in exts) + ")")
    if kind == "folder":
        conds.append("System.ItemType = 'Directory'")
    elif kind == "file":
        conds.append("System.ItemType <> 'Directory'")
    sql = (f"SELECT TOP {limit} System.ItemPathDisplay FROM SYSTEMINDEX WHERE {' AND '.join(conds)} "
           "ORDER BY System.DateModified DESC")
    script = ("$c = New-Object -ComObject ADODB.Connection; "
              "$c.Open(\"Provider=Search.CollatorDSO;Extended Properties='Application=Windows';\"); "
              f"$rs = $c.Execute(\"{sql}\"); "
              "while (-not $rs.EOF) { $rs.Fields.Item('System.ItemPathDisplay').Value; $rs.MoveNext() }; $c.Close()")
    try:
        res = shell.run(script, "powershell", timeout=8)
    except Exception:
        return []
    out = []
    for line in res.get("output", "").splitlines():
        p = line.strip()
        if p and os.path.exists(p):
            info = _info(Path(p))
            score = max(match_score(query, info["name"]), MIN_SCORE) if query else 70
            out.append({**info, "score": score - 5})  # a little below direct folder hits
    return out


TYPE_EXTS: dict[str, tuple[str, ...]] = {
    "image": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff", ".svg", ".ico"),
    "video": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".m4v", ".3gp"),
    "audio": (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".wma", ".opus"),
    "document": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".txt", ".md", ".rtf", ".odt"),
    "archive": (".zip", ".rar", ".7z", ".tar", ".gz"),
    "program": (".exe", ".msi", ".lnk", ".url", ".bat", ".cmd", ".appref-ms"),
    "code": (".py", ".js", ".ts", ".java", ".kt", ".cs", ".cpp", ".c", ".h", ".html", ".css", ".json", ".xml",
             ".yml", ".yaml", ".sql", ".go", ".rs", ".php", ".ps1"),
}


def file_type(name: str) -> str:
    """image / video / audio / document / archive / program / code / file, from the extension."""
    low = name.lower()
    for kind, exts in TYPE_EXTS.items():
        if low.endswith(exts):
            return kind
    return "file"


def fixed_drives() -> list[Path]:
    """The PC's own disks (C:, D: …), not network, CD or USB drives."""
    if not IS_WIN:
        return []
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # type: ignore[attr-defined]  # DRIVE_FIXED
                out.append(Path(root))
        except Exception:
            continue
    return out


def cmd_search_everywhere(names: list[str], kind: str = "any", exts: list[str] | None = None, limit: int = 40,
                          timeout: float = 10.0) -> list[dict[str, Any]]:
    """Last local try: ``dir /s /b`` on every disk at once (each disk in its own window, same time limit)."""
    drives = fixed_drives()
    results: list[list[dict[str, Any]]] = [[] for _ in drives]

    def one(i: int, root: Path) -> None:
        try:
            results[i] = cmd_search(names, [root], kind, exts, limit, timeout)
        except Exception:
            results[i] = []

    threads = [threading.Thread(target=one, args=(i, d), daemon=True) for i, d in enumerate(drives)]
    for th in threads:
        th.start()
    end = time.monotonic() + timeout + 2
    for th in threads:
        th.join(max(0.0, end - time.monotonic()))
    merged: dict[str, dict[str, Any]] = {}
    for part in results:
        for r in part:
            merged.setdefault(r["path"].lower(), r)
    return sorted(merged.values(), key=lambda r: (-r["score"], -r.get("mtime", 0)))[:limit]


def cmd_search(names: list[str], roots: list[Path], kind: str = "any", exts: list[str] | None = None,
               limit: int = 40, timeout: float = 8.0) -> list[dict[str, Any]]:
    """``dir /s /b`` in the given folders for each spelling of the name (the whole tree, no depth limit).
    Used when the quick search found nothing where the user said the thing is."""
    pats = []
    for n in names:
        n = re.sub(r'[*?"<>|/\\:]', " ", str(n)).strip()
        if n:
            pats.append("*" + "*".join(n.split()) + "*")
    if not pats or not roots:
        return []
    found: dict[str, dict[str, Any]] = {}
    end = time.monotonic() + timeout
    for root in roots:
        for pat in pats:
            left = end - time.monotonic()
            if left <= 0.5:
                break
            if IS_WIN:
                import shell

                flag = "/a-d" if kind == "file" or exts else "/ad" if kind == "folder" else "/a"
                res = shell.run(f'dir /s /b {flag} "{os.path.join(str(root), pat)}"', "cmd", timeout=max(1, int(left)))
                lines = res.get("output", "").splitlines()
            else:  # same search elsewhere (development / tests)
                import fnmatch

                lines = []
                for dirpath, dirnames, fnames in os.walk(root):
                    for nm in dirnames + fnames:
                        if fnmatch.fnmatch(nm.lower(), pat.lower()):
                            lines.append(os.path.join(dirpath, nm))
                    if time.monotonic() > end:
                        break
            for line in lines:
                path = line.strip()
                if not path or not os.path.exists(path) or not _ext_ok(path, exts):
                    continue
                info = _info(Path(path))
                if kind in ("file", "folder") and info["kind"] != kind:
                    continue
                score = max(match_score(names[0], info["name"]), *(match_score(n, info["name"]) for n in names))
                found.setdefault(path.lower(), {**info, "score": max(score, MIN_SCORE)})
                if len(found) >= limit:
                    break
    return sorted(found.values(), key=lambda r: (-r["score"], -r.get("mtime", 0)))[:limit]


def list_dir(path: str, limit: int = 60) -> list[dict[str, Any]]:
    p = Path(os.path.expandvars(path))
    items = []
    with os.scandir(p) as it:
        for e in it:
            try:
                d = e.is_dir()
            except OSError:
                d = False
            items.append({"name": e.name, "kind": "folder" if d else "file"})
    items.sort(key=lambda i: (i["kind"] != "folder", i["name"].lower()))
    return items[:limit]


TEXT_LIMIT = 200_000


def read_text(path: str, limit: int = TEXT_LIMIT) -> str:
    p = Path(os.path.expandvars(path))
    raw = p.read_bytes()
    if len(raw) > limit:
        raise ValueError(tr(f"الملف كبير جداً ({len(raw) // 1024} KB)", f"The file is too big ({len(raw) // 1024} KB)"))
    if b"\x00" in raw[:4096]:
        raise ValueError(tr("الملف ليس نصياً", "Not a text file"))
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Programs and games
# ---------------------------------------------------------------------------

APP_ALIASES: dict[str, str] = {
    "كروم": "chrome", "جوجل كروم": "chrome", "قوقل كروم": "chrome", "المتصفح": "msedge", "ايدج": "msedge",
    "edge": "msedge", "فايرفوكس": "firefox", "الحاسبه": "calc", "الاله الحاسبه": "calc", "حاسبه": "calc",
    "calculator": "calc", "المفكره": "notepad", "نوت باد": "notepad", "الرسام": "mspaint", "paint": "mspaint",
    "الاعدادات": "ms-settings:", "اعدادات الكمبيوتر": "ms-settings:", "settings": "ms-settings:",
    "مدير المهام": "taskmgr", "task manager": "taskmgr",
    "موجه الاوامر": "cmd", "سي ام دي": "cmd", "باورشل": "powershell", "باور شل": "powershell",
    "المستكشف": "explorer", "مستكشف الملفات": "explorer", "file explorer": "explorer",
    "وورد": "winword", "word": "winword", "اكسل": "excel", "بوربوينت": "powerpnt", "powerpoint": "powerpnt",
    "فيجوال ستوديو كود": "code", "في اس كود": "code", "vs code": "code", "vscode": "code",
    "سبوتيفاي": "spotify:", "spotify": "spotify:", "ستيم": "steam://open/main", "steam": "steam://open/main",
    "ديسكورد": "discord", "واتساب": "whatsapp:", "whatsapp": "whatsapp:", "تيمز": "msteams:",
    "لوحه التحكم": "control", "control panel": "control", "snipping tool": "snippingtool", "اداه القص": "snippingtool",
    "المتجر": "ms-windows-store:", "متجر مايكروسوفت": "ms-windows-store:", "اكس بوكس": "xbox:",
}

URI_PREFIXES = ("shell:", "steam://", "com.epicgames.launcher://", "ms-", "xbox:", "spotify:", "whatsapp:",
                "msteams:", "http://", "https://")

_apps_cache: dict[str, Any] = {"at": 0.0, "items": []}
_apps_lock = threading.Lock()
_apps_ready = threading.Event()  # set once the first list is loaded


def _start_menu_dirs() -> list[Path]:
    dirs = []
    for base in (os.environ.get("ProgramData"), os.environ.get("APPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if p.is_dir():
                dirs.append(p)
    return dirs


def _steam_games() -> list[dict[str, str]]:
    games: list[dict[str, str]] = []
    if not IS_WIN:
        return games
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except Exception:
        return games
    libs = {steam}
    try:
        vdf = (steam / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libs |= {Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)}
    except OSError:
        pass
    for lib in libs:
        for acf in glob.glob(str(lib / "steamapps" / "appmanifest_*.acf")):
            try:
                txt = Path(acf).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid = re.search(r'"appid"\s+"(\d+)"', txt)
            name = re.search(r'"name"\s+"([^"]+)"', txt)
            if appid and name and "redistributable" not in name.group(1).lower():
                games.append({"name": name.group(1), "target": f"steam://rungameid/{appid.group(1)}",
                              "kind": "game", "source": "Steam"})
    return games


def _epic_games() -> list[dict[str, str]]:
    games: list[dict[str, str]] = []
    base = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    for item in glob.glob(str(base / "*.item")):
        try:
            data = json.loads(Path(item).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        name, app = data.get("DisplayName"), data.get("AppName")
        if name and app:
            games.append({"name": name, "target": f"com.epicgames.launcher://apps/{app}?action=launch&silent=true",
                          "kind": "game", "source": "Epic"})
    return games


def _start_apps() -> list[dict[str, str]]:
    """Every Start-menu entry, including Store apps and Xbox games (Get-StartApps)."""
    if not IS_WIN:
        return []
    import shell

    try:
        res = shell.run("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress", "powershell",
                        timeout=15)
        data = json.loads(res.get("output") or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return [{"name": d["Name"], "target": "shell:AppsFolder\\" + d["AppID"], "kind": "app", "source": "Start"}
            for d in data if isinstance(d, dict) and d.get("Name") and d.get("AppID")]


def installed_apps(refresh: bool = False) -> list[dict[str, str]]:
    """Cached list; an old list is returned at once and refreshed in the background (Get-StartApps is slow).
    While the very first list is still loading (right after start), wait for it a few seconds at most."""
    with _apps_lock:
        items, age = _apps_cache["items"], time.time() - _apps_cache["at"]
        loading = bool(_apps_cache.get("refreshing"))
        if not refresh and items:
            if age >= 600 and not loading:
                _apps_cache["refreshing"] = True
                threading.Thread(target=warm_cache, daemon=True).start()
            return list(items)
        if not refresh and loading:
            wait = True
        else:
            wait = False
            _apps_cache["refreshing"] = True
    if wait:
        _apps_ready.wait(8)
        with _apps_lock:
            return list(_apps_cache["items"])
    try:
        return _load_apps()
    finally:
        with _apps_lock:
            _apps_cache["refreshing"] = False
        _apps_ready.set()


def _load_apps() -> list[dict[str, str]]:
    items = _start_apps() + _steam_games() + _epic_games()
    for d in _start_menu_dirs():
        for root, _dirs, fnames in os.walk(d):
            for f in fnames:
                if f.lower().endswith((".lnk", ".url")):
                    items.append({"name": Path(f).stem, "target": os.path.join(root, f), "kind": "app",
                                  "source": "shortcut"})
    for d in folder_dirs("Desktop"):  # shortcuts sit on the desktop itself: never walk its project folders
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.lower().endswith((".lnk", ".url")):
                        items.append({"name": Path(e.name).stem, "target": e.path, "kind": "app", "source": "shortcut"})
        except OSError:
            continue
    with _apps_lock:
        _apps_cache.update(at=time.time(), items=items)
    return list(items)


def find_apps(name: str, limit: int = 8) -> list[dict[str, Any]]:
    """Programs and games matching ``name``: aliases, Start menu, Store apps, Steam, Epic, desktop shortcuts."""
    n = norm(name)
    if not n:
        return []
    out: dict[str, dict[str, Any]] = {}
    for alias, target in APP_ALIASES.items():
        if n == norm(alias):
            out[target] = {"name": name, "target": target, "kind": "app", "source": "alias", "score": 100}
    for app in installed_apps():
        s = match_score(name, app["name"])
        if s >= MIN_SCORE:
            key = norm(app["name"])
            if key not in out or out[key]["score"] < s:
                out[key] = {**app, "score": s + (3 if app["kind"] == "game" else 0)}
    return sorted(out.values(), key=lambda a: -a["score"])[:limit]


def find_app(name: str) -> str | None:
    """Best launch target (alias, URI, AppsFolder id or shortcut) for a program name, if any."""
    apps = find_apps(name, 1)
    return apps[0]["target"] if apps and apps[0]["score"] >= 58 else None


def warm_cache() -> None:
    try:
        installed_apps(refresh=True)
    except Exception:
        pass
