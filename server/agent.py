"""The smart assistant: understands a spoken/typed request, proposes a plan, runs it after confirmation.

Flow
  phone ──/agent {text}──▶ plan  (kind = "plan" | "choose" | "reply" | "done")
  phone ──/agent/confirm {plan_id, approve, choice}──▶ results

Read-only lookups (file search, folder listing, reading a file, window titles) run automatically
while planning; anything that changes the PC waits for the user's "yes".
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import ai
import files
import i18n
import ide
import memory
import resolver
import rules
import settings
import shell
import winapi
from events import EVENTS
from control import CONTROL
from game import GAME
from i18n import t as tr
from tasks import TASKS
from textnorm import NO_WORDS, YES_WORDS, norm
from watch import WATCH

PLAN_TTL = 15 * 60
INSTANT_TYPES = {"media", "game_stop", "watch"}  # run without asking in "smart" confirm mode (+ read-only CMD)
MODIFYING_TYPES = {"run", "edit_file", "write_file", "system", "control"}
EDIT_MAX_CHARS = 80_000
MAX_TEXT = 100_000      # typed / dictated requests: practically no limit
MAX_ROUNDS = 8          # follow-up rounds of one task ("until it finishes")
TASK_SECONDS = 10 * 60
REPEAT_LIMIT = 5        # follow-up rounds: the same failing steps this many times in a row -> stop
REQUEST_SECONDS = 150   # rounds that run by themselves within one phone request (older apps wait 3 minutes)
PLAN_SECONDS = 140      # understanding one request, AI rounds included (the phone waits up to 3 minutes)
AI_CALL_SECONDS = 45    # one AI call for an easy request (longer requests get twice as long)
LOOKUP_ROUNDS = 2       # "look it up first" rounds before the AI must answer
LOOKUP_SECONDS = 12     # all lookups of one round (they run side by side)
FAST_MIN_SCORE = 58     # a local search hit this good is answered without the AI
BARE_MIN_SCORE = 75     # … when the request is only a name
FAST_NAME_WORDS = 6
SEARCH_SECONDS = 25     # the local search (quick scan, CMD dir /s, Windows index) never holds a request longer
TURN_SECONDS = 150      # a step that needs the screen waits this long at most for its turn (one control round)
# steps that use the screen, mouse or keyboard: they take turns with screen-control tasks (tasks.py);
# everything else (CMD in the background, editing files, searching) runs side by side with them
SCREEN_TYPES = {"open", "open_app", "open_url", "open_in_ide", "ide_chat", "send_to_window", "type", "keys", "system",
                "game_start"}

# several steps in one sentence: «افتح كروم وابحث عن …», "open X then …" -> the AI plans it
_MULTI_STEP = re.compile(r"(?<![^\s])(?:ثم|بعدين|بعدها|وبعدين|وبعدها|وبعد|then|and|after|afterwards|finally)(?![^\s])"
                         r"|[،,;\n]", re.I)
_AND_VERB = re.compile(r"(?<![^\s])و(?:افتح|فتح|شغل|اكتب|ابحث|ادخل|دخل|سو|سوي|خل|خلي|احذف|امسح|عدل|انسخ|الصق|اقفل|"
                       r"سكر|ارسل|حط|نزل|ثبت|روح|شوف|قل|نبه|بلغ|اعطني|العب|سجل|كبر|صغر)")
# first words that make a short request a task, not a name to open
_NOT_A_NAME = {"العب", "اكتب", "سو", "سوي", "اعمل", "عدل", "احذف", "امسح", "ارسل", "ابن", "نظف", "جهز", "ركب", "ثبت",
               "نزل", "كيف", "ليش", "لماذا", "لما", "متى", "وش", "ايش", "هل", "مين", "من", "كم", "اشرح", "ترجم", "قل",
               "type", "write", "make", "edit", "delete", "remove", "send", "build", "install", "explain", "translate",
               "what", "how", "why", "when", "who", "is", "are", "can", "do", "does", "tell", "say", "run", "execute",
               "نفذ", "cmd", "powershell"}
_SMALL_TALK = {"مرحبا", "هلا", "اهلا", "السلام عليكم", "سلام", "شكرا", "مشكور", "تسلم", "تمام", "كيف حالك", "كيفك",
               "مين انت", "من انت", "صباح الخير", "مساء الخير", "hi", "hello", "hey", "thanks", "thank you", "ok",
               "okay", "good", "nice", "who are you", "how are you"}

SYSTEM_PROMPT = """You are the assistant inside "Wi-Fi Remote", an app that lets the user control their Windows PC
from their phone by voice or text. You turn each request into concrete actions on the PC. The user always sees
your plan and confirms it before anything runs.

Answer with ONE JSON object and nothing else. Possible shapes:

1) Look something up first (read-only, runs automatically, then you are asked again with the results):
   {"lookup": [{"tool": "find", "query": "<name only>", "kind": "file|folder|app|game|any",
                "ext": "<optional file type: pdf, docx, xlsx, mp4 …>", "in": "<optional: Desktop|Documents|Downloads|Pictures|Music|Videos>"},
               {"tool": "list_dir", "path": "<folder>"},
               {"tool": "read_file", "path": "<file>"},
               {"tool": "windows"}]}
2) Just answer / ask a clarifying question (no actions): {"reply": "<text>"}
3) Several candidates - let the user pick one, then run actions where "$choice" is replaced by the picked value:
   {"reply": "<text>", "choose": {"question": "<text>", "options": [{"label": "<short>", "detail": "<full path>",
    "value": "<full path or value>"}]}, "actions": [ ...actions using "$choice"... ]}
4) A plan: {"reply": "<one or two sentences saying exactly what will happen>", "actions": [ ... ]}

Actions:
- {"type": "open", "path": "<full path, or a program/game target from search results>", "label": "<short name>"}
   opens a file with its default app, a folder in Explorer, or launches a program / game
- {"type": "open_app", "name": "<app name, exe or URI>"}       e.g. "chrome", "notepad", "ms-settings:"
- {"type": "open_url", "url": "https://..."}
- {"type": "open_in_ide", "path": "<file or folder>", "ide": "<editor id>", "line": <optional int>}
- {"type": "run", "shell": "powershell|cmd", "command": "<command>", "cwd": "<optional folder>",
   "visible": <true to open a console window on the PC for interactive commands / servers that never end>,
   "background": <true for long jobs that end (build, install, tests, download): runs hidden and alerts the phone
                  with the result when finished>,
   "danger": <true if it deletes/overwrites/kills/reconfigures>}
- {"type": "edit_file", "path": "<file>", "instruction": "<precise change to make>", "open_after": "<editor id or empty>"}
   (the server makes the edit with AI and shows the user a diff before saving; a backup is kept)
- {"type": "write_file", "path": "<new file>", "content": "<full content>"}
- {"type": "ide_chat", "ide": "<editor id>", "message": "<prompt for the editor's AI chat>", "path": "<optional file to open first>"}
- {"type": "send_to_window", "window": "<part of the window title>", "text": "<text>", "submit": true}
- {"type": "type", "text": "<text typed into the focused window>"}
- {"type": "keys", "keys": ["ctrl", "s"]}
- {"type": "media", "key": "play_pause|next|previous|volume_up|volume_down|mute", "times": <optional int>}
- {"type": "system", "op": "lock|sleep|shutdown|restart|cancel_shutdown"}
- {"type": "game_start", "goal": "<what to achieve>", "keys": ["<allowed keys>"], "seconds": <int, 0 = until the user stops it>}
- {"type": "game_stop"}
- {"type": "wait", "seconds": <1-10>}
- {"type": "watch", "label": "<what we are waiting for>", "minutes": <max minutes>}
   (watches the screen and alerts the phone with an alarm when the activity stops, e.g. an editor's AI agent,
   an installer or a render)
- {"type": "control", "goal": "<the whole task, complete, in the user's words>", "app": "<optional program to open first>"}
   works INSIDE programs like a person: a screenshot every round, clicks, typing, menus, drawing, until the goal
   is reached. It runs round after round by itself, asks the user when it needs a decision, and stops by itself
   when it makes no progress.

Asking the user in the middle of a task (the task goes on with the answer):
   {"reply": "<short>", "ask": {"question": "<text>", "options": ["<a>", "<b>", "<c>"], "text": true,
    "help": "<optional https link to a short guide>"}}
   "text": true lets the user type an answer that is not in the options.

Rules:
- Most requests are about THIS PC: its files, folders, programs, games and windows. A bare name («فالورانت»,
  «التقرير», "budget") means something on this PC to open: search it. Do not answer from general knowledge or send
  the user to the web unless they ask for a website.
- Be fast: act, do not deliberate. A simple request needs one answer: use [Local search] when it is there, at most
  one lookup round otherwise. No explanations.
