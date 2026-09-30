"""Control mode: the assistant works inside programs like a person - it looks at the screen, clicks and types,
looks again, and goes on round after round until the task is done.

The steps are decided by this program, not by the AI:
* every round a fresh screenshot is sent to the chosen AI provider with the goal and what happened so far;
* the AI may only answer with the actions listed in SYSTEM, a question for the user, or "done";
* the program runs the actions, checks whether the screen changed, and stops by itself when
  - the AI says the goal is reached,
  - the same actions were tried STUCK_LIMIT times in a row and the screen did not change (stuck),
  - nothing changed on the screen for NO_CHANGE_LIMIT rounds whatever was tried,
  - MAX_ROUNDS rounds or MAX_MINUTES minutes have passed, or the user presses Stop;
* when the AI asks the user something, the loop waits for the answer from the phone and goes on.

Several screen-control tasks may be given at once: they take turns on the screen (tasks.py). A task that waits
for the user's answer, or for its turn, is frozen - not failed - and is told what the other tasks did meanwhile.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import ai
import files
import i18n
import resolver
import screen
import winapi
from events import EVENTS
from i18n import t as tr
from tasks import ACTIVE_STATES as ACTIVE
from tasks import TASKS

MAX_ROUNDS = 60
MAX_MINUTES = 30
STUCK_LIMIT = 5         # the same actions this many times in a row with no change on screen
NO_CHANGE_LIMIT = 8     # any actions, no change on screen for this many rounds
WAIT_ROUNDS_LIMIT = 20  # rounds that only wait (loading, installing)
ASK_MINUTES = 30        # how long a question waits for the user
ERROR_LIMIT = 3         # AI errors in a row
MAX_ACTIONS = 8

SYSTEM = """You operate a Windows PC for the user, like a person using the mouse and keyboard, to reach a goal.
Each round you get a screenshot ({w}x{h} pixels: give coordinates in this image) and the rounds so far.
Answer with ONE JSON object only:
{{"note": "<one short sentence in {language}: what you see and what you do now>",
 "actions": [ <up to {max_actions} actions> ],
 "ask": null,
 "done": false,
 "result": ""}}
Actions:
 {{"click": [x, y]}}  {{"double_click": [x, y]}}  {{"right_click": [x, y]}}  {{"move": [x, y]}}
 {{"drag": [x1, y1, x2, y2]}}   hold the left button from the first point to the second (drawing, moving, selecting)
 {{"scroll": <clicks, positive = up>, "at": [x, y]}}
 {{"type": "<text>"}}   {{"keys": ["ctrl", "s"]}}   {{"wait": <milliseconds, up to 3000>}}
 {{"open": "<program name or full path>"}}
Rules:
- Look carefully and click the centre of buttons, menu items, list entries and fields.
- When you are not sure, do few actions and look at the result in the next round.
- The rounds so far say whether the screen changed after each round. If nothing changed, do NOT repeat the same
  actions: try another element, a keyboard shortcut, a menu, or scroll to find it.
- Repeating is fine when the task needs it (drawing strokes, several items) and the screen changes each time.
- "ask": {{"question": "<text>", "options": ["<a>", "<b>"], "help": "<optional https link>"}} when you need the
  user to decide something (which option, which name). The user may also type another answer. No actions then.
- "done": true only when the goal is visibly reached, with a short "result". If the goal cannot be reached,
  set "done": true and explain why in "result".
