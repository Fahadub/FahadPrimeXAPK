"""Persistent server settings (%APPDATA%\\WifiRemote\\settings.json)."""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("WIFI_REMOTE_HOME") or Path(os.environ.get("APPDATA", Path.home())) / "WifiRemote")
SETTINGS_FILE = CONFIG_DIR / "settings.json"
BACKUP_DIR = CONFIG_DIR / "backups"

DEFAULTS: dict[str, Any] = {
    "ai": {
        "provider": "none",  # see ai.PRESETS
        "effort": "medium",
        # per provider id: each provider keeps its own key, model and URL
        "keys": {},
        "models": {},
        "bases": {},
        # model names the user typed, per provider (offered in the model list next to the provider's own)
        "custom_models": {},
        # when the chosen provider fails, try the others that have a saved key
        "fallback": True,
    },
    # "smart": media/volume run instantly, everything else asks first. "always": ask for everything.
    "confirm_mode": "smart",
    "default_shell": "cmd",  # read-only CMD commands run at once; PowerShell always asks first
    "shell_timeout": 60,
    "preferred_ide": "",
    # after sending a prompt to an editor's AI chat, watch the screen and alert the phone when it finishes
    "watch_after_ide_chat": True,
    # Hotkey that opens the AI chat panel of each editor.
    "ide_chat_hotkeys": {
        "vscode": ["ctrl", "alt", "i"],
        "vscode-insiders": ["ctrl", "alt", "i"],
        "cursor": ["ctrl", "l"],
        "windsurf": ["ctrl", "l"],
        "trae": ["ctrl", "u"],
    },
    "game_keys": ["w", "a", "s", "d", "up", "down", "left", "right", "space", "enter", "shift", "e", "q", "esc"],
    "search_roots": [],
    # mobile data: encrypted port opened on the router (remote.py); off until the user turns it on in the app
    "remote_access": False,
}

_lock = threading.RLock()
_data: dict[str, Any] = {}


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> None:
    global _data
    with _lock:
        data: dict[str, Any] = {}
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        _data = _merge(DEFAULTS, data if isinstance(data, dict) else {})
        _migrate(_data)


def _migrate(data: dict[str, Any]) -> None:
    """v2.0/2.1 kept one model / URL / key for whichever provider was selected."""
    ai = data["ai"]
    pid = ai.get("provider") or "none"
    for old, new in (("model", "models"), ("base_url", "bases"), ("api_key", "keys")):
        value = ai.pop(old, None)
        if value and pid != "none":
            ai[new].setdefault(pid, value)


def get() -> dict[str, Any]:
    with _lock:
        if not _data:
            load()
        return copy.deepcopy(_data)


def update(patch: dict[str, Any]) -> dict[str, Any]:
    global _data
    with _lock:
        _data = _merge(get(), patch)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
        return copy.deepcopy(_data)


def public(current_key: str = "") -> dict[str, Any]:
    """Settings safe to send to the phone (API keys never leave the PC)."""
    s = get()
    pid = s["ai"].get("provider") or "none"
    s["ai"]["base_url"] = (s["ai"].get("bases") or {}).get(pid, "")
    s["ai"]["model"] = (s["ai"].get("models") or {}).get(pid, "")
    for k in ("keys", "models", "bases"):
        s["ai"].pop(k, None)
    s["ai"]["api_key"] = ""
    s["ai"]["has_key"] = bool(current_key)
    s["ai"]["key_hint"] = ("…" + current_key[-4:]) if len(current_key) >= 8 else ""
    return s
