"""Alerts for the phone ("task finished" etc.), delivered by long-polling GET /events."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from typing import Any

BOOT = secrets.token_hex(4)  # changes on every server start, so the phone can reset its cursor


class EventBus:
    def __init__(self, keep: int = 100) -> None:
        self._cond = threading.Condition()
        self._events: deque[dict[str, Any]] = deque(maxlen=keep)
        self._next = 1

    def push(self, kind: str, title: str, body: str = "") -> dict[str, Any]:
        with self._cond:
            ev = {"id": self._next, "time": time.time(), "kind": kind, "title": title[:120], "body": str(body)[:600]}
            self._next += 1
            self._events.append(ev)
            self._cond.notify_all()
        print(f"[alert] {title}")
        return ev

    def last_id(self) -> int:
        with self._cond:
            return self._next - 1

    def since(self, since: int, wait: float = 0.0) -> list[dict[str, Any]]:
        """Events newer than ``since``; waits up to ``wait`` seconds for one to arrive."""
        end = time.monotonic() + max(0.0, min(wait, 60.0))
        with self._cond:
            while True:
                items = [e for e in self._events if e["id"] > since]
                remaining = end - time.monotonic()
                if items or remaining <= 0:
                    return items
                self._cond.wait(remaining)


EVENTS = EventBus()
