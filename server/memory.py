"""What the assistant remembers for each phone, kept on disk (%APPDATA%\\WifiRemote\\memory\\<device>.json).

* the conversation (last messages), so the AI keeps the context even after the PC program restarts;
* what the user meant by a name («التقرير» -> the file they picked or opened), so the same request is answered
  at once next time, without searching or asking the AI again.

The phone's "clear memory" button wipes both.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import settings

MAX_HISTORY = 24
MAX_LEARNED = 200

_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] = {}


def _path(device: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", device)[:64] or "phone"
    return settings.CONFIG_DIR / "memory" / f"{safe}.json"


def _load(device: str) -> dict[str, Any]:
    with _lock:
        if device not in _cache:
            try:
                data = json.loads(_path(device).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            _cache[device] = {"history": list(data.get("history") or [])[-MAX_HISTORY:],
                              "learned": dict(data.get("learned") or {})}
        return _cache[device]


def _save(device: str) -> None:
    with _lock:
        data = _cache.get(device)
        if data is None:
            return
        p = _path(device)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            pass  # memory is a convenience: never fail a request because of it


# --------------------------------------------------------------------------- conversation

def history(device: str) -> list[dict[str, str]]:
    with _lock:
        return [dict(m) for m in _load(device)["history"]]


def add_history(device: str, role: str, content: str) -> None:
    with _lock:
        h = _load(device)["history"]
        h.append({"role": role, "content": content})
        del h[:-MAX_HISTORY]
        _save(device)


def note_last(device: str, note: str, limit: int = 8000) -> None:
    """Add the outcome of the last plan to the assistant's last message."""
    with _lock:
        h = _load(device)["history"]
        if h and h[-1]["role"] == "assistant":
            text = h[-1]["content"] + f"\n[outcome: {note}]"
            h[-1]["content"] = text[:limit]
            _save(device)


# --------------------------------------------------------------------------- names

def learn(device: str, key: str, target: str, label: str) -> None:
    if not key or not target:
        return
    with _lock:
        learned = _load(device)["learned"]
        old = learned.get(key) or {}
        learned[key] = {"target": target, "label": label or target, "at": int(time.time()),
                        "hits": int(old.get("hits", 0)) + 1 if old.get("target") == target else 1}
        if len(learned) > MAX_LEARNED:
            for k, _v in sorted(learned.items(), key=lambda kv: kv[1].get("at", 0))[:len(learned) - MAX_LEARNED]:
                del learned[k]
        _save(device)


def recall(device: str, key: str) -> dict[str, Any] | None:
    with _lock:
        hit = _load(device)["learned"].get(key)
        return dict(hit) if hit else None


def forget(device: str, key: str) -> None:
    with _lock:
        if _load(device)["learned"].pop(key, None) is not None:
            _save(device)


def learned_items(device: str, n: int = 12) -> list[tuple[str, dict[str, Any]]]:
    with _lock:
        items = sorted(_load(device)["learned"].items(), key=lambda kv: -kv[1].get("at", 0))
        return [(k, dict(v)) for k, v in items[:n]]


# --------------------------------------------------------------------------- the phone's button

def clear(device: str) -> None:
    with _lock:
        _cache[device] = {"history": [], "learned": {}}
        try:
            _path(device).unlink()
        except OSError:
            pass


def stats(device: str) -> dict[str, int]:
    with _lock:
        d = _load(device)
        return {"messages": len(d["history"]), "learned": len(d["learned"])}
