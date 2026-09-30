"""The task organiser: several requests at once, without the assistants getting in each other's way.

Only one task may use the screen, mouse and keyboard at a time: it "has the turn". This program - not the AI -
decides who has it, with fixed rules:

* work that does not touch the screen (CMD commands in the background, editing files, searching) never needs the
  turn: it runs at once, side by side with a task that is using the screen;
* a screen-control task (round after round with screenshots) holds the turn round by round;
* a newer screen-control task waits in the queue behind it - it is not cancelled - and starts when the turn is free;
  among waiting tasks the older one goes first;
* a short screen step of the assistant (open a window, type, press keys) is urgent: the task that has the turn
  freezes after its current round (never in the middle of one), the step runs, then the frozen task goes on;
* a task that waits for the user's answer gives the turn away meanwhile, so the next task can work; when the answer
  comes it takes its turn back (it is older) after the other task's current round;
* a task the user paused keeps the turn for the user: only the user's own short steps may run meanwhile.

Everything that ran on the screen is written in a short journal. A frozen task is told, when it goes on, what the
other tasks did meanwhile (the screen may look different), so it continues with all the details, old and new.
"""

from __future__ import annotations

import itertools
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

URGENT, RESERVED, NORMAL = 0, 1, 2
KEEP_ENDED = 12            # finished tasks still listed on the phone
ENDED_SECONDS = 15 * 60    # … for this long
JOURNAL_KEEP = 60

ACTIVE_STATES = ("thinking", "working", "queued", "frozen", "asking", "paused")


class Coordinator:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._order = itertools.count(1)
        self.owner: str | None = None
        self.reserved: str | None = None       # a paused task keeps the turn for the user
        self._waiting: dict[str, tuple[int, int]] = {}  # task id -> (priority, order): lower goes first
        self.tasks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.journal: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ the list

    def add(self, device: str, kind: str, title: str, state: str = "thinking") -> str:
        tid = secrets.token_hex(4)
        with self._cv:
            self.tasks[tid] = {"id": tid, "device": device, "kind": kind, "title": str(title)[:160], "state": state,
                               "note": "", "result": "", "order": next(self._order), "created": time.time(),
                               "ended": 0.0}
            self._prune()
        return tid

    def update(self, tid: str, **kw: Any) -> None:
        with self._cv:
            t = self.tasks.get(tid)
            if t is not None:
                t.update(kw)

    def end(self, tid: str, state: str = "done", result: str = "") -> None:
        self.release(tid)
        with self._cv:
            self._waiting.pop(tid, None)
            if self.reserved == tid:
                self.reserved = None
            t = self.tasks.get(tid)
            if t is not None and t["state"] in ACTIVE_STATES:
                t.update(state=state, result=str(result)[:400], ended=time.time())
            self._cv.notify_all()

    def drop(self, tid: str) -> None:
        self.release(tid)
        with self._cv:
            self._waiting.pop(tid, None)
            self.tasks.pop(tid, None)
            self._cv.notify_all()

    def get(self, tid: str) -> dict[str, Any] | None:
        with self._cv:
            t = self.tasks.get(tid)
            return dict(t) if t else None

    def listing(self) -> list[dict[str, Any]]:
        """For the phone: active tasks first (in turn order), then the recently finished ones."""
        with self._cv:
            self._prune()
            queue = self._queue_locked()
            owner = self.tasks.get(self.owner or "")
            out = []
            for t in self.tasks.values():
                item = {k: t[k] for k in ("id", "kind", "title", "state", "note", "result", "created", "ended")}
                item["active"] = t["state"] in ACTIVE_STATES
                item["has_turn"] = t["id"] == self.owner
                if t["id"] in queue:
                    item["position"] = queue.index(t["id"]) + 1
                    if owner is not None:
                        item["waiting_for"] = owner["title"]
                out.append(item)
        out.sort(key=lambda i: (not i["active"], not i["has_turn"], i.get("position", 0),
                                -(i["ended"] or 0) if not i["active"] else i["created"]))
        return out

    def _prune(self) -> None:
        ended = [t for t in self.tasks.values() if t["state"] not in ACTIVE_STATES]
        now = time.time()
        for t in ended[:-KEEP_ENDED] + [t for t in ended if now - (t["ended"] or now) > ENDED_SECONDS]:
            self.tasks.pop(t["id"], None)

    # ------------------------------------------------------------------ the screen turn

    def _eligible(self, tid: str) -> bool:
        return self.reserved is None or tid == self.reserved or self._waiting[tid][0] == URGENT

    def _queue_locked(self) -> list[str]:
        return sorted(self._waiting, key=lambda t: self._waiting[t])

    def acquire(self, tid: str, urgent: bool = False, stop: threading.Event | None = None,
                timeout: float | None = None) -> bool:
        """Wait for the screen turn. False if ``stop`` was set or ``timeout`` passed first."""
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            if self.owner == tid:
                return True
            order = self.tasks.get(tid, {}).get("order", next(self._order))
            prio = URGENT if urgent else RESERVED if self.reserved == tid else NORMAL
            self._waiting[tid] = (prio, order)
            self._cv.notify_all()  # the task that has the turn may have to step aside
            try:
                while True:
                    if stop is not None and stop.is_set():
                        return False
                    if self.owner is None:
                        first = next((w for w in self._queue_locked() if self._eligible(w)), None)
                        if first == tid:
                            self.owner = tid
                            if self.reserved == tid:
                                self.reserved = None
                            return True
                    left = None if end is None else end - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    self._cv.wait(0.25 if left is None else min(0.25, left))
            finally:
                self._waiting.pop(tid, None)
                self._cv.notify_all()

    def release(self, tid: str, reserve: bool = False) -> None:
        with self._cv:
            if self.owner == tid:
                self.owner = None
                if reserve:
                    self.reserved = tid
                self._cv.notify_all()

    def unreserve(self, tid: str) -> None:
        with self._cv:
            if self.reserved == tid:
                self.reserved = None
                self._cv.notify_all()

    def has_turn(self, tid: str) -> bool:
        with self._cv:
            return self.owner == tid

    def should_yield(self, tid: str) -> bool:
        """The task with the turn asks this after each round: does an urgent step or an older task wait?"""
        with self._cv:
            if self.owner != tid:
                return False
            mine = (NORMAL, self.tasks.get(tid, {}).get("order", 0))
            return any(k < mine for w, k in self._waiting.items() if self._eligible(w))

    def owner_title(self) -> str:
        with self._cv:
            t = self.tasks.get(self.owner or "")
            return t["title"] if t else ""

    # ------------------------------------------------------------------ what happened on the screen

    def note(self, tid: str, what: str) -> None:
        with self._cv:
            t = self.tasks.get(tid)
            self.journal.append({"at": time.time(), "task": tid, "title": t["title"] if t else "", "what": str(what)[:300]})
            del self.journal[:-JOURNAL_KEEP]

    def since(self, t0: float, exclude: str) -> list[str]:
        with self._cv:
            return [f"«{j['title'][:80]}»: {j['what']}" for j in self.journal if j["at"] > t0 and j["task"] != exclude]


TASKS = Coordinator()