- Questions about this PC (disk space, IP, running programs, Windows version, where a file is …): answer with a
  read-only CMD command (dir, where, type, tree, tasklist, ipconfig, systeminfo, netstat, ping, findstr …). It runs
  at once and its output is shown to the user, so no summary round is needed.
- Not sure which item the user means (several matches, or a vague name)? Answer with "choose" listing the real
  candidates. Never guess, and never ask in plain text when you can offer options.
- Write "reply", questions and labels in the "App language" from [PC context] (if the user clearly wrote in
  another language, use theirs). Keep them short; they are read aloud. State what will happen
  ("سأفتح مجلد التنزيلات" / "I will open the Downloads folder") - do not ask "should I?" because the app asks
  for confirmation itself.
- Understand everyday phrasing: pull the NAME, the FILE TYPE and the LOCATION out of the sentence.
  «افتح ملف بسطح مكتب الكمبيوتر اسمه TAB نوع الملف pdf» -> find query "TAB", ext "pdf", in "Desktop".
  Speech often spells English names in Arabic letters («تاب» = TAB, «فالورانت» = VALORANT); search handles that.
- You can open anything: files, folders, programs, Store apps, Steam/Epic games, shortcuts or any path.
- Never invent paths. "[Local search]" in the request already lists what matched on this PC: use those targets.
  Otherwise use a "find" lookup first. One clear match -> plan with it (mention it in "reply").
  Several plausible matches -> "choose". No match -> ONE round of find lookups with the name in the other
  language and spelling (Arabic and English: «الميزانية» -> "budget", «تاب» -> "tab", "report" -> «تقرير»), shorter,
  kind "any", in the place the user said and then without a place; still nothing -> say briefly it is not on the PC.
- Shell: CMD by default. PowerShell only when CMD cannot do it or the user asks for PowerShell / "باورشل".
  Every PowerShell command, and anything that installs, needs admin rights or changes settings, waits for the
  user's OK - that is expected, just plan it. Prefer safe, specific commands. Mark destructive ones with
  "danger": true.
- Editing: "edit/modify file X ..." -> edit_file with open_after = the preferred editor, unless the user asks to hand
  it to the editor's AI chat (Copilot, Cursor, Windsurf, Claude...), then ide_chat with that file in "path".
  "Send this to the chat/IDE" -> ide_chat (or send_to_window for other apps such as a browser chat).
- "Go to" / "open" a folder or file -> "open"; "open it in VS Code/the editor" -> open_in_ide.
- "Tell me / alert me when it finishes" (نبهني / بلغني لما يخلص): commands -> "background": true;
  GUI work (editor AI agent, installer, render) -> add a "watch" action after it.
- Work inside a program's window that needs seeing and clicking (pick an entry in an app's menu or model list,
  draw in Paint, work in Blender, change a setting in an app, fill a form): ONE "control" action with the complete
  goal (and "app" to open first). Do not plan the clicks yourself.
- A task that needs a program the user did not name (a code editor/IDE, 3D, drawing, video editing …): ask which one,
  offering 3 well-known suitable programs, installed ones first and marked "(installed)" (see Installed editors, or
  use a find lookup), with "text": true for another name. After the answer: open it (or use it in a control goal);
  if it is not installed, install it with winget (run, "shell": "cmd"; the user approves it) and go on.
- Games: when asked to play, use game_start with a clear goal and the keys the game needs (movement, jump, action).
- Chit-chat or questions about something else: answer briefly in "reply" with no actions.
- Tasks that need several rounds: when the next steps depend on the results of these actions (read a command's
  output, check that something worked, then continue), add "continue": true to the plan. You will then receive the
  results as [outcome] and must answer with the next plan (again with "continue": true if more is needed), or with
  {"reply": "<short summary of what was done>", "done": true} when the whole task is finished.
  The user may approve such a task once with "full access": still mark destructive commands with "danger": true.
