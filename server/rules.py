"""Understanding common Arabic/English requests without an AI provider.

Returns the same plan shape the AI planner produces:
{"reply": str, "actions": [...]} or {"reply": str, "lookup_find": {...}} or None when not understood.
"""

from __future__ import annotations

import re
from typing import Any

import resolver
from i18n import t as tr
from textnorm import norm

MEDIA = {
    "play_pause": ["شغل", "وقف", "ايقاف مؤقت", "استئناف", "كمل التشغيل", "play", "pause", "resume"],
    "next": ["التالي", "اللي بعده", "المقطع التالي", "next", "skip"],
    "previous": ["السابق", "اللي قبله", "المقطع السابق", "previous", "back"],
    "volume_up": ["ارفع الصوت", "علي الصوت", "زود الصوت", "volume up", "louder"],
    "volume_down": ["اخفض الصوت", "وطي الصوت", "نزل الصوت", "قلل الصوت", "volume down", "quieter"],
    "mute": ["كتم", "اكتم", "اكتم الصوت", "كتم الصوت", "mute", "unmute"],
}
SYSTEM_OPS = {
    "lock": ["قفل الجهاز", "اقفل الجهاز", "اقفل الكمبيوتر", "قفل الكمبيوتر", "lock", "lock pc"],
    "sleep": ["سكون", "نوم الجهاز", "sleep"],
}

SHELL_RE = re.compile(r"^(?P<shell>cmd|سي ام دي|powershell|ps|باورشل|باور شل)\s*[:：]\s*(?P<cmd>.+)$", re.I | re.S)
RUN_RE = re.compile(r"^(?:نفذ|شغل|run|execute)\s+(?:الامر|امر|the command|command)\s+(?P<cmd>.+)$", re.I | re.S)
_CMD_WORD = re.compile(r"(?:الأمر|الامر|أمر|امر|command)\s+", re.I)
TYPE_RE = re.compile(r"^(?:اكتب|type)\s+(?P<text>.+)$", re.I | re.S)


def quick(text: str) -> dict[str, Any] | None:
    """Deterministic commands handled even when an AI provider is configured (fast, free)."""
    raw = text.strip()
    m = SHELL_RE.match(raw)
    if m:
        shell = "cmd" if norm(m["shell"]) in ("cmd", "سي ام دي") else "powershell"
        cmd = m["cmd"].strip()
        name = "CMD" if shell == "cmd" else "PowerShell"
        return {"reply": tr(f"سأنفذ في {name}: {cmd}", f"I will run in {name}: {cmd}"),
                "actions": [{"type": "run", "shell": shell, "command": cmd}]}
    t = norm(raw).rstrip(".!؟?")
    for key, words in MEDIA.items():
        if t in (norm(w) for w in words):
            return {"reply": tr("تم", "Done"), "actions": [{"type": "media", "key": key}]}
    for op, words in SYSTEM_OPS.items():
        if t in (norm(w) for w in words):
            return {"reply": tr("سأقفل الجهاز", "I will lock the PC") if op == "lock"
                    else tr("سأضع الجهاز في وضع السكون", "I will put the PC to sleep"),
                    "actions": [{"type": "system", "op": op}]}
    return None


def plan(text: str, default_shell: str = "powershell") -> dict[str, Any] | None:
    q = quick(text)
    if q:
        return q
    raw = text.strip()
    m = RUN_RE.match(norm(raw))
    if m:
        w = _CMD_WORD.search(raw)
        command = raw[w.end():] if w else m["cmd"]  # keep the original spelling/case of the command
        return {"reply": tr(f"سأنفذ الأمر: {command}", f"I will run the command: {command}"),
                "actions": [{"type": "run", "shell": default_shell, "command": command.strip()}]}
    m = TYPE_RE.match(raw)
    if m:
        return {"reply": tr("سأكتب النص على الكمبيوتر", "I will type the text on the PC"), "actions": [{"type": "type", "text": m["text"].strip()}]}

    t = norm(raw).rstrip(".!؟?")
    if re.match(r"^(?:العب|play the game|play game)\b", t):
        return {"reply": tr("وضع اللعب يحتاج مزود ذكاء اصطناعي يدعم الصور. اختره من الإعدادات.",
                             "Game mode needs an AI provider that supports images. Choose one in Settings."), "actions": []}

    um = re.match(r"^(?:افتح|open|روح|اذهب|go to)\s+(?:الرابط\s+|الموقع\s+|link\s+)?((?:https?://|www\.)\S+)$",
                  raw, re.I)
    if um:
        url = um.group(1) if um.group(1).startswith("http") else "https://" + um.group(1)
        return {"reply": tr(f"سأفتح الرابط {url}", f"I will open the link {url}"), "actions": [{"type": "open_url", "url": url}]}
    req = resolver.parse(raw)
    return {"resolve": req} if req else None
