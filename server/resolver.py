"""Understand "open …" requests in everyday Arabic/English and find what the user means.

"افتح ملف بسطح مكتب الكمبيوتر اسمه TAB نوع الملف pdf"
    -> {"kind": "file", "location": "Desktop", "name": "tab", "exts": [".pdf"]}
    -> candidates: [Desktop\\TAB.pdf, …]

Works without any AI provider; the AI planner also receives these candidates as a head start.
"""

from __future__ import annotations

import re
from typing import Any

import files
from i18n import t as tr
from textnorm import norm, translit

TYPE_WORDS: dict[str, list[str]] = {
    "pdf": [".pdf"], "بي دي اف": [".pdf"], "بدف": [".pdf"],
    "وورد": [".docx", ".doc"], "word": [".docx", ".doc"], "docx": [".docx"], "doc": [".doc", ".docx"],
    "اكسل": [".xlsx", ".xls", ".csv"], "اكسيل": [".xlsx", ".xls", ".csv"], "excel": [".xlsx", ".xls", ".csv"],
    "xlsx": [".xlsx"], "xls": [".xls"], "csv": [".csv"],
    "بوربوينت": [".pptx", ".ppt"], "باوربوينت": [".pptx", ".ppt"], "بور بوينت": [".pptx", ".ppt"],
    "باور بوينت": [".pptx", ".ppt"], "powerpoint": [".pptx", ".ppt"], "pptx": [".pptx"], "ppt": [".ppt"],
    "صوره": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"],
    "image": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"], "jpg": [".jpg", ".jpeg"], "png": [".png"],
    "فيديو": [".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv"], "مقطع": [".mp4", ".mkv", ".avi", ".mov", ".webm"],
    "video": [".mp4", ".mkv", ".avi", ".mov", ".webm"], "mp4": [".mp4"], "فيلم": [".mp4", ".mkv", ".avi"],
    "صوت": [".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"], "اغنيه": [".mp3", ".wav", ".m4a", ".flac"],
    "mp3": [".mp3"], "audio": [".mp3", ".wav", ".m4a", ".flac"],
    "نص": [".txt", ".md"], "نصي": [".txt", ".md"], "txt": [".txt"], "text": [".txt", ".md"],
    "مضغوط": [".zip", ".rar", ".7z"], "zip": [".zip"], "rar": [".rar"],
    "بايثون": [".py"], "python": [".py"], "py": [".py"],
    "exe": [".exe", ".msi"], "تنفيذي": [".exe", ".msi"],
}
KIND_WORDS = {
    "ملف": "file", "file": "file",
    "مجلد": "folder", "فولدر": "folder", "folder": "folder", "directory": "folder",
    "برنامج": "app", "تطبيق": "app", "app": "app", "application": "app", "program": "app",
    "لعبه": "game", "قيم": "game", "game": "game",
}
_VERB = (r"(?:افتح(?:لي|ي)?|فتح|شغل(?:لي|ي)?|تشغيل|روح|رح|اذهب|انتقل|ودني|خذني|وريني|اعرض|"
         r"ابحث\s+عن|دور(?:\s+على|\s+لي|لي)?|وين|اين|open|launch|start|run|play|show(?:\s+me)?|go\s+to|goto|"
         r"find|search(?:\s+for)?|where\s+is)")
_FILLER = (r"(?:لو\s+سمحت|من\s+فضلك|رجاء|please|pls|لي|لنا|الي|اللي|الذي|التي|هذا|هذي|هذه|ذا|يا|طيب|ممكن|تكفى|"
           r"ابي|ابغى|ابغا|ابا|اريد|بغيت|عطني|i\s+want\s+to|i\s+want|i\s+need|can\s+you|could\s+you|the|a|my)")