- "Play until I stop" -> game_start with "seconds": 0.
"""

EDIT_SYSTEM = """You edit a text file according to the user's instruction. Keep everything that the instruction does
not ask to change exactly as it is (formatting, comments, line endings). Reply in exactly this format:
SUMMARY: <one short sentence in the requested language describing the change>
<<<FILE
<the complete new file content>
FILE>>>"""

MEDIA_KEYS = {"play_pause": "media_play_pause", "next": "media_next", "previous": "media_previous",
              "volume_up": "volume_up", "volume_down": "volume_down", "mute": "volume_mute",
              "stop": "media_stop"}
_MEDIA_LABELS = {"play_pause": ("تشغيل/إيقاف", "Play / pause"), "next": ("التالي", "Next"),
                 "previous": ("السابق", "Previous"), "volume_up": ("رفع الصوت", "Volume up"),
                 "volume_down": ("خفض الصوت", "Volume down"), "mute": ("كتم الصوت", "Mute"), "stop": ("إيقاف", "Stop")}
_SYSTEM_LABELS = {"lock": ("قفل الجهاز", "Lock the PC"), "sleep": ("وضع السكون", "Sleep"),
                  "shutdown": ("إيقاف تشغيل الكمبيوتر", "Shut down the PC"),
                  "restart": ("إعادة تشغيل الكمبيوتر", "Restart the PC"), "cancel_shutdown": ("إلغاء الإيقاف", "Cancel shutdown")}
MEDIA_KEYS_SET = set(_MEDIA_LABELS)
SYSTEM_OPS = set(_SYSTEM_LABELS)


def media_label(key: str) -> str:
    return tr(*_MEDIA_LABELS[key])


def system_label(op: str) -> str:
    return tr(*_SYSTEM_LABELS[op])


def _clip(s: Any, n: int = 400) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(p or "").strip().strip('"')))


def _startfile(target: str) -> None:
    if winapi.IS_WIN:
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        winapi.SIM_LOG.append(("startfile", target))


class Agent:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.pending: dict[str, dict[str, Any]] = {}
        self.searched: dict[str, dict[str, Any]] = {}  # the last local search per phone (reused by the AI step)
        self.last_path: dict[str, str] = {}
        self.progress: dict[str, dict[str, Any]] = {}  # what each phone's request is doing right now
        self._tl = threading.local()  # the organiser's entry for the request this thread is answering

    # ------------------------------------------------------------------ public

    @staticmethod
    def device_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    def handle(self, device: str, text: str, source: str = "text", ask: bool = True) -> dict[str, Any]:
        """``ask`` False = the phone's "ask before running" switch is off: clear plans run right away
        (choices are still asked when unsure, and dangerous steps still need a tap)."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": tr("الطلب فارغ", "The request is empty")}
        if len(text) > MAX_TEXT:
            return {"ok": False, "error": tr("الطلب طويل جداً", "The request is too long")}
        self._stage(device, "search", start=True)
        self._tl.tid = TASKS.add(device, "agent", text)
        try:
            return self._settle(self._handle(device, text, ask))
        finally:
            self.progress.pop(device, None)
            TASKS.end(self._tl.tid, "error")  # only if it was not settled (an unexpected error)
            self._tl.tid = None

    def _handle(self, device: str, text: str, ask: bool) -> dict[str, Any]:
        s = settings.get()
        ai.take_notices()
        notices: list[str] = []
        ai_error: dict[str, Any] | None = None
        self.searched.pop(device, None)
        obj = rules.quick(text)
        used_ai = False
        if obj is None:
            obj = self._from_memory(device, text)  # asked before: the same target at once, no search, no AI
        if obj is None and ai.enabled():
            obj = self._fast_local(device, text)  # a name / "open X": answered from this PC, no AI round-trip
            if obj is not None:
                self._remember(device, text, obj)
        if obj is None:
            if ai.enabled():
                try:
                    obj = _within(PLAN_SECONDS, self._ai_plan, device, text)
                    used_ai = True
                except ai.AIError as exc:
                    ai_error = ai.describe(exc)  # the phone shows what is wrong (key, model, provider…) + copy
                    obj = rules.plan(text, s["default_shell"])
                    if obj is None:
                        return {**self._reply(f"{ai_error['title']}: {_clip(exc, 300)}"), "ai_error": ai_error}
                    notices.append(tr(f"الذكاء الاصطناعي لم يعمل ({_clip(exc, 160)})، ففهمت طلبك بالطريقة الأساسية",
                                      f"The AI did not respond ({_clip(exc, 160)}), so I used basic understanding"))
            else:
                obj = rules.plan(text, s["default_shell"])
                if obj is None:
                    return self._reply(tr(
                        "لم أفهم الطلب. للأوامر الحرة اختر مزود ذكاء اصطناعي من الإعدادات.\n"
                        "أمثلة تعمل بدونه: «افتح مجلد التنزيلات» · «افتح كروم» · «cmd: ipconfig» · «اكتب مرحبا»",
                        "I did not understand. For free-form requests choose an AI provider in Settings.\n"
                        "Works without it: \"open the Downloads folder\" · \"open chrome\" · \"cmd: ipconfig\" · \"type hello\"",
                    ))
        if "resolve" in obj:
            obj = self._resolve_plan(obj["resolve"], self._search(device, text, obj["resolve"]))
        task = {"ask": ask, "full": False, "round": 0, "text": text, "started": time.time(), "used_ai": used_ai,
                "request": time.time()}
        res = self._respond(device, text, obj, used_ai, task)
        notices += ai.take_notices()
        if notices:
            res["notice"] = "\n".join(notices)
        if ai_error and "ai_error" not in res:
            res["ai_error"] = ai_error
        found = self.searched.get(device)
        if found and found.get("line") and found.get("text") == text:
            res["searched"] = found["line"]  # a small line on the phone: what was looked for, and where
        return res

    def confirm(self, device: str, plan_id: str, approve: bool, choice: int | None = None,
                full: bool = False, answer: str | None = None) -> dict[str, Any]:
        """``full``: "full access until it finishes" — follow-up rounds of this task run without asking.
        ``answer``: the user's reply to a question the assistant asked in the middle of a task."""
        self._stage(device, "run", start=True)
        with self._lock:
            p = self.pending.get(plan_id) or {}
        self._tl.tid = TASKS.add(device, "agent", (p.get("task") or {}).get("text") or p.get("reply") or "…",
                                 state="working")
        try:
            return self._settle(self._confirm(device, plan_id, approve, choice, full, answer))
        finally:
            self.progress.pop(device, None)
            TASKS.end(self._tl.tid, "error")  # only if it was not settled (an unexpected error)
            self._tl.tid = None

    def _settle(self, res: dict[str, Any]) -> dict[str, Any]:
        """The organiser's entry: finished, or gone when the answer is a question for the user (the phone shows it)."""
        tid = getattr(self._tl, "tid", None)
        if tid:
            if res.get("kind") in ("plan", "choose", "ask"):
                TASKS.drop(tid)
            else:
                TASKS.end(tid, "done" if res.get("ok") else "error", res.get("reply") or res.get("error") or "")
        par = res.pop("parallel", "")
        if par:
            res["notice"] = (res.get("notice", "") + "\n" + par).strip()
        return res

    def _confirm(self, device: str, plan_id: str, approve: bool, choice: int | None, full: bool,
                 answer: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._purge()
            p = self.pending.pop(plan_id, None)
        if not p or p["device"] != device:
            return {"ok": False, "error": tr("انتهت صلاحية الطلب، أعد المحاولة", "This request expired, please try again")}
        if not approve:
            self._note_result(device, "user cancelled")
            return self._reply(tr("تم الإلغاء", "Cancelled"))
        task = p.get("task") or {"ask": True, "full": False, "round": 0, "text": "", "started": time.time(),
                                 "used_ai": False}
        if full:
            task["full"] = True
            task["started"] = time.time()
        task["request"] = time.time()
        if p["stage"] == "ask":
            options = p.get("options") or []
            text = str(answer or "").strip()
            if not text and choice is not None and 0 <= int(choice) < len(options):
                text = options[int(choice)]
            if not text:
                return {"ok": False, "error": tr("اكتب إجابة أو اختر واحدة", "Type an answer or pick one")}
            return self._after_answer(device, task, p.get("question", ""), text)
        if p["stage"] == "choose":
            options = p["options"]
            if choice is None or not (0 <= int(choice) < len(options)):
                return {"ok": False, "error": tr("اختيار غير صالح", "Invalid choice")}
            value = options[int(choice)]["value"]
            actions = _substitute(p["actions"], value) or [{"type": "open", "path": value}]
            steps, notes = self._prepare(actions)
            if not steps:
                return self._reply("; ".join(notes) or tr("لا يوجد ما يُنفَّذ", "Nothing to do"))
            if any(st["danger"] or st.get("needs_ok") or (st["action"]["type"] in MODIFYING_TYPES
                                                          and not st.get("read_only")) for st in steps) \
                    and not self._auto_ok(task, steps):
                return self._store_plan(device, "", steps, notes, task, p.get("cont", False))
            return self._run_task(device, steps, "", task, p.get("cont", False))  # picking was the confirmation
        return self._run_task(device, p["steps"], p.get("reply", ""), task, p.get("cont", False))

    # ---------------------------------------------------------------- progress

    def _stage(self, device: str, stage: str, start: bool = False, **info: Any) -> None:
        now = time.time()
        prev = self.progress.get(device)
        if prev is None and not start:
            return  # that request already answered (a late worker thread)
        self.progress[device] = {"stage": stage, "started": now if start or not prev else prev["started"], **info}

    def status(self, device: str) -> dict[str, Any]:
        """What the phone shows while it waits: "asking DeepSeek (deepseek-flash)…", "running: …"."""
        p = self.progress.get(device)
        if not p:
            return {"busy": False, "text": ""}
        st = p["stage"]
        if st == "ai":
            who = p.get("provider", "") + (f" ({p['model']})" if p.get("model") else "")
            text = tr(f"يسأل {who}…", f"Asking {who}…") + (tr(f" — جولة {p['round']}", f" — round {p['round']}")
                                                       if p.get("round", 1) > 1 else "")
        elif st == "lookup":
            text = tr("يبحث في الكمبيوتر عمّا طلبه الذكاء…", "Looking up what the AI asked for…")
        elif st == "run":
            text = tr("ينفّذ", "Running") + (f": {p['title']}" if p.get("title") else "…")
        elif st == "turn":
            text = tr(f"⏳ ينتظر دوره على الشاشة: «{p.get('title', '')}» تنهي جولتها ثم تتجمّد حتى أنتهي",
                      f"⏳ Waiting for the screen: “{p.get('title', '')}” finishes its round, then waits until I am done")
        elif p.get("cmd"):
            text = p["cmd"]
        elif p.get("what"):
            text = tr(f"يبحث: {p['what']}…", f"Searching: {p['what']}…")
        else:
            text = tr("يبحث في الكمبيوتر…", "Searching the PC…")
        return {"busy": True, "stage": st, "text": text, "seconds": int(time.time() - p["started"]),
                "provider": p.get("provider", ""), "model": p.get("model", "")}

    # ---------------------------------------------------------------- planning

    def _context(self, device: str) -> str:
        s = settings.get()
        kf = files.known_folders()
        ides = ide.summary()
        game = GAME.status()
        return "\n".join([
            f"App language: {i18n.language_name()}",
            f"Now: {time.strftime('%Y-%m-%d %H:%M')}",
            f"User home: {files.HOME}",
            "Known folders: " + "; ".join(f"{k} = {v}" for k, v in kf.items()),
            "Installed editors (id = name): " + (", ".join(f"{i['id']} = {i['label']}" for i in ides) or "none"),
            f"Preferred editor: {ide.pick(None) or 'none'}",
            f"Foreground window: {winapi.foreground_title() or '-'}",
            f"Last opened path: {self.last_path.get(device) or '-'}",
            _tasks_line(),
            f"Game mode: {'running: ' + game.get('goal', '') if game.get('running') else 'off'}; "
            f"default game keys: {', '.join(s.get('game_keys') or [])}",
        ] + ([f"Memory (what the user meant before): " + "; ".join(
            f"{k.split('|')[2] or k} = {v['target']}" for k, v in memory.learned_items(device))]
             if memory.learned_items(device) else []))

    def _ai_plan(self, device: str, text: str) -> dict[str, Any]:
        hist = memory.history(device)[-10:]
        msgs: list[dict[str, Any]] = hist + [
            {"role": "user", "content": f"[PC context]\n{self._context(device)}\n\n[Request]\n{text}"
                                        + self._local_search(device, text)}
        ]
        # planning is short: think little and act (commands show their own output); long requests get more time
        simple = _is_simple(text)
        return self._ai_loop(msgs, effort="low", seconds=PLAN_SECONDS - 5,
                             call_seconds=AI_CALL_SECONDS if simple else 2 * AI_CALL_SECONDS, device=device)

    def _ai_next(self, device: str, task: dict[str, Any]) -> dict[str, Any]:
        """Next round of a multi-step task: the model sees the outcome of the last actions."""
        hist = memory.history(device)[-10:]
        msgs: list[dict[str, Any]] = hist + [
            {"role": "user", "content": f"[PC context]\n{self._context(device)}\n\n[Continue]\nThe actions above ran; "
                                        f"their results are in [outcome]. Original request: {_clip(task['text'], 3000)}\n"
                                        "If the task is not finished, answer with the next plan (with \"continue\": true if "
                                        "you will need its results too). If it is finished, answer "
                                        "{\"reply\": \"<short summary of what was done>\", \"done\": true}."}
        ]
        return self._ai_loop(msgs, effort="low", device=device)

    def _after_answer(self, device: str, task: dict[str, Any], question: str, text: str) -> dict[str, Any]:
        """The user answered the assistant's question: the task goes on with that answer."""
        if not ai.enabled():
            return self._reply(tr("لا يوجد مزود ذكاء اصطناعي لمتابعة المهمة", "No AI provider to go on with the task"))
        task["round"] = task.get("round", 0) + 1
        task["used_ai"] = True
        hist = memory.history(device)[-10:]
        msgs: list[dict[str, Any]] = hist + [
            {"role": "user", "content": f"[PC context]\n{self._context(device)}\n\n[Answer]\nYou asked: {_clip(question, 400)}\n"
                                        f"The user answered: {_clip(text, 2000)}\nOriginal request: {_clip(task.get('text', ''), 3000)}\n"
                                        "Go on with the task: answer with the next plan (\"continue\": true if you will need "
                                        "its results), another question, or {\"reply\": \"<summary>\", \"done\": true}."}
        ]
        try:
            obj = _within(PLAN_SECONDS, self._ai_loop, msgs, "low", PLAN_SECONDS - 5, 2 * AI_CALL_SECONDS, device)
        except ai.AIError as exc:
            err = ai.describe(exc)
            return {**self._reply(f"{err['title']}: {_clip(exc, 300)}"), "ai_error": err}
        return self._respond(device, f"[answer] {text}", obj, True, task)

    def _ai_loop(self, msgs: list[dict[str, Any]], effort: str | None = None, seconds: float = TASK_SECONDS,
                 call_seconds: float = 120.0, device: str = "") -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        obj: dict[str, Any] = {}
        found: list[dict[str, Any]] = []  # what the lookups found, to offer as choices if the AI does not decide
        looked = False
        for rnd in range(LOOKUP_ROUNDS + 1):
            def asking(label: str, model: str, n: int = rnd + 1) -> None:
                self._stage(device, "ai", provider=label, model=model, round=n)

            obj = ai.parse_json(ai.complete(SYSTEM_PROMPT, msgs, effort=effort, timeout=call_seconds,
                                            deadline=deadline, on_try=asking))
            lookups = obj.get("lookup")
            if not lookups or rnd == LOOKUP_ROUNDS or deadline - time.monotonic() < 20:
                break
            looked = True
            self._stage(device, "search", what=" · ".join(
                str(lk.get("query") or lk.get("path") or lk.get("tool") or "") for lk in
                (lookups if isinstance(lookups, list) else [lookups]) if isinstance(lk, dict))[:120])
            results = self._run_lookups(lookups if isinstance(lookups, list) else [lookups])
            found += [r for res in results if res.get("tool") == "find" for r in res.get("results") or []]
            msgs = msgs + [
                {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)},
                {"role": "user", "content": "[Lookup results]\n" + _clip(json.dumps(results, ensure_ascii=False), 14000)
                 + "\nNow answer with the final JSON (reply / choose / actions)."},
            ]
        if obj.get("lookup") and not any(obj.get(k) for k in ("reply", "actions", "choose")):
            obj = _choices_from(found) if looked else {"reply": tr(
                "انتهى الوقت قبل أن أكمل البحث. حاول مرة أخرى أو اذكر الاسم ومكانه بدقة.",
                "Time ran out before I finished looking. Try again, or say the exact name and where it is.")}
        obj.pop("lookup", None)
        return obj

    def _fast_local(self, device: str, text: str) -> dict[str, Any] | None:
        """A name or a simple "open X": search this PC and answer without the AI (seconds instead of a minute).
        One clear match -> plan, several -> "which one?". None: nothing good found, the AI takes over."""
        if not _is_simple(text) or norm(text).split(" ", 1)[0] in ("run", "execute", "نفذ"):
            return None
        req, min_score = resolver.parse(text), FAST_MIN_SCORE
        if req is None:
            req, min_score = _bare_name(text), BARE_MIN_SCORE
        if not req or len(req["name"].split()) > FAST_NAME_WORDS or not (req["name"] or req["exts"] or req["location"]):
            return None
        cands = self._search(device, text, req)
        if not cands or cands[0]["score"] < min_score:
            return None
        return self._resolve_plan(req, cands, sure=BARE_MIN_SCORE)  # a weak single match: "did you mean this?"

    def _search(self, device: str, text: str, req: dict[str, Any]) -> list[dict[str, Any]]:
        """Search this PC once per request (the AI step reuses it), with a time limit, showing what is searched:
        the status line while it runs, and a short "searched for …" line in the answer."""
        done = self.searched.get(device)
        if done and done.get("text") == text:
            return done["cands"]
        what = resolver.search_label(req)
        self._stage(device, "search", what=what)

        def step(cmd: str) -> None:
            if (self.progress.get(device) or {}).get("stage") == "search":  # not after the request moved on
                self._stage(device, "search", what=what, cmd=cmd)

        used_cmd = []
        try:
            cands = _within(SEARCH_SECONDS, resolver.resolve, req, 8, lambda c: (used_cmd.append(c), step(c)))
        except Exception:
            cands = []
        n = len(cands)
        line = "🔎 " + (what or tr("بحث", "search")) + (" · CMD" if used_cmd else "") + " — " + (
            tr(f"وجدت {n}", f"{n} found") if n else tr("لم أجد شيئاً", "nothing found"))
        self.searched[device] = {"text": text, "req": req, "cands": cands, "line": line}
        return cands

    def _local_search(self, device: str, text: str) -> str:
        """For "open …" requests (or a bare name), search the PC first so the model can pick a real target."""
        req = resolver.parse(text) or (_bare_name(text) if _is_simple(text) else None)
        if not req:
            return ""
        cands = self._search(device, text, req)
        head = (f"\n\n[Local search] understood: name={req['name'] or '-'}, kind={req['kind']}, "
                f"types={','.join(req['exts']) or '-'}, location={req['location'] or '-'}")
        if not cands:
            return head + "\nNo match on this PC for that (try another spelling with a find lookup, or ask)."
        lines = [f"{i + 1}. ({c['kind']}) {c['label']} -> {c['target']} [score {c['score']}]" for i, c in enumerate(cands)]
        return head + "\n" + "\n".join(lines)

    def _run_lookups(self, lookups: list[Any]) -> list[dict[str, Any]]:
        """Up to 5 lookups side by side; one that takes too long reports a time-out instead of holding everything."""
        items = [lk for lk in lookups[:5] if isinstance(lk, dict)]
        slots: list[list[dict[str, Any]]] = [[] for _ in items]
        threads = [threading.Thread(target=i18n.bound(self._lookup), args=(lk, slots[i]), daemon=True)
                   for i, lk in enumerate(items)]
        for th in threads:
            th.start()
        end = time.monotonic() + LOOKUP_SECONDS
        for th in threads:
            th.join(max(0.0, end - time.monotonic()))
        return [slot[0] if slot else {"tool": lk.get("tool") or lk.get("type"), "error": "took too long"}
                for lk, slot in zip(items, slots)]

    @staticmethod
    def _lookup(lk: dict[str, Any], out: list[dict[str, Any]]) -> None:
        tool = lk.get("tool") or lk.get("type")
        try:
            if tool == "find":
                kind = str(lk.get("kind") or "any")
                ext = str(lk.get("ext") or "").strip().lstrip(".")
                exts = resolver.TYPE_WORDS.get(ext.lower()) or ([f".{ext.lower()}"] if ext else [])
                req = {"kind": kind if kind in ("file", "folder", "app", "game") else "any",
                       "name": str(lk.get("query") or ""), "exts": exts,
                       "location": files.folder_key(str(lk.get("in") or "")) if lk.get("in") else None}
                res = resolver.resolve(req, 10)
                out.append({"tool": "find", "query": lk.get("query"), "results": [
                    {"kind": r["kind"], "name": r["label"], "target": r["target"], "score": r["score"]}
                    for r in res]})
            elif tool == "list_dir":
                out.append({"tool": "list_dir", "path": lk.get("path"),
                            "items": files.list_dir(_expand(lk.get("path", "")))})
            elif tool == "read_file":
                content = files.read_text(_expand(lk.get("path", "")))
                out.append({"tool": "read_file", "path": lk.get("path"), "content": _clip(content, 8000)})
            elif tool == "windows":
                out.append({"tool": "windows", "titles": [t for _h, t in winapi.list_windows()][:40]})
            else:
                out.append({"tool": tool, "error": "unknown lookup tool"})
        except Exception as exc:
            out.append({"tool": tool, "error": str(exc)})

    def _resolve_plan(self, req: dict[str, Any], cands: list[dict[str, Any]] | None = None,
                      sure: int = 0) -> dict[str, Any]:
        """Turn a parsed "open …" request into a plan (one clear match) or a choice list.
        ``sure``: below this score even a single match is offered as a choice, not opened."""
        cands = resolver.resolve(req) if cands is None else cands
        if cands:  # leave out far weaker matches
            cands = [c for c in cands if c["score"] >= cands[0]["score"] - 30]
        what = req["name"] or (" / ".join(e.lstrip(".") for e in req["exts"]) if req["exts"] else "")
        where = tr({"Desktop": "سطح المكتب", "Documents": "المستندات", "Downloads": "التنزيلات", "Pictures": "الصور",
                    "Music": "الموسيقى", "Videos": "الفيديو", "Home": "مجلدك"}.get(req["location"] or "", ""),
                   req["location"] or "")
        if not cands:
            return {"reply": tr(f"لم أجد «{what}»" + (f" في {where}" if where else "") +
                                ". جرّب اسماً أقصر أو اذكر مكانه، مثل: «افتح ملف التقرير في سطح المكتب»",
                                f"I could not find \"{what}\"" + (f" in {where}" if where else "") +
                                ". Try a shorter name or say where it is, e.g. \"open the report file on the desktop\"")}
        if resolver.is_clear(cands) and cands[0]["score"] >= sure:
            c = cands[0]
            label = resolver.kind_label(c["kind"])
            reply = tr(f"سأفتح {label} «{c['label']}»", f"I will open the {label} \"{c['label']}\"") + \
                (f"\n{c['detail']}" if c["kind"] in ("file", "folder") else "")
            return {"reply": reply, "actions": [{"type": "open", "path": c["target"], "label": c["label"]}]}
        return {
            "reply": tr(f"وجدت {len(cands)} نتائج", f"I found {len(cands)} results") +
                     ((tr(f" لـ «{what}»", f" for \"{what}\"")) if what else ""),
            "choose": {"question": tr("أيها تقصد؟", "Which one do you mean?") if len(cands) > 1
                       else tr("هل تقصد هذا؟", "Did you mean this one?"), "options": [
                {"label": f"{resolver.type_icon(c)} {c['label']}", "detail": f"{resolver.type_label(c)} · {c['detail']}",
                 "value": c["target"]} for c in cands]},
            "actions": [{"type": "open", "path": "$choice"}],
        }

    @staticmethod
    def _auto_ok(task: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
        """Run without asking: full access granted, or the switch is off and nothing needs the user's OK
        (PowerShell, installs, admin rights) — never for dangerous steps."""
        if any(st["danger"] for st in steps):
            return False
        if task.get("full"):
            return True
        return not task.get("ask", True) and not any(st.get("needs_ok") for st in steps)

    @staticmethod
    def _instant(steps: list[dict[str, Any]]) -> bool:
        """Volume, media, read-only CMD …: run at once (unless the user wants to be asked for everything)."""
        return settings.get().get("confirm_mode") != "always" and all(
            st["action"]["type"] in INSTANT_TYPES or st.get("read_only") for st in steps)

    def _respond(self, device: str, text: str, obj: dict[str, Any], used_ai: bool,
                 task: dict[str, Any]) -> dict[str, Any]:
        reply = str(obj.get("reply") or "").strip()
        actions = [a for a in (obj.get("actions") or []) if isinstance(a, dict)]
        choose = obj.get("choose") if isinstance(obj.get("choose"), dict) else None
        if used_ai:
            self._remember(device, text, obj)
        if choose and isinstance(choose.get("options"), list) and choose["options"]:
            options = []
            for o in choose["options"][:12]:
                if isinstance(o, dict) and o.get("value"):
                    options.append({"label": _clip(o.get("label") or o["value"], 80),
                                    "detail": _clip(o.get("detail") or "", 200), "value": str(o["value"])})
                elif isinstance(o, str):
                    options.append({"label": _clip(o, 80), "detail": "", "value": o})
            if options:
                plan_id = secrets.token_urlsafe(8)
                with self._lock:
                    self.pending[plan_id] = {"device": device, "stage": "choose", "actions": actions,
                                             "options": options, "reply": reply, "created": time.time(),
                                             "task": task, "cont": used_ai and bool(obj.get("continue"))}
                return {"ok": True, "kind": "choose", "plan_id": plan_id, "reply": reply,
                        "question": str(choose.get("question") or reply or tr("اختر:", "Choose:")),
                        "options": [{"label": o["label"], "detail": o["detail"]} for o in options]}
        ask = obj.get("ask") if isinstance(obj.get("ask"), dict) else None
        if ask and ask.get("question") and used_ai and not actions:
            options = [_clip(str(o), 80) for o in (ask.get("options") or []) if o][:8]
            help_link = str(ask.get("help") or "")
            plan_id = secrets.token_urlsafe(8)
            with self._lock:
                self.pending[plan_id] = {"device": device, "stage": "ask", "question": str(ask["question"]),
                                         "options": options, "reply": reply, "created": time.time(), "task": task}
            return {"ok": True, "kind": "ask", "plan_id": plan_id, "reply": reply,
                    "question": _clip(str(ask["question"]), 400), "options": options,
                    "text": bool(ask.get("text", True)), "help": help_link if help_link.startswith("https://") else ""}
        if not actions:
            return self._reply(reply or tr("تم", "Done"))
        steps, notes = self._prepare(actions)
        if not steps:
            return self._reply("; ".join([reply] + notes if reply else notes) or tr("لا يوجد ما يُنفَّذ", "Nothing to do"))
        cont = used_ai and bool(obj.get("continue"))
        if self._instant(steps) or self._auto_ok(task, steps):
            return self._run_task(device, steps, reply, task, cont)
        return self._store_plan(device, reply, steps, notes, task, cont)

    def _run_task(self, device: str, steps: list[dict[str, Any]], reply: str, task: dict[str, Any],
                  cont: bool) -> dict[str, Any]:
        """Run the approved steps; for multi-round tasks let the model see the results and go on."""
        res = self._run_steps(device, steps, reply)
        if not task.get("round") and len(steps) == 1 and steps[0]["action"]["type"] == "open" \
                and res["results"] and res["results"][0]["ok"]:
            a = steps[0]["action"]
            key = _memory_key(task.get("text", ""))
            if key:  # next time the same words open the same thing at once
                memory.learn(device, key, str(a["path"]), str(a.get("label") or os.path.basename(str(a["path"]))))
        if not (cont and task.get("used_ai") and ai.enabled()):
            return res
        sig = json.dumps([st["action"] for st in steps], sort_keys=True, default=str)
        failed = any(not r["ok"] for r in res.get("results", []))
        task["repeats"] = (task.get("repeats", 0) + 1) if failed and sig == task.get("last_sig") else (1 if failed else 0)
        task["last_sig"] = sig
        if task["repeats"] >= REPEAT_LIMIT:  # the same failing steps again and again: stop, do not burn tokens
            res["reply"] += tr(f" — توقفت: نفس الخطوات فشلت {task['repeats']} مرات متتالية",
                               f" — stopped: the same steps failed {task['repeats']} times in a row")
            return res
        if task["round"] >= MAX_ROUNDS or time.time() - task["started"] > TASK_SECONDS or (
                task.get("ask", True) and not task.get("full")
                and time.time() - task.get("request", time.time()) > REQUEST_SECONDS):
            res["reply"] += tr(" — توقفت المتابعة (وصلت لحد الخطوات)، اطلب «كمّل» للمتابعة",
                               " — stopped following up (step limit reached); say \"continue\" to go on")
            return res
        task["round"] += 1
        try:
            obj = self._ai_next(device, task)
        except ai.AIError as exc:
            res["notice"] = tr(f"توقفت المتابعة: {exc}", f"Stopped following up: {exc}")
            res["ai_error"] = ai.describe(exc)
            return res
        nxt = self._respond(device, "[continue]", obj, True, task)
        prev = res.get("results", [])
        if nxt.get("kind") == "done":
            nxt["results"] = prev + nxt.get("results", [])
        elif nxt.get("kind") == "reply":  # the model says the task is finished
            nxt = {"ok": True, "kind": "done", "reply": nxt.get("reply") or tr("تم ✓", "Done ✓"), "results": prev}
        else:  # a new plan / choice to confirm: show what already ran first
            nxt["progress"] = prev + nxt.get("progress", [])
        return nxt

    def _store_plan(self, device: str, reply: str, steps: list[dict[str, Any]], notes: list[str],
                    task: dict[str, Any] | None = None, cont: bool = False) -> dict[str, Any]:
        plan_id = secrets.token_urlsafe(8)
        with self._lock:
            self._purge()
            self.pending[plan_id] = {"device": device, "stage": "confirm", "steps": steps, "created": time.time(),
                                     "reply": reply, "task": task, "cont": cont}
        danger = any(st["danger"] for st in steps)
        first = steps[0]
        detail = (first["detail"].splitlines() or [""])[0]
        summary = reply or (f"{first['title']}: {_clip(detail, 120)}" if detail else first["title"])
        question = summary if summary.endswith(("؟", "?")) else summary + tr(" — هل أكمل؟", " — shall I go ahead?")
        return {"ok": True, "kind": "plan", "plan_id": plan_id, "reply": summary, "question": question,
                "danger": danger, "notes": notes, "multi": len(steps) > 1 or cont,
                "follow_up": bool(task and task.get("round")),
                "steps": [{"title": st["title"], "detail": st["detail"], "danger": st["danger"],
                           "preview": st.get("preview") or ""} for st in steps]}

    # ------------------------------------------------------------ preparation

    def _prepare(self, actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        steps: list[dict[str, Any]] = []
        notes: list[str] = []
        for a in actions[:12]:
            try:
                st = self._prepare_one(dict(a))
                if st:
                    steps.append(st)
            except Exception as exc:
                notes.append(str(exc))
        return steps, notes

    def _prepare_one(self, a: dict[str, Any]) -> dict[str, Any] | None:
        t = str(a.get("type") or "")
        st: dict[str, Any] = {"action": a, "title": "", "detail": "", "danger": bool(a.get("danger")), "preview": ""}
        if t == "open":
            p = _expand(a.get("path", ""))
            a["path"] = p
            launch = (p.lower().startswith(files.URI_PREFIXES) or p.lower().endswith((".lnk", ".url", ".exe"))
                      or (not os.path.exists(p) and not re.search(r"[\\/]", p)))  # a program name like "calc"
            st["title"] = (tr("فتح المجلد", "Open folder") if os.path.isdir(p) else tr("تشغيل", "Launch") if launch
                           else tr("فتح الملف", "Open file"))
            st["detail"] = (a.get("label") or p) if launch else p
        elif t == "open_app":
            st["title"], st["detail"] = tr("تشغيل برنامج", "Start program"), str(a.get("name", ""))
        elif t == "open_url":
            url = str(a.get("url", ""))
            if not re.match(r"^https?://", url):
                raise ValueError(tr(f"رابط غير صالح: {url}", f"Invalid link: {url}"))
            st["title"], st["detail"] = tr("فتح رابط", "Open link"), url
        elif t == "open_in_ide":
            ide_id = ide.pick(a.get("ide"))
            label = ide.CATALOG[ide_id]["label"] if ide_id else tr("المحرر", "the editor")
            a["path"] = _expand(a.get("path", ""))
            st["title"], st["detail"] = tr(f"فتح في {label}", f"Open in {label}"), a["path"]
        elif t == "run":
            cmd = str(a.get("command", "")).strip()
            if not cmd:
                raise ValueError(tr("أمر فارغ", "Empty command"))
            a["shell"] = "cmd" if str(a.get("shell") or settings.get().get("default_shell") or "cmd").lower() in (
                "cmd", "cmd.exe") else "powershell"
            sh = "CMD" if a["shell"] == "cmd" else "PowerShell"
            st["danger"] = st["danger"] or shell.is_dangerous(cmd)
            st["read_only"] = shell.is_read_only(cmd, a["shell"]) and not a.get("visible") and not a.get("background") \
                and not st["danger"]
            st["needs_ok"] = shell.needs_ok(cmd, a["shell"])
            st["title"] = tr(f"تنفيذ في {sh}", f"Run in {sh}") + (
                tr(" (نافذة ظاهرة)", " (visible window)") if a.get("visible") else
                tr(" (في الخلفية مع تنبيه)", " (in the background, with an alert)") if a.get("background") else "") + (
                tr(" · يحتاج موافقتك", " · needs your OK") if st["needs_ok"] else "")
            st["detail"] = cmd + (tr(f"\nفي المجلد: {a['cwd']}", f"\nIn folder: {a['cwd']}") if a.get("cwd") else "")
        elif t == "edit_file":
            self._prepare_edit(a, st)
        elif t == "write_file":
            p = _expand(a.get("path", ""))
            a["path"] = p
            exists = os.path.exists(p)
            st["title"] = tr("استبدال ملف موجود", "Replace existing file") if exists else tr("إنشاء ملف", "Create file")
            st["detail"] = p
            st["preview"] = _clip(a.get("content", ""), 1500)
            st["danger"] = st["danger"] or exists
        elif t == "ide_chat":
            ide_id = ide.pick(a.get("ide"))
            label = ide.CATALOG[ide_id]["label"] if ide_id else tr("المحرر", "the editor")
            st["title"] = tr(f"إرسال إلى شات {label}", f"Send to {label} chat")
            st["detail"] = str(a.get("message", "")) + (tr(f"\nالملف: {a['path']}", f"\nFile: {a['path']}") if a.get("path") else "")
        elif t == "send_to_window":
            st["title"], st["detail"] = tr(f"إرسال إلى نافذة «{a.get('window', '')}»",
                                           f"Send to window \"{a.get('window', '')}\""), str(a.get("text", ""))
        elif t == "type":
            st["title"], st["detail"] = tr("كتابة نص", "Type text"), str(a.get("text", ""))
        elif t == "keys":
            keys = [str(k) for k in (a.get("keys") or [])]
            for k in keys:
                winapi.vk_of(k)
            st["title"], st["detail"] = tr("ضغط اختصار", "Press shortcut"), " + ".join(keys)
        elif t == "media":
            key = str(a.get("key", "play_pause"))
            if key not in MEDIA_KEYS:
                raise ValueError(tr(f"أمر وسائط غير معروف: {key}", f"Unknown media command: {key}"))
            st["title"], st["detail"] = tr("وسائط", "Media"), media_label(key)
        elif t == "system":
            op = str(a.get("op", ""))
            if op not in SYSTEM_OPS:
                raise ValueError(tr(f"أمر نظام غير معروف: {op}", f"Unknown system command: {op}"))
            st["title"], st["detail"] = tr("النظام", "System"), system_label(op)
            st["danger"] = st["danger"] or op in ("shutdown", "restart")
        elif t == "game_start":
            keys = a.get("keys") or settings.get().get("game_keys") or []
            secs = int(a.get("seconds", 120) or 0)
            st["title"] = tr("بدء لعب الذكاء الاصطناعي", "Start AI game play")
            st["detail"] = (f"{a.get('goal', '')}\n" + tr("المفاتيح", "Keys") + f": {', '.join(map(str, keys))} · " +
                            (f"{secs} " + tr("ث", "s") if secs > 0 else tr("حتى أوقفه", "until I stop it")))
        elif t == "game_stop":
            st["title"], st["detail"] = tr("إيقاف اللعب", "Stop game play"), ""
        elif t == "wait":
            st["title"], st["detail"] = tr("انتظار", "Wait"), f"{a.get('seconds', 1)} " + tr("ث", "s")
        elif t == "control":
            goal = str(a.get("goal") or "").strip()
            if not goal:
                raise ValueError(tr("المهمة فارغة", "The task is empty"))
            st["title"] = tr("تحكم بالشاشة حتى تنتهي المهمة", "Control the screen until the task is done")
            st["detail"] = goal + (tr(f"\nيفتح أولاً: {a['app']}", f"\nOpens first: {a['app']}") if a.get("app") else "")
        elif t == "watch":
            st["title"] = tr("مراقبة وتنبيه عند الانتهاء", "Watch and alert when finished")
            st["detail"] = (f"{a.get('label') or tr('الشاشة', 'the screen')} · " +
                            tr(f"حتى {int(a.get('minutes') or 45)} د", f"up to {int(a.get('minutes') or 45)} min"))
        else:
            raise ValueError(tr(f"إجراء غير مدعوم: {t}", f"Unsupported action: {t}"))
        return st

    def _prepare_edit(self, a: dict[str, Any], st: dict[str, Any]) -> None:
        p = _expand(a.get("path", ""))
        a["path"] = p
        instruction = str(a.get("instruction") or "").strip()
        if not os.path.isfile(p):
            raise ValueError(tr(f"الملف غير موجود: {p}", f"File not found: {p}"))
        if not instruction:
            raise ValueError(tr("لم يُحدد المطلوب تعديله", "No change was described"))
        old = files.read_text(p)
        if len(old) > EDIT_MAX_CHARS:
            raise ValueError(tr("الملف كبير على التعديل التلقائي — استخدم شات المحرر",
                                "The file is too big for automatic editing — use the editor's chat"))
        reply = ai.complete(EDIT_SYSTEM, [{"role": "user", "content": f"File: {p}\nInstruction: {instruction}\n"
                                                                       f"Summary language: {i18n.language_name()}\n\n"
                                                                       f"<<<CURRENT\n{old}\nCURRENT>>>"}],
                            json_mode=False, max_tokens=32000)
        m = re.search(r"<<<FILE\r?\n(.*?)\r?\n?FILE>>>", reply, re.S)
        if not m:
            raise ValueError(tr("لم يرجع النموذج محتوى الملف المعدل", "The model did not return the edited file"))
        new = m.group(1)
        if old.endswith("\n") and not new.endswith("\n"):
            new += "\n"
        summary = re.search(r"SUMMARY:\s*(.+)", reply)
        diff_lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), tr("قبل", "before"), tr("بعد", "after"),
                                               lineterm="", n=1))
        added = sum(1 for d in diff_lines if d.startswith("+") and not d.startswith("+++"))
        removed = sum(1 for d in diff_lines if d.startswith("-") and not d.startswith("---"))
        st["title"] = tr("تعديل ملف", "Edit file")
        st["detail"] = (f"{p}\n" + tr("التعديل", "Change") + f": {summary.group(1).strip() if summary else instruction}\n"
                        f"(+{added} / -{removed} " + tr("سطر", "lines") + ")")
        st["preview"] = _clip("\n".join(diff_lines[2:]), 3500) or tr("لا توجد تغييرات", "No changes")
        st["new_content"] = new
        st["danger"] = True if removed > 40 else st["danger"]

    # ---------------------------------------------------------------- running

    def _run_steps(self, device: str, steps: list[dict[str, Any]], reply: str = "") -> dict[str, Any]:
        """Run the steps. Steps on the screen take their turn first (a screen-control task freezes after its current
        round and goes on afterwards); the others run at once, side by side with it."""
        tid = getattr(self._tl, "tid", None)
        on_screen = [st for st in steps if _needs_screen(st)]
        other = TASKS.owner_title()
        turn = False
        if on_screen and tid and not TASKS.has_turn(tid):
            if other:
                self._stage(device, "turn", title=other)
                TASKS.update(tid, state="queued", note=tr(f"ينتظر دوره بعد جولة «{other}»", f"Waits for “{other}” to finish its round"))
            turn = TASKS.acquire(tid, urgent=True, timeout=TURN_SECONDS)
            if not turn:
                busy = tr(f"الشاشة مشغولة بمهمة «{other}» ولم تتفرّغ — أوقفها مؤقتاً من لوحة المهام ثم أعد الطلب",
                          f"The screen is busy with “{other}” and did not free up — pause it from the task list and ask again")
                return {"ok": True, "kind": "done", "reply": busy,
                        "results": [{"title": st["title"], "ok": False, "output": busy} for st in steps]}
            TASKS.update(tid, state="working", note="")
        res: dict[str, Any] | None = None
        try:
            res = self._run_all(device, steps, reply)
        finally:
            if turn:
                if res is not None:  # the frozen task is told what changed on the screen
                    TASKS.note(tid, "; ".join(
                        f"{st['title']} {_clip((st['detail'].splitlines() or [''])[0], 80)} {'✓' if r['ok'] else '✗'}"
                        for st, r in zip(steps, res["results"]) if _needs_screen(st)))
                TASKS.release(tid)
        if other and not on_screen:
            res["parallel"] = tr(f"🖥️ نُفّذ في الخلفية (عبر الطرفية) بالتوازي — «{other}» تكمل على الشاشة بدون توقف",
                                 f"🖥️ Done in the background (terminal) side by side — “{other}” goes on on the screen")
        return res

    def _run_all(self, device: str, steps: list[dict[str, Any]], reply: str) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        failed = False
        for st in steps:
            if failed:
                results.append({"title": st["title"], "ok": False, "output": tr("تم التخطي بسبب خطأ سابق",
                                                                                "Skipped because of an earlier error")})
                continue
            self._stage(device, "run", title=st["title"])
            try:
                ok, output = self._exec(device, st)
            except Exception as exc:
                ok, output = False, str(exc) or exc.__class__.__name__
            results.append({"title": st["title"], "ok": ok, "output": _clip(output, 6000)})
            failed = not ok
        if failed:
            msg = next(r for r in results if not r["ok"])
            text = tr(f"حدث خطأ في «{msg['title']}»: {_clip(msg['output'], 200)}",
                      f"Error in \"{msg['title']}\": {_clip(msg['output'], 200)}")
        else:
            text = reply or tr("تم ✓", "Done ✓")
        self._note_result(device, "; ".join(f"{r['title']}: {'ok' if r['ok'] else 'failed'} {_clip(r['output'], 1500)}"
                                            for r in results))
        return {"ok": True, "kind": "done", "reply": text, "results": results}

    def _exec(self, device: str, st: dict[str, Any]) -> tuple[bool, str]:
        a = st["action"]
        t = a["type"]
        if t == "open":
            p = a["path"]
            if p.lower().startswith(files.URI_PREFIXES):
                _startfile(p)
                return True, tr("تم التشغيل", "Started")
            if not os.path.exists(p):
                target = files.find_app(p) if not re.search(r"[\\/]", p) else None
                if not target:
                    return False, tr(f"غير موجود: {p}", f"Not found: {p}")
                _startfile(target)
                return True, tr("تم التشغيل", "Started")
            _startfile(p)
            self.last_path[device] = p
            return True, tr("تم الفتح", "Opened")
        if t == "open_app":
            name = str(a.get("name", ""))
            target = files.find_app(name) or name
            try:
                _startfile(target)
            except OSError:
                return False, tr(f"تعذر تشغيل «{name}» — لم أجده", f"Could not start \"{name}\" — not found")
            return True, tr("تم التشغيل", "Started")
        if t == "open_url":
            webbrowser.open(a["url"])
            return True, tr("تم فتح الرابط", "Link opened")
        if t == "open_in_ide":
            p = a["path"]
            if not os.path.exists(p):
                return False, tr(f"المسار غير موجود: {p}", f"Path not found: {p}")
            label = ide.open_in(a.get("ide"), p, a.get("line"))
            self.last_path[device] = p
            return True, tr(f"فُتح في {label}", f"Opened in {label}")
        if t == "run" and a.get("background") and not a.get("visible"):
            s = settings.get()
            label = _clip(str(a["command"]), 60)

            lang = i18n.lang()

            def done(res: dict) -> None:
                i18n.set_lang(lang)  # runs on the background thread
                tail = res["output"][-400:]
                EVENTS.push("command_done", (tr("✓ انتهى: ", "✓ Finished: ") if res["ok"] else tr("✗ فشل: ", "✗ Failed: "))
                            + label, tail)

            shell.run_background(str(a["command"]), str(a.get("shell") or s.get("default_shell") or "powershell"),
                                 a.get("cwd"), done)
            return True, tr("يعمل في الخلفية — سيصلك تنبيه عند الانتهاء", "Running in the background — you will get an alert when it ends")
        if t == "run":
            s = settings.get()
            res = shell.run(str(a["command"]), str(a.get("shell") or s.get("default_shell") or "powershell"),
                            a.get("cwd"), int(s.get("shell_timeout") or 60), bool(a.get("visible")))
            return bool(res["ok"]), res["output"]
        if t == "edit_file":
            return self._write(device, a["path"], st["new_content"], a.get("open_after"))
        if t == "write_file":
            return self._write(device, a["path"], str(a.get("content", "")), a.get("open_after"))
        if t == "ide_chat":
            path = _expand(a["path"]) if a.get("path") else None
            label = ide.chat(a.get("ide"), str(a.get("message", "")), path, bool(a.get("submit", True)))
            if settings.get().get("watch_after_ide_chat", True):
                WATCH.start(tr(f"شات {label}", f"{label} chat"))
                return True, tr(f"أُرسل إلى شات {label} — سأنبهك عندما ينتهي", f"Sent to {label} chat — I will alert you when it finishes")
            return True, tr(f"أُرسل إلى شات {label}", f"Sent to {label} chat")
        if t == "send_to_window":
            ide.send_to_window(str(a.get("window", "")), str(a.get("text", "")), bool(a.get("submit", True)))
            return True, tr("تم الإرسال", "Sent")
        if t == "type":
            winapi.type_text(str(a.get("text", "")))
            return True, tr("تمت الكتابة", "Typed")
        if t == "keys":
            winapi.hotkey([str(k) for k in a.get("keys") or []])
            return True, tr("تم", "Done")
        if t == "media":
            key = str(a.get("key", "play_pause"))
            times = int(a.get("times") or (5 if key.startswith("volume_") else 1))
            for _ in range(max(1, min(times, 50))):
                winapi.tap(MEDIA_KEYS[key])
            return True, tr("تم", "Done")
        if t == "system":
            return system_op(str(a["op"]))
        if t == "game_start":
            GAME.start(str(a.get("goal") or "play the game"), a.get("keys"), int(a.get("seconds", 120) or 0))
            return True, tr("بدأ اللعب — أوقفه من زر «لعب» أو قل «أوقف اللعب»",
                            "Playing — stop it from the Play button or say \"stop playing\"")
        if t == "game_stop":
            GAME.stop()
            return True, tr("تم إيقاف اللعب", "Game play stopped")
        if t == "wait":
            time.sleep(min(max(float(a.get("seconds") or 1), 0), 10))
            return True, ""
        if t == "control":
            st_ = CONTROL.start(device, str(a.get("goal") or ""), str(a.get("app") or ""))
            if st_.get("state") == "queued" and st_.get("waiting_for"):
                return True, tr(f"⏳ في الطابور (رقم {st_.get('position', 1)}): تبدأ بعد «{st_['waiting_for']}» — "
                                "لا تُلغى أي مهمة، وتتابع كل شيء من لوحة المهام",
                                f"⏳ In the queue (#{st_.get('position', 1)}): starts after “{st_['waiting_for']}” — "
                                "no task is cancelled; follow everything in the task list")
            return True, tr("بدأ التحكم بالشاشة — تابع كل جولة في التطبيق، ويمكنك إيقافه في أي وقت",
                            "Screen control started — follow every round in the app; you can stop it any time")
        if t == "watch":
            WATCH.start(str(a.get("label") or tr("الشاشة", "the screen")), float(a.get("minutes") or 45))
            return True, tr("أراقب الشاشة — سيصلك تنبيه عند الانتهاء", "Watching the screen — you will get an alert when it is done")
        return False, tr(f"إجراء غير مدعوم: {t}", f"Unsupported action: {t}")

    def _write(self, device: str, path: str, content: str, open_after: Any) -> tuple[bool, str]:
        p = Path(path)
        backup = ""
        crlf = False
        if p.exists():
            raw = p.read_bytes()
            crlf = b"\r\n" in raw
            settings.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            b = settings.BACKUP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{p.name}"
            b.write_bytes(raw)
            backup = str(b)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
        text = content.replace("\r\n", "\n")
        if crlf:
            text = text.replace("\n", "\r\n")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.last_path[device] = str(p)
        msg = tr("تم الحفظ", "Saved") + (tr(f" (نسخة احتياطية: {backup})", f" (backup: {backup})") if backup else "")
        if open_after:
            try:
                label = ide.open_in(None if open_after is True else str(open_after), str(p))
                msg += tr(f" وفُتح في {label}", f" and opened in {label}")
            except Exception as exc:
                msg += tr(f" — تعذر فتحه في المحرر: {exc}", f" — could not open it in the editor: {exc}")
        return True, msg

    # ---------------------------------------------------------------- memory

    def _remember(self, device: str, text: str, obj: dict[str, Any]) -> None:
        summary = json.dumps({k: obj[k] for k in ("reply", "choose", "actions") if k in obj}, ensure_ascii=False)
        memory.add_history(device, "user", _clip(text, 1500))
        memory.add_history(device, "assistant", _clip(summary, 2500))

    def _note_result(self, device: str, note: str) -> None:
        memory.note_last(device, note)

    def _from_memory(self, device: str, text: str) -> dict[str, Any] | None:
        """The same words as before (after a successful open): the same target, if it still exists."""
        key = _memory_key(text)
        hit = memory.recall(device, key) if key else None
        if not hit:
            return None
        target = hit["target"]
        if not (os.path.exists(target) or target.lower().startswith(files.URI_PREFIXES)):
            memory.forget(device, key)
            return None
        self.searched[device] = {"text": text, "req": None, "cands": [],
                                 "line": tr(f"🧠 من الذاكرة: «{hit['label']}»", f"🧠 From memory: \"{hit['label']}\"")}
        return {"reply": tr(f"سأفتح «{hit['label']}»", f"I will open \"{hit['label']}\""),
                "actions": [{"type": "open", "path": target, "label": hit["label"]}]}

    def clear_memory(self, device: str) -> None:
        """The phone's "clear memory" button: conversation, learned names and waiting plans."""
        memory.clear(device)
        with self._lock:
            for k in [k for k, v in self.pending.items() if v["device"] == device]:
                del self.pending[k]
        self.searched.pop(device, None)

    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, v in self.pending.items() if now - v["created"] > PLAN_TTL]:
            del self.pending[k]

    @staticmethod
    def _reply(text: str) -> dict[str, Any]:
        return {"ok": True, "kind": "reply", "reply": text}


