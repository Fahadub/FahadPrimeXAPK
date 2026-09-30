"""AI game mode: the model looks at the screen and plays with an allowed set of keys."""

from __future__ import annotations

import threading
import time
from typing import Any

import ai
import screen
import settings
import winapi
import i18n
from events import EVENTS
from i18n import t as tr

UNLIMITED = 4 * 3600

SYSTEM = """You are playing a video game on a Windows PC. Each turn you get a screenshot and must reply with
ONE JSON object only:
{"note": "<very short description of what you see / plan, in the user's language>",
 "actions": [ {"key": "<key>", "ms": <hold milliseconds 30-1500>} | {"keys": ["<key>", "<key>"], "ms": <ms>} | {"wait": <ms up to 1500>} ],
 "done": false}
Rules:
- Use ONLY these keys: {keys}.
- 1 to 6 actions per turn. Short taps are 60-120 ms; hold longer to move further.
- "keys" presses several keys together (e.g. diagonal movement or run+jump).
- Set "done": true only when the goal is clearly reached or the game is over and cannot continue.
Goal: {goal}"""


class GameAgent:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"running": False, "step": 0, "note": "", "error": "", "goal": "", "keys": []}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self.state.update(kw)

    def start(self, goal: str, keys: list[str] | None = None, seconds: int = 120) -> dict[str, Any]:
        if not ai.enabled():
            raise RuntimeError(tr("وضع اللعب يحتاج مزود ذكاء اصطناعي يدعم الصور — اختره من الإعدادات",
                                  "Game mode needs an AI provider that supports images — choose one in Settings"))
        self.stop()
        allowed = [winapi.key_name(k) for k in (keys or settings.get().get("game_keys") or [])]
        allowed = [k for k in allowed if k in winapi.VK]
        if not allowed:
            raise RuntimeError(tr("لا توجد مفاتيح مسموحة للعب", "No keys are allowed for playing"))
        # 0 = until the user stops it (capped at 4 hours so a forgotten game ends)
        seconds = UNLIMITED if int(seconds or 0) <= 0 else max(10, min(int(seconds), UNLIMITED))
        self._stop.clear()
        self._set(running=True, step=0, note=tr("يبدأ…", "Starting…"), error="", goal=goal, keys=allowed,
                  ends_at=time.time() + seconds)
        self._thread = threading.Thread(target=i18n.bound(self._loop), args=(goal, allowed, seconds), daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        self._set(running=False)
        return self.status()

    def _press(self, actions: list[dict[str, Any]], allowed: list[str]) -> list[str]:
        done: list[str] = []
        for a in actions[:6]:
            if self._stop.is_set():
                break
            if "wait" in a:
                time.sleep(min(max(int(a.get("wait") or 0), 0), 1500) / 1000)
                continue
            keys = a.get("keys") or ([a["key"]] if a.get("key") else [])
            keys = [winapi.key_name(k) for k in keys]
            keys = [k for k in keys if k in allowed][:3]
            if not keys:
                continue
            ms = min(max(int(a.get("ms") or 90), 30), 1500)
            pressed: list[str] = []
            try:
                for k in keys:
                    winapi.key_down(k, scancode=True)  # scan codes work in DirectInput games
                    pressed.append(k)
                end = time.monotonic() + ms / 1000
                while time.monotonic() < end and not self._stop.is_set():
                    time.sleep(0.01)
            finally:
                for k in reversed(pressed):
                    winapi.key_up(k, scancode=True)
            done.append("+".join(keys) + f"({ms})")
            time.sleep(0.03)
        return done

    def _loop(self, goal: str, allowed: list[str], seconds: int) -> None:
        system = SYSTEM.replace("{keys}", ", ".join(allowed)).replace("{goal}", goal)
        history: list[str] = []
        end = time.time() + seconds
        step = 0
        errors = 0
        try:
            while not self._stop.is_set() and time.time() < end:
                step += 1
                img, mime = screen.capture_b64(1024)
                recent = "\n".join(history[-4:]) or "(first turn)"
                prompt = f"Turn {step}. Your recent turns:\n{recent}\nLook at the screenshot and play."
                try:
                    reply = ai.complete(system, [{"role": "user", "content": prompt}], images=[(img, mime)],
                                        effort="low", max_tokens=4000)
                    obj = ai.parse_json(reply)
                    errors = 0
                except ai.AIError as exc:
                    errors += 1
                    self._set(error=str(exc))
                    if errors >= 3:
                        break
                    time.sleep(1.5)
                    continue
                note = str(obj.get("note") or "")[:200]
                actions = obj.get("actions") if isinstance(obj.get("actions"), list) else []
                pressed = self._press([a for a in actions if isinstance(a, dict)], allowed)
                history.append(f"{step}: {note} -> {', '.join(pressed) or 'nothing'}")
                self._set(step=step, note=note, error="", last=pressed)
                if obj.get("done"):
                    self._set(note=(note + " ✓").strip())
                    break
        finally:
            for k in allowed:  # never leave a key stuck down
                try:
                    winapi.key_up(k, scancode=True)
                except Exception:
                    pass
            st = self.status()
            self._set(running=False)
            if not self._stop.is_set():  # ended by itself (goal reached, time up or errors), not by the user
                EVENTS.push("game_done", tr("انتهى لعب الذكاء", "AI play finished"),
                            (st.get("error") or st.get("note") or "") + f" — {st.get('step', 0)} " + tr("خطوة", "steps"))


GAME = GameAgent()
