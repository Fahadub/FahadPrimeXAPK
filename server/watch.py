"""Watch the screen until a long task (IDE agent, install, render…) goes quiet, then alert the phone."""

from __future__ import annotations

import threading
import time
from typing import Any

import ai
import i18n
import screen
from events import EVENTS
from i18n import t as tr

SAMPLE_W = 192          # tiny frames are enough to notice activity
CHANGE_RATIO = 0.005    # >0.5% of sampled pixels changed = still working
START_GRACE = 90        # seconds to wait for any activity at all
MAX_AI_CHECKS = 4

JUDGE_PROMPT = """A long-running task was being watched on this Windows screen: "{label}".
The screen has stopped changing. Look at the screenshot and answer with ONE JSON object only:
{{"done": true|false, "summary": "<one short sentence in {language}: the outcome, or what it is waiting for>"}}
"done" is false only if it is clearly still working (spinner, progress bar, "generating", "running")."""


def _grab() -> bytes:
    _w, _h, bgra = screen.capture_bgra(SAMPLE_W, draw_cursor=False)
    return bgra[1::8]  # green channel of every other pixel


def _changed(a: bytes, b: bytes) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 1.0
    diff = sum(1 for x, y in zip(a[:n], b[:n]) if abs(x - y) > 24)
    return diff / n


class ScreenWatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"running": False, "label": ""}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def start(self, label: str, minutes: float = 45, quiet: float = 20) -> dict[str, Any]:
        self.stop()
        self._stop.clear()
        minutes = max(1.0, min(float(minutes or 45), 240.0))
        quiet = max(8.0, min(float(quiet or 20), 300.0))
        with self._lock:
            self.state = {"running": True, "label": label, "started": time.time()}
        self._thread = threading.Thread(target=i18n.bound(self._loop), args=(label, minutes * 60, quiet), daemon=True)
        self._thread.start()
        print(f"[watch] watching: {label}")
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        with self._lock:
            self.state["running"] = False

    def _loop(self, label: str, max_seconds: float, quiet: float) -> None:
        t0 = time.time()
        try:
            prev = _grab()
            active = False
            last_change = time.time()
            ai_checks = 0
            while not self._stop.wait(2.0):
                now = time.time()
                if now - t0 > max_seconds:
                    EVENTS.push("watch_timeout", tr(f"ما زال يعمل: {label}", f"Still working: {label}"),
                                tr(f"مرّت {int(max_seconds // 60)} دقيقة ولم يتوقف التغيير على الشاشة",
                                   f"{int(max_seconds // 60)} minutes passed and the screen is still changing"))
                    return
                cur = _grab()
                if _changed(prev, cur) > CHANGE_RATIO:
                    active = True
                    last_change = now
                prev = cur
                if not active:
                    if now - t0 > START_GRACE:
                        EVENTS.push("watch_done", label, tr("لم ألاحظ أي نشاط على الشاشة — ربما انتهى بسرعة",
                                                           "No activity seen on the screen — it may have finished quickly"))
                        return
                    continue
                if now - last_change < quiet:
                    continue
                verdict = self._judge(label) if ai_checks < MAX_AI_CHECKS else None
                ai_checks += 1
                if verdict and verdict.get("done") is False:
                    last_change = time.time()  # the model sees it still working: keep watching
                    continue
                summary = (verdict or {}).get("summary") or tr("توقفت الشاشة عن التغيّر", "The screen stopped changing")
                mins = int((time.time() - t0) // 60)
                EVENTS.push("watch_done", tr(f"✓ انتهى: {label}", f"✓ Finished: {label}"), f"{summary} ({mins} " + tr("د", "min") + ")")
                return
        except Exception as exc:
            EVENTS.push("watch_error", tr(f"تعذرت مراقبة: {label}", f"Could not watch: {label}"), str(exc))
        finally:
            with self._lock:
                self.state["running"] = False

    @staticmethod
    def _judge(label: str) -> dict[str, Any] | None:
        if not ai.enabled():
            return None
        try:
            img, mime = screen.capture_b64(1024)
            reply = ai.complete(JUDGE_PROMPT.format(label=label, language=i18n.language_name()), [{"role": "user", "content": "Is it finished?"}],
                                images=[(img, mime)], effort="low", max_tokens=2000)
            return ai.parse_json(reply)
        except Exception:
            return None


WATCH = ScreenWatch()