def _tasks_line() -> str:
    """For the AI: what else is running, so it prefers background (CMD) steps while the screen is in use."""
    busy = [t for t in TASKS.listing() if t["active"] and t["kind"] == "control"]
    if not busy:
        return "Screen tasks now: none"
    return ("Screen tasks now: " + "; ".join(f"«{t['title'][:60]}» ({t['state']})" for t in busy[:4]) +
            ". Another \"control\" task waits in the queue behind them (nothing is cancelled). While they run, prefer"
            " steps that do not need the screen (CMD in the background, editing files); screen steps (open, type,"
            " keys) pause the running task for a moment between its rounds.")


def _needs_screen(st: dict[str, Any]) -> bool:
    a = st["action"]
    t = a.get("type")
    if t in SCREEN_TYPES:
        return True
    if t == "run":
        return bool(a.get("visible"))
    if t in ("edit_file", "write_file"):
        return bool(a.get("open_after"))
    return False


def _is_simple(text: str) -> bool:
    """One short request (open / play / a name …), not a task with several steps."""
    return len(text) <= 160 and not _MULTI_STEP.search(text) and not _AND_VERB.search(norm(text))


def _bare_name(text: str) -> dict[str, Any] | None:
    """A request that is only a name («فالورانت», "budget 2024"): something on this PC to open."""
    t = norm(text).strip(" .!")
    words = t.split()
    if not 1 <= len(words) <= 3 or len(t) > 40 or "?" in t or "؟" in t or t in _SMALL_TALK or words[0] in _NOT_A_NAME \
            or t in YES_WORDS or t in NO_WORDS or not re.search(r"[^\W\d_]{2}", t):  # "نعم", "2" are not names
        return None
    return resolver.parse("open " + text)