Goal: {goal}"""


def _pt(v: Any) -> tuple[int, int] | None:
    try:
        return int(float(v[0])), int(float(v[1]))
    except (TypeError, ValueError, IndexError):
        return None


def _open(name: str) -> str:
    """Start a program, file or folder by name or path (the same search as the assistant)."""
    name = os.path.expandvars(os.path.expanduser(name.strip().strip('"')))
    target = name if os.path.exists(name) else ""
    if not target:
        req = resolver.parse("open " + name)
        cands = resolver.resolve(req, 3) if req else []
        if cands and cands[0]["score"] >= 58:
            target = cands[0]["target"]
    target = target or files.find_app(name) or name
    if winapi.IS_WIN:
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        winapi.SIM_LOG.append(("startfile", target))
    return target


class Control:
    """One screen-control task. Several may exist: the organiser (tasks.py) gives them the screen in turn."""

    def __init__(self, device: str, goal: str, app: str = "") -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._answered = threading.Event()
        self._answer = ""
        self._resume = threading.Event()  # set = not paused
        self._resume.set()
        self._took_over = False
        self._left_at = 0.0  # when it last gave the screen away (asked, paused or stepped aside)
        self._thread: threading.Thread | None = None
        self.goal, self.app = goal, app
        self.id = TASKS.add(device, "control", goal, state="queued")
        self.state: dict[str, Any] = {
            "id": self.id, "running": True, "state": "queued", "device": device, "goal": goal, "app": app,
            "round": 0, "max_rounds": MAX_ROUNDS, "note": "", "last": [], "history": [], "question": None,
            "result": "", "error": "", "ai_error": None, "started": time.time(), "ended": 0}

    # ------------------------------------------------------------------ for the phone

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self.state)
            st["history"] = list(st.get("history") or [])[-8:]
        if st.get("started"):
            st["elapsed"] = int((st.get("ended") or time.time()) - st["started"])
        t = next((i for i in TASKS.listing() if i["id"] == self.id), None)
        if t and t.get("position"):
            st["position"] = t["position"]
            st["waiting_for"] = t.get("waiting_for", "")
        return st

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self.state.update(kw)
        mirror = {k: kw[k] for k in ("state", "note") if k in kw}
        if mirror:
            TASKS.update(self.id, **mirror)

    def start(self) -> dict[str, Any]:
        self._thread = threading.Thread(target=i18n.bound(self._loop), daemon=True)
        self._thread.start()
        return self.status()

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self.state.get("running"))

    def current(self) -> str:
        with self._lock:
            return str(self.state.get("state") or "")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            was_running = bool(self.state.get("running"))
        self._stop.set()
        self._answered.set()
        self._resume.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        with self._lock:
            if was_running and self.state.get("state") in ACTIVE:
                self.state.update(running=False, state="stopped", ended=time.time(), question=None,
                                  result=tr("أوقفتها أنت", "You stopped it"))
        TASKS.end(self.id, "stopped", tr("أوقفتها أنت", "You stopped it"))
        return self.status()

    def pause(self) -> dict[str, Any]:
        """The user takes over the PC for a moment; the loop waits and the time does not count."""
        with self._lock:
            if not self.state.get("running"):
                raise ValueError(tr("لا توجد مهمة تعمل", "No task is running"))
        self._set(state="paused", note=tr("متوقفة مؤقتاً — اعمل ما تريد ثم اضغط «أكمل»",
                                          "Paused — do what you need, then press “Continue”"))
        self._resume.clear()
        return self.status()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if not self.state.get("running"):
                raise ValueError(tr("لا توجد مهمة تعمل", "No task is running"))
            self._took_over = True
        self._set(state="working")
        self._resume.set()
        return self.status()

    def answer(self, text: str = "", choice: int | None = None) -> dict[str, Any]:
        with self._lock:
            q = self.state.get("question")
            if not self.state.get("running") or not q:
                raise ValueError(tr("لا يوجد سؤال ينتظر إجابة", "No question is waiting for an answer"))
            options = q.get("options") or []
            if choice is not None and 0 <= int(choice) < len(options):
                text = options[int(choice)]
            self._answer = str(text or "").strip()
        self._answered.set()
        return self.status()

    # ------------------------------------------------------------------ one round's actions

    def _act(self, actions: list[dict[str, Any]], scale_x: float, scale_y: float) -> list[str]:
        """Run the AI's actions (image coordinates -> screen coordinates). Returns what was done, in words."""
        done: list[str] = []
        sw, sh = winapi.screen_size()

        def at(v: Any) -> tuple[int, int] | None:
            p = _pt(v)
            if p is None:
                return None
            return min(max(int(p[0] * scale_x), 0), sw - 1), min(max(int(p[1] * scale_y), 0), sh - 1)

        for a in actions[:MAX_ACTIONS]:
            if self._stop.is_set():
                break
            try:
                if "click" in a or "double_click" in a or "right_click" in a:
                    kind = "double_click" if "double_click" in a else "right_click" if "right_click" in a else "click"
                    p = at(a[kind])
                    if p is None:
                        continue
                    winapi.set_cursor(*p)
                    time.sleep(0.05)
                    winapi.click("right" if kind == "right_click" else "left", 2 if kind == "double_click" else 1)
                    done.append(f"{kind} {p[0]},{p[1]}")
                elif "move" in a:
                    p = at(a["move"])
                    if p:
                        winapi.set_cursor(*p)
                        done.append(f"move {p[0]},{p[1]}")
                elif "drag" in a:
                    v = a["drag"]
                    p1, p2 = at(v[:2]), at(v[2:4])
                    if not p1 or not p2:
                        continue
                    winapi.set_cursor(*p1)
                    winapi.mouse_button("left_down")
                    for i in range(1, 13):  # a smooth path, so drawing programs see a stroke
                        winapi.set_cursor(p1[0] + (p2[0] - p1[0]) * i // 12, p1[1] + (p2[1] - p1[1]) * i // 12)
                        time.sleep(0.015)
                    winapi.mouse_button("left_up")
                    done.append(f"drag {p1[0]},{p1[1]}->{p2[0]},{p2[1]}")
                elif "scroll" in a:
                    p = at(a.get("at")) if a.get("at") else None
                    if p:
                        winapi.set_cursor(*p)
                    n = max(-20, min(int(a.get("scroll") or 0), 20))
                    winapi.scroll(n * 120)
                    done.append(f"scroll {n}")
                elif "type" in a:
                    text = str(a.get("type") or "")[:2000]
                    winapi.type_text(text)
                    done.append(f"type «{text[:40]}»")
                elif "keys" in a:
                    keys = [str(k) for k in (a.get("keys") or [])][:4]
                    for k in keys:
                        winapi.vk_of(k)  # unknown key -> ValueError, skipped
                    winapi.hotkey(keys)
                    done.append("keys " + "+".join(keys))
                elif "wait" in a:
                    ms = max(0, min(int(a.get("wait") or 0), 3000))
                    end = time.monotonic() + ms / 1000
                    while time.monotonic() < end and not self._stop.is_set():
                        time.sleep(0.05)
                    done.append(f"wait {ms}")
                elif "open" in a:
                    target = _open(str(a.get("open") or ""))
                    done.append(f"open {os.path.basename(target) or target}")
                    time.sleep(1.5)
            except Exception as exc:  # one bad action must not end the task
                done.append(f"failed: {str(exc)[:80]}")
            time.sleep(0.08)
        return done

    # ------------------------------------------------------------------ the loop

    def _finish(self, state: str, result: str) -> None:
        self._set(running=False, state=state, result=result, question=None, ended=time.time())
        TASKS.note(self.id, tr(f"انتهت: {result}", f"finished: {result}"))
        TASKS.end(self.id, state, result)
        titles = {"done": tr("✓ انتهت المهمة", "✓ Task finished"),
                  "stuck": tr("توقفت المهمة: لا تقدّم", "Task stopped: no progress"),
                  "limit": tr("توقفت المهمة: وصلت للحد", "Task stopped: limit reached"),
                  "error": tr("توقفت المهمة: خطأ", "Task stopped: error")}
        if state in titles:
            EVENTS.push("control_done", titles[state], result)

    def _wait_answer(self, question: dict[str, Any]) -> str | None:
        self._answered.clear()
        self._set(state="asking", question=question,
                  note=tr("تنتظر إجابتك — وأثناء الانتظار تعمل المهام الأخرى على الشاشة",
                          "Waiting for your answer — meanwhile the other tasks can use the screen"))
        EVENTS.push("control_ask", tr("المساعد يحتاج إجابتك", "The assistant needs your answer"), question["question"])
        got = self._answered.wait(ASK_MINUTES * 60)
        self._set(question=None)
        if self._stop.is_set() or not got:
            return None
        return self._answer

    def _take_turn(self, first: bool) -> bool:
        """Wait for the screen (queued / frozen). False when stopped meanwhile."""
        other = TASKS.owner_title()
        if first:
            note = (tr(f"في الطابور — تبدأ بعد «{other}»", f"In the queue — starts after “{other}”") if other
                    else tr("في الطابور — تبدأ بعد المهمة الحالية", "In the queue — starts after the current task"))
        else:
            note = (tr(f"مجمّدة مؤقتاً — «{other}» تستخدم الشاشة الآن، ثم أكمل من حيث توقفت",
                       f"Frozen for now — “{other}” is using the screen; then I go on from where I stopped") if other
                    else tr("مجمّدة مؤقتاً — ثم أكمل من حيث توقفت", "Frozen for now — then I go on from where I stopped"))
        if not TASKS.has_turn(self.id) and self.current() != "paused":
            self._set(state="queued" if first else "frozen", note=note)
        if not TASKS.acquire(self.id, stop=self._stop):
            return False
        if self.current() != "paused":
            self._set(state="working", note=tr("أكمل…", "Going on…") if not first else tr("يبدأ…", "Starting…"))
        return True

    def _loop(self) -> None:
        goal, app = self.goal, self.app
        deadline = time.time() + MAX_MINUTES * 60
        history: list[dict[str, Any]] = []
        prev_grid: list[int] | None = None
        last: dict[str, Any] | None = None
        same_streak = 0
        prev_sig = ""
        no_change = 0
        wait_rounds = 0
        errors = 0
        answer_note = ""
        opened = False
        try:
            n = 0
            while not self._stop.is_set():
                # ---- the screen turn: the organiser decides; frozen time does not count
                if not TASKS.has_turn(self.id):
                    waited_from = time.time()
                    if not self._take_turn(first=not opened):
                        return
                    if opened:
                        deadline += time.time() - waited_from
                        others = TASKS.since(self._left_at, self.id)
                        if others:  # tell it everything that happened on the PC while it was frozen
                            answer_note += ("\nWhile this task was frozen, other tasks used the PC:\n- " + "\n- ".join(others)
                                            + "\nThe screen may look different now: look again, bring this task's program"
                                              " back to the front if needed, and do not close or undo the other tasks'"
                                              " windows or work.")
                            history.append({"n": n, "note": "frozen while other tasks used the screen",
                                            "did": ["frozen: " + "; ".join(o[:60] for o in others[-3:])],
                                            "sig": "frozen", "changed": True})
                        last, same_streak, no_change, prev_sig = None, 0, 0, ""
                if not opened:
                    opened = True
                    if app:
                        self._set(note=tr(f"يفتح {app}…", f"Opening {app}…"))
                        try:
                            _open(app)
                            time.sleep(3)
                        except Exception as exc:
                            answer_note = f"(opening {app} failed: {exc})"
                # ---- paused by the user: the turn stays kept for them (only their own short steps may run)
                if not self._resume.is_set():
                    paused_at = time.time()
                    self._left_at = paused_at
                    TASKS.release(self.id, reserve=True)
                    while not self._resume.wait(0.5):
                        if self._stop.is_set():
                            return
                    deadline += time.time() - paused_at
                    continue
                if self._stop.is_set():
                    return
                if self._took_over:  # the screen may look different now: start the checks afresh
                    self._took_over = False
                    last, same_streak, no_change, prev_sig = None, 0, 0, ""
                    answer_note = ("The user paused the task, did something on the PC and pressed continue. Look again."
                                   + answer_note)
                if n >= MAX_ROUNDS or time.time() > deadline:
                    self._finish("limit", tr(f"وصلت للحد ({n} جولة) ولم تنتهِ المهمة.",
                                             f"Reached the limit ({n} rounds) before the task was finished."))
                    return
                snap = screen.snapshot(1280)
                if last is not None:  # judge the previous round now that we see its result
                    last["changed"] = screen.changed(prev_grid, snap["grid"])
                    only_wait = bool(last["did"]) and all(d.startswith("wait") for d in last["did"])
                    if only_wait:
                        wait_rounds += 1
                    else:
                        no_change = 0 if last["changed"] else no_change + 1
                    if last["changed"]:
                        same_streak = 0  # it worked, even if the same actions repeat (drawing, lists…)
                    else:
                        same_streak = same_streak + 1 if last["sig"] == prev_sig else 1
                    prev_sig = last["sig"]
                    if same_streak >= STUCK_LIMIT:
                        self._finish("stuck", tr(f"كرّرت نفس الخطوات {same_streak} مرات ولم يتغيّر شيء على الشاشة، فتوقفت حتى لا أستهلك الرصيد.",
                                                 f"Repeated the same steps {same_streak} times with no change on screen; stopped to save tokens."))
                        return
                    if no_change >= NO_CHANGE_LIMIT:
                        self._finish("stuck", tr(f"لم يتغيّر شيء على الشاشة في آخر {no_change} جولات، فتوقفت.",
                                                 f"Nothing changed on screen in the last {no_change} rounds; stopped."))
                        return
                    if wait_rounds >= WAIT_ROUNDS_LIMIT:
                        self._finish("limit", tr("انتظرت طويلاً دون تقدّم، فتوقفت.", "Waited too long without progress; stopped."))
                        return
                prev_grid = snap["grid"]
                n += 1
                self._set(round=n)
                if self.current() != "paused":
                    self._set(state="working")
                sw, sh = winapi.screen_size()
                system = SYSTEM.format(w=snap["w"], h=snap["h"], language=i18n.language_name(),
                                       max_actions=MAX_ACTIONS, goal=goal)
                lines = [f"{h['n']}: {h['note']} -> {', '.join(h['did']) or 'nothing'} "
                         f"[{'screen changed' if h.get('changed') else 'NO CHANGE on screen'}]" for h in history[-10:]]
                prompt = (f"Round {n} of at most {MAX_ROUNDS}.\nRounds so far:\n" + ("\n".join(lines) or "(first round)")
                          + (f"\n{answer_note.strip()}" if answer_note.strip() else "") + "\nLook at the screenshot and continue.")
                answer_note = ""
                try:
                    reply = ai.complete(system, [{"role": "user", "content": prompt}], images=[(snap["b64"], snap["mime"])],
                                        effort="low", max_tokens=4000, timeout=90)
                    obj = ai.parse_json(reply)
                    errors = 0
                except ai.AIError as exc:
                    errors += 1
                    self._set(error=str(exc), ai_error=ai.describe(exc))
                    if errors >= ERROR_LIMIT:
                        self._finish("error", str(exc))
                        return
                    time.sleep(2)
                    continue
                note = str(obj.get("note") or "")[:240]
                self._set(note=note, error="", ai_error=None)
                if obj.get("done"):
                    self._finish("done", str(obj.get("result") or note or tr("تم", "Done"))[:400])
                    return
                ask = obj.get("ask") if isinstance(obj.get("ask"), dict) else None
                if ask and ask.get("question"):
                    q = {"question": str(ask["question"])[:400],
                         "options": [str(o)[:80] for o in (ask.get("options") or []) if o][:6],
                         "help": str(ask.get("help") or "")[:300] if str(ask.get("help") or "").startswith("https://") else ""}
                    # waiting for the user: the screen goes to the next task meanwhile (this one is frozen)
                    asked_at = time.time()
                    self._left_at = asked_at
                    TASKS.note(self.id, tr(f"سألت المستخدم: {q['question'][:80]}", f"asked the user: {q['question'][:80]}"))
                    TASKS.release(self.id)
                    got = self._wait_answer(q)
                    if got is None:
                        if not self._stop.is_set():
                            self._finish("limit", tr("لم تصل إجابة، فتوقفت.", "No answer came, so I stopped."))
                        return
                    deadline += time.time() - asked_at
                    answer_note = f"The user answered the question «{q['question']}»: {got}" + answer_note
                    history.append({"n": n, "note": note, "did": [f"asked: {q['question'][:60]}"], "sig": "ask",
                                    "changed": True})
                    last = None
                    continue
                actions = [a for a in (obj.get("actions") or []) if isinstance(a, dict)]
                did = self._act(actions, sw / snap["w"], sh / snap["h"])
                sig = json.dumps([{k: ([round(float(x) / 20) for x in v] if isinstance(v, list) and k != "keys" else v)
                                   for k, v in a.items()} for a in actions], sort_keys=True, default=str)
                last = {"n": n, "note": note, "did": did, "sig": sig}
                history.append(last)
                self._set(last=did, history=[{k: h.get(k) for k in ("n", "note", "did", "changed")} for h in history])
                time.sleep(0.6)  # let the program draw the result before the next screenshot
                if TASKS.should_yield(self.id):  # an urgent step or an older task waits: step aside after this round
                    TASKS.note(self.id, tr(f"الجولة {n}: {note}", f"round {n}: {note}"))
                    self._left_at = time.time()
                    TASKS.release(self.id)
        finally:
            with self._lock:
                if self.state.get("running"):
                    self.state.update(running=False, ended=time.time())
                    if self.state.get("state") in ACTIVE:
                        self.state["state"] = "stopped"
            TASKS.end(self.id, "stopped" if self._stop.is_set() else "error")


class Controls:
    """All screen-control tasks. A new one no longer cancels the running one: it waits for its turn."""

    KEEP = 10

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: dict[str, Control] = {}

    def start(self, device: str, goal: str, app: str = "") -> dict[str, Any]:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError(tr("اكتب ما المطلوب", "Say what should be done"))
        if not ai.enabled():
            raise RuntimeError(tr("التحكم بالشاشة يحتاج مزود ذكاء اصطناعي يدعم الصور — اختره من الإعدادات",
                                  "Screen control needs an AI provider that supports images — choose one in Settings"))
        c = Control(device, goal, app)
        with self._lock:
            ended = [k for k, v in self.items.items() if not v.active]
            for k in ended[:max(0, len(self.items) + 1 - self.KEEP)]:
                self.items.pop(k, None)
            self.items[c.id] = c
        return c.start()

    def _pick(self, tid: str | None = None, want: tuple[str, ...] = ()) -> Control | None:
        """The given task, or the one that matters most now: asking, working, paused, frozen, queued - then the
        most recently finished one."""
        with self._lock:
            items = list(self.items.values())
        if tid:
            return next((c for c in items if c.id == tid), None)
        rank = {s: i for i, s in enumerate(want or ("asking", "working", "paused", "frozen", "queued"))}
        live = [(c, c.current()) for c in items if c.active]
        live = [(c, s) for c, s in live if s in rank]
        if live:
            return min(live, key=lambda cs: (rank[cs[1]], cs[0].state["started"]))[0]
        if want:
            return None
        return max(items, key=lambda c: c.state.get("ended") or 0, default=None)

    def status(self, tid: str | None = None) -> dict[str, Any]:
        c = self._pick(tid)
        st = c.status() if c else {"running": False, "state": "idle"}
        st["tasks"] = TASKS.listing()
        return st

    def _need(self, tid: str | None, want: tuple[str, ...]) -> Control:
        c = self._pick(tid, want)
        if c is None:
            raise ValueError(tr("لا توجد مهمة تعمل", "No task is running"))
        return c

    def stop(self, tid: str | None = None) -> dict[str, Any]:
        c = self._pick(tid)
        return c.stop() if c else {"running": False, "state": "idle"}

    def stop_all(self) -> None:
        with self._lock:
            items = list(self.items.values())
        for c in items:
            if c.active:
                c.stop()

    def pause(self, tid: str | None = None) -> dict[str, Any]:
        return self._need(tid, ("working", "frozen", "queued")).pause()

    def resume(self, tid: str | None = None) -> dict[str, Any]:
        return self._need(tid, ("paused",)).resume()

    def answer(self, text: str = "", choice: int | None = None, tid: str | None = None) -> dict[str, Any]:
        c = self._pick(tid) if tid else self._pick(None, ("asking",))
        if c is None:
            raise ValueError(tr("لا يوجد سؤال ينتظر إجابة", "No question is waiting for an answer"))
        return c.answer(text, choice)


CONTROL = Controls()