_DEVICE = r"(?:ال)?(?:كمبيوتر|جهاز|حاسب|حاسوب|لابتوب|بي\s+سي|computer|pc|laptop)"
_NAME_MARK = r"(?:اسمه|اسمها|باسم|الاسم|بعنوان|عنوانه|named|called|with\s+name|name)"
_TYPE_MARK = r"(?:نوع(?:ه|ها)?(?:\s+(?:ال)?ملف)?|امتداد(?:ه|ها)?(?:\s+(?:ال)?ملف)?|صيغت(?:ه|ها)|صيغه|بصيغه|type|extension|format)"


def _w(pattern: str) -> str:
    """Whole-word pattern that also works for Arabic (no \\b for Arabic letters)."""
    return rf"(?<![^\s]){pattern}(?![^\s])"


def _type_exts(word: str) -> list[str] | None:
    w = norm(word).lstrip(".")
    return TYPE_WORDS.get(w) or (TYPE_WORDS.get(w[2:]) if w.startswith("ال") else None)


def parse(text: str) -> dict[str, Any] | None:
    """Split an open/find request into kind, location, name and file types. None if it is not one."""
    t = norm(text)
    t = re.sub(r"[؟?!،,«»\"“”]", " ", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(".")
    m = re.match(rf"^(?:{_FILLER}\s+)*{_VERB}(?:\s+|$)", t)
    if not m:
        return None
    play = bool(re.search(r"(?<![^\s])(?:شغل(?:لي|ي)?|تشغيل|play|launch|start|run)(?![^\s])", t[:m.end()]))
    rest = t[m.end():]

    # file type: "نوع الملف pdf", "بصيغه pdf", "extension pdf"
    exts: list[str] = []
    tm = re.search(rf"{_w(_TYPE_MARK)}\s+(?:ال)?(\S+(?:\s+\S+)?)", rest)
    if tm:
        two = tm.group(1)
        for cand in (two, two.split()[0]):
            e = _type_exts(cand)
            if e:
                exts = e
                rest = rest[:tm.start()] + " " + rest[tm.start(1) + len(cand):]
                break

    # location: "بسطح مكتب الكمبيوتر", "في التنزيلات", "on the desktop"
    location = None
    aliases = sorted((norm(a) for a in files.FOLDER_ALIASES), key=len, reverse=True)
    full_re = "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in aliases)
    # "للتنزيلات" / "بالمستندات": the article merged with a preposition
    bare_re = "|".join(re.escape(a[2:]).replace(r"\ ", r"\s+") for a in aliases if a.startswith("ال"))
    lm = re.search(rf"(?:(?:في|على|علي|من|داخل|الى|الي|in|on|at|from|to)\s+)?(?:(?:the|my|your)\s+)?(?<![^\s])"
                   rf"(?:(?:ب|ل|ف|ع)?(?:ال)?({full_re})|(?:لل|بال|فال|وال|عال)({bare_re}))"
                   rf"(?:\s+(?:حق|تبع|مال|في|بتاع|of|on)?\s*(?:(?:the|my)\s+)?{_DEVICE})?(?![^\s])", rest)
    if lm:
        found = lm.group(1) or ("ال" + lm.group(2))
        location = files.folder_key(found)
        rest = rest[:lm.start()] + " " + rest[lm.end():]
    rest = re.sub(rf"(?:(?:في|على|علي|من|in|on)\s+)(?:(?:the|my)\s+)?{_DEVICE}", " ", rest)

    # kind word ("ملف", "لعبه", "برنامج" …) — the first one only
    kind = "any"
    km = re.search(_w(r"(?:ال)?(" + "|".join(KIND_WORDS) + r")"), rest)
    if km:
        kind = KIND_WORDS[km.group(1)]
        rest = rest[:km.start()] + " " + rest[km.end():]

    # name: after "اسمه / named", otherwise whatever is left
    name = ""
    nm = re.search(rf"{_w(_NAME_MARK)}\s+(.+)$", rest)
    if nm:
        name = nm.group(1)
        for wd in rest[:nm.start()].split():  # "الصوره اللي اسمها رحله": the type is before the name
            if not exts and _type_exts(wd):
                exts = _type_exts(wd) or []
    else:
        name = re.sub(_w(_FILLER), " ", rest)

    # a bare type word left in the name ("افتح التقرير pdf")
    words = name.split()
    if words and not exts:
        for i, wd in enumerate(words):
            e = _type_exts(wd)
            if e and (len(words) > 1 or kind == "file"):
                exts = e
                words.pop(i)
                break
    name = re.sub(r"\s+", " ", " ".join(words)).strip(" .-")
    name = re.sub(r"^(?:في|على|علي|من|in|on|at|from)\s+|\s+(?:في|على|علي|من|in|on|at|from)$", "", name).strip()
    # "tab.pdf" typed with the extension
    em = re.match(r"^(.*\S)\.([a-z0-9]{2,4})$", name)
    if em and not exts and _type_exts(em.group(2)):
        name, exts = em.group(1), _type_exts(em.group(2)) or []
    if exts and kind == "any":
        kind = "file"
    return {"kind": kind, "location": location, "name": name, "exts": exts, "play": play}


_WHERE = {"Desktop": ("سطح المكتب", "the Desktop"), "Documents": ("المستندات", "Documents"),
          "Downloads": ("التنزيلات", "Downloads"), "Pictures": ("الصور", "Pictures"), "Music": ("الموسيقى", "Music"),
          "Videos": ("الفيديو", "Videos"), "Home": ("مجلدك", "your folder")}


def name_variants(name: str) -> list[str]:
    """The name as said and, for Arabic, its Latin spelling («تاب» -> tab), for searching both ways."""
    out = [name.strip()] if name.strip() else []
    if re.search(r"[\u0600-\u06FF]", name):
        lat = translit(name).strip()
        if lat and re.search(r"[a-z]", lat) and lat not in out:
            out.append(lat)
    return out


def search_label(req: dict[str, Any]) -> str:
    """Short text of what is searched: «تاب» / tab · pdf · سطح المكتب."""
    parts = []
    names = name_variants(req.get("name") or "")
    if names:
        parts.append(" / ".join(f"«{n}»" if i == 0 else n for i, n in enumerate(names)))
    if req.get("exts"):
        parts.append(", ".join(e.lstrip(".") for e in req["exts"][:3]))
    if req.get("location"):
        parts.append(tr(*_WHERE.get(req["location"], (req["location"], req["location"]))))
    elif req.get("kind") in ("app", "game"):
        parts.append(tr("البرامج والألعاب", "programs and games"))
    return " · ".join(parts)


def resolve(req: dict[str, Any], limit: int = 8, on_step: Any = None) -> list[dict[str, Any]]:
    """Ranked candidates: {"kind": file|folder|app|game, "label", "detail", "target", "score"}.
    ``on_step(text)`` is told when a slower step starts (the CMD search), for the phone's status line."""
    kind, name, exts, location = req["kind"], req["name"], req["exts"] or None, req["location"]
    out: list[dict[str, Any]] = []

    if name and kind in ("any", "app", "game") and not exts:
        for a in files.find_apps(name, limit):
            boost = 8 if kind in ("app", "game") else 4 if req.get("play") else 0
            out.append({"kind": a["kind"], "label": a["name"], "detail": _app_detail(a), "type": a["kind"] if
                        a["kind"] == "game" else "program", "target": a["target"], "score": a["score"] + boost})

    if kind in ("any", "file", "folder"):
        fkind = kind if kind in ("file", "folder") else "any"
        roots = files.folder_dirs(location) if location else None
        found = files.find_paths(name, fkind, limit, roots=roots, exts=exts, use_index=not roots) \
            if (name or exts) else []
        if not found and roots and name:  # the whole tree where they said, with CMD, in both spellings
            names = name_variants(name)
            if on_step:
                on_step("CMD: dir /s /b " + " ".join(f'"*{n}*"' for n in names))
            found = files.cmd_search(names, roots, fkind, exts, limit)
        if not found and roots and name:  # not where they said: look everywhere
            found = files.find_paths(name, fkind, limit, exts=exts)
        for f in found:
            ftype = "folder" if f["kind"] == "folder" else files.file_type(f["name"])
            boost = 6 if (exts or kind in ("file", "folder") or location) else 0
            if req.get("play") and ftype in ("video", "audio", "program"):
                boost += 4
            out.append({"kind": f["kind"], "label": f["name"], "detail": f["path"], "target": f["path"],
                        "type": ftype, "score": f["score"] + boost})
        if not name and not exts and location:
            d = files.known_folders().get(location)
            if d:
                out.append({"kind": "folder", "label": location, "detail": d, "target": d, "type": "folder",
                            "score": 100})
        if not out and name:  # nothing anywhere yet: every disk with CMD (dir /s /b), a few seconds at most
            names = name_variants(name)
            if on_step:
                on_step("CMD: dir /s /b " + " ".join(f"{d}*{names[0]}*" for d in files.fixed_drives()[:3]))
            for f in files.cmd_search_everywhere(names, fkind, exts, limit):
                ftype = "folder" if f["kind"] == "folder" else files.file_type(f["name"])
                out.append({"kind": f["kind"], "label": f["name"], "detail": f["path"], "target": f["path"],
                            "type": ftype, "score": f["score"]})

    seen: set[str] = set()
    uniq = []
    for c in sorted(out, key=lambda c: -c["score"]):
        keys = {c["target"].lower()}
        if c["kind"] in ("app", "game"):  # the same program from the Start menu and a desktop shortcut
            keys.add("app|" + norm(c["label"]))
        if not keys & seen:
            seen |= keys
            uniq.append(c)
    return uniq[:limit]


def _app_detail(a: dict[str, Any]) -> str:
    src = a.get("source", "")
    if src in ("Steam", "Epic"):
        return tr(f"لعبة {src}", f"{src} game")
    return tr("اختصار", "Shortcut") if src == "shortcut" else tr("برنامج", "Program")


def is_clear(cands: list[dict[str, Any]]) -> bool:
    """One obvious answer (open it after a normal confirmation) vs. several to choose from."""
    if not cands:
        return False
    if len(cands) == 1:
        return cands[0]["score"] >= 55
    return cands[0]["score"] >= 85 and cands[0]["score"] - cands[1]["score"] >= 12


_KIND_LABEL = {"file": ("الملف", "file"), "folder": ("المجلد", "folder"), "app": ("البرنامج", "program"),
               "game": ("اللعبة", "game")}

_TYPE_LABEL = {"program": ("🧩", "برنامج", "Program"), "game": ("🎮", "لعبة", "Game"), "folder": ("📁", "مجلد", "Folder"),
               "image": ("🖼", "صورة", "Image"), "video": ("🎬", "فيديو", "Video"), "audio": ("🎵", "صوت", "Audio"),
               "document": ("📄", "مستند", "Document"), "archive": ("🗜", "ملف مضغوط", "Archive"),
               "code": ("💻", "كود", "Code"), "file": ("📄", "ملف", "File")}


def type_icon(c: dict[str, Any]) -> str:
    return _TYPE_LABEL.get(c.get("type") or c.get("kind", "file"), _TYPE_LABEL["file"])[0]


def type_label(c: dict[str, Any]) -> str:
    """«🎬 فيديو · MP4», «🧩 برنامج», «📁 مجلد» — so two things with the same name are easy to tell apart."""
    t = c.get("type") or ("program" if c.get("kind") == "app" else c.get("kind", "file"))
    icon, ar, en = _TYPE_LABEL.get(t, _TYPE_LABEL["file"])
    ext = ""
    if c.get("kind") == "file":
        m = re.search(r"\.([A-Za-z0-9]{1,5})$", c.get("label", ""))
        ext = f" · {m.group(1).upper()}" if m else ""
    return f"{icon} {tr(ar, en)}{ext}"


def kind_label(kind: str) -> str:
    return tr(*_KIND_LABEL.get(kind, ("", "")))