def _memory_key(text: str) -> str:
    """The same request in other words («افتح التقرير» / «شغل التقرير») gives the same key."""
    req = resolver.parse(text) or (_bare_name(text) if _is_simple(text) else None)
    if not req or not (req["name"] or req["exts"] or req["location"]):
        return ""
    return f"{req['kind']}|{req['location'] or ''}|{norm(req['name'])}|{','.join(req['exts'])}"


def _choices_from(found: list[dict[str, Any]]) -> dict[str, Any]:
    """The AI kept looking without deciding: offer what the lookups found."""
    uniq: dict[str, dict[str, Any]] = {}
    for r in sorted(found, key=lambda r: -int(r.get("score") or 0)):
        if r.get("target"):
            uniq.setdefault(str(r["target"]).lower(), r)
    opts = list(uniq.values())[:8]
    if not opts:
        return {"reply": tr("لم أجد ما تقصده على الكمبيوتر. جرّب اسماً أقصر أو اذكر مكانه.",
                            "I couldn't find that on the PC. Try a shorter name or say where it is.")}
    return {"reply": tr(f"وجدت {len(opts)} نتائج", f"I found {len(opts)} results"),
            "choose": {"question": tr("أيها تقصد؟", "Which one do you mean?"),
                       "options": [{"label": r.get("name") or r["target"], "detail": r["target"], "value": r["target"]}
                                   for r in opts]},
            "actions": [{"type": "open", "path": "$choice"}]}


def _within(seconds: float, fn: Any, *args: Any) -> Any:
    """Run ``fn`` but give up after ``seconds`` so the phone always gets an answer in time."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # handed back to the caller below
            box["error"] = exc
        box["notices"] = ai.take_notices()

    th = threading.Thread(target=i18n.bound(run), daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise ai.AIError(tr(f"لم يصل رد خلال {int(seconds)} ثانية", f"no answer within {int(seconds)} seconds"), "timeout")
    ai.add_notices(box.get("notices") or [])
    if "error" in box:
        raise box["error"]
    return box["value"]


def _substitute(obj: Any, value: str) -> Any:
    if isinstance(obj, str):
        return obj.replace("$choice", value)
    if isinstance(obj, list):
        return [_substitute(v, value) for v in obj]
    if isinstance(obj, dict):
        return {k: _substitute(v, value) for k, v in obj.items()}
    return obj


def system_op(op: str) -> tuple[bool, str]:
    cmds = {
        "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
        "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        "shutdown": ["shutdown", "/s", "/t", "5"],
        "restart": ["shutdown", "/r", "/t", "5"],
        "cancel_shutdown": ["shutdown", "/a"],
    }
    if op not in cmds:
        return False, tr(f"أمر غير معروف: {op}", f"Unknown command: {op}")
    if winapi.IS_WIN:
        subprocess.Popen(cmds[op], creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        winapi.SIM_LOG.append(("system", op))
    return True, system_label(op)


AGENT = Agent()
