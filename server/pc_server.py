#!/usr/bin/env python3
"""Wi-Fi Remote PC companion — discovery, pairing, remote input, live screen and the smart assistant.

Run:  python pc_server.py
Stop: Ctrl+C
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai  # noqa: E402
import devices  # noqa: E402
import memory  # noqa: E402
import terminal  # noqa: E402
import i18n  # noqa: E402
import ide  # noqa: E402
import screen  # noqa: E402
import settings  # noqa: E402
import winapi  # noqa: E402
from agent import AGENT, system_op  # noqa: E402
from events import BOOT, EVENTS  # noqa: E402
from i18n import t as tr  # noqa: E402
from control import CONTROL  # noqa: E402
from tasks import TASKS  # noqa: E402
from remote import REMOTE  # noqa: E402
from game import GAME  # noqa: E402
from watch import WATCH  # noqa: E402

VERSION = "3.9"
UDP_DISCOVER_PORT = 47800
HTTP_PORT = 47801
CONFIG_DIR = settings.CONFIG_DIR
MAX_BODY = 2 * 1024 * 1024
BIND_HOST = "0.0.0.0"
DEBUG = bool(os.environ.get("WIFI_REMOTE_DEBUG"))

PC_NAME = socket.gethostname()
PC_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "wifi-remote|" + PC_NAME))

# ---------------------------------------------------------------------------
# Pairing: see devices.py (named devices, at most 2, invites, PIN to reconnect)
# ---------------------------------------------------------------------------

load_devices = devices.load


# ---------------------------------------------------------------------------
# Direct remote commands (buttons, touchpad, screen taps)
# ---------------------------------------------------------------------------

SIMPLE_KEYS = {
    "keys.up": "up", "keys.down": "down", "keys.left": "left", "keys.right": "right", "keys.enter": "enter",
    "keys.escape": "esc", "keys.tab": "tab", "keys.backspace": "backspace", "keys.space": "space",
    "keys.home": "home", "keys.end": "end", "keys.page_up": "pageup", "keys.page_down": "pagedown",
    "keys.delete": "delete",
    "media.play_pause": "media_play_pause", "media.next": "media_next", "media.previous": "media_previous",
    "media.stop": "media_stop", "media.volume_up": "volume_up", "media.volume_down": "volume_down",
    "media.mute": "volume_mute",
}
SHORTCUTS = {
    "keys.copy": ["ctrl", "c"], "keys.paste": ["ctrl", "v"], "keys.cut": ["ctrl", "x"],
    "keys.select_all": ["ctrl", "a"], "keys.undo": ["ctrl", "z"], "keys.redo": ["ctrl", "y"],
    "keys.save": ["ctrl", "s"], "keys.close": ["alt", "f4"], "keys.switch_window": ["alt", "tab"],
    "keys.show_desktop": ["win", "d"], "keys.run": ["win", "r"],
}


def run_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action in SIMPLE_KEYS:
        winapi.tap(SIMPLE_KEYS[action])
        return {"ok": True}
    if action in SHORTCUTS:
        winapi.hotkey(SHORTCUTS[action])
        return {"ok": True}
    if action == "keys.combo":
        winapi.hotkey([str(k) for k in payload.get("keys", [])])
        return {"ok": True}
    if action == "keys.text":
        text = str(payload.get("text", ""))
        if len(text) > 5000:
            return {"ok": False, "error": "text too long"}
        winapi.type_text(text)
        return {"ok": True}
    if action == "keys.edit":
        deletes = max(0, min(int(payload.get("delete") or 0), 500))
        text = str(payload.get("text", ""))[:5000]
        for _ in range(deletes):
            winapi.tap("backspace", hold=0.005)
        if text:
            winapi.type_text(text)
        return {"ok": True}
    if action == "clipboard.get":
        if payload.get("copy_first"):
            winapi.hotkey(["ctrl", "c"])
            time.sleep(0.25)
        return {"ok": True, "text": winapi.get_clipboard()[:100_000]}
    if action == "clipboard.set":
        winapi.set_clipboard(str(payload.get("text", ""))[:100_000])
        if payload.get("paste"):
            time.sleep(0.05)
            winapi.hotkey(["ctrl", "v"])
        return {"ok": True}
    if action.startswith("system."):
        op = action.split(".", 1)[1]
        if op == "open":
            return open_target(str(payload.get("path") or payload.get("name") or ""))
        ok, msg = system_op(op)
        return {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg}
    if action.startswith("mouse."):
        return handle_mouse(action, payload)
    return {"ok": False, "error": f"unknown action: {action}"}


def open_target(target: str) -> dict[str, Any]:
    """The phone's "Open…" box: a path, a known folder, a program/game or a file name."""
    import files
    import resolver

    target = target.strip().strip('"')
    if not target:
        return {"ok": False, "error": "missing path"}
    expanded = os.path.expandvars(os.path.expanduser(target))
    chosen = expanded if os.path.exists(expanded) else None
    if not chosen:
        req = resolver.parse(target) or resolver.parse("open " + target)
        cands = resolver.resolve(req) if req else []
        if cands and (resolver.is_clear(cands) or cands[0]["score"] >= 70):
            chosen = cands[0]["target"]
        elif cands:
            names = tr("، ", ", ").join(c["label"] for c in cands[:4])
            return {"ok": False, "error": tr(f"وجدت أكثر من نتيجة: {names} — اكتب الاسم بدقة أو اطلبه من المساعد",
                                             f"Several matches: {names} — type the exact name or ask the assistant")}
    chosen = chosen or files.find_app(target) or target
    try:
        if winapi.IS_WIN:
            os.startfile(chosen)  # type: ignore[attr-defined]
        else:
            winapi.SIM_LOG.append(("startfile", chosen))
        return {"ok": True, "opened": chosen}
    except OSError as exc:
        return {"ok": False, "error": tr(f"تعذر فتح «{target}»: {exc}", f"Could not open \"{target}\": {exc}")}


def _xy(payload: dict[str, Any]) -> tuple[int, int]:
    return int(payload.get("x", -1)), int(payload.get("y", -1))


def handle_mouse(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    button = str(payload.get("button", "left")).lower()
    x, y = _xy(payload)
    has_xy = x >= 0 and y >= 0

    if action == "mouse.move":
        winapi.set_cursor(x, y)
        return {"ok": True}
    if action == "mouse.move_rel":
        nx, ny = winapi.move_rel(int(payload.get("dx", 0)), int(payload.get("dy", 0)))
        return {"ok": True, "at": [nx, ny]}
    if action in ("mouse.click", "mouse.double_click"):
        if has_xy:
            winapi.set_cursor(x, y)
            if not payload.get("fast"):
                time.sleep(0.02)
                winapi.activate_at(x, y)
        winapi.click(button, 2 if action == "mouse.double_click" else 1)
        result: dict[str, Any] = {"ok": True, "at": list(winapi.get_cursor())}
        if payload.get("detect") and button == "left":
            time.sleep(0.15)  # let the app move focus/caret first
            result["text_input"] = winapi.text_input_focused()
        return result
    if action == "mouse.down":
        if has_xy:
            winapi.set_cursor(x, y)
        winapi.mouse_button("right_down" if button == "right" else "left_down")
        return {"ok": True}
    if action == "mouse.up":
        if has_xy:
            winapi.set_cursor(x, y)
        winapi.mouse_button("right_up" if button == "right" else "left_up")
        return {"ok": True}
    if action == "mouse.scroll":
        winapi.scroll(int(payload.get("delta", 120)), bool(payload.get("horizontal")))
        return {"ok": True}
    return {"ok": False, "error": f"unknown mouse action: {action}"}


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WifiRemote/" + VERSION

    def log_message(self, fmt: str, *args: Any) -> None:
        if DEBUG:
            sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def end_headers(self) -> None:
        if self.close_connection:
            self.send_header("Connection", "close")
        super().end_headers()

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _token(self, body: dict[str, Any] | None = None, query: dict[str, str] | None = None) -> str | None:
        return (
            (body or {}).get("token")
            or self.headers.get("X-Auth-Token")
            or (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            or (query or {}).get("token")
        )

    def do_GET(self) -> None:
        self._safe(self._get)

    def do_POST(self) -> None:
        self._safe(self._post)

    def _safe(self, fn: Any) -> None:
        """Always answer: a dropped connection shows up on the phone as 'unexpected end of stream'."""
        i18n.set_lang(self.headers.get("X-Lang"))
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:
            traceback.print_exc()
            try:
                self._json(500, {"ok": False, "error": tr(f"خطأ في برنامج الكمبيوتر: {exc}", f"PC program error: {exc}")})
            except OSError:
                self.close_connection = True

    def _get(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}

        if path in ("/", "/status"):
            sw, sh = winapi.screen_size()
            self._json(200, {"ok": True, "name": PC_NAME, "id": PC_ID, "version": VERSION,
                             "http_port": HTTP_PORT, "screen": {"w": sw, "h": sh}, "pairing": devices.pairing_state()})
            return

        if path == "/pairing":  # the pairing page, for the person sitting at this PC only
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json(403, {"ok": False, "error": "open this page on the PC itself"})
                return
            page = pairing_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return

        dev = devices.device_for_token(self._token(query=query))
        if not dev:
            self._json(401, {"ok": False, "error": "not paired"})
            return

        if path == "/devices":
            self._json(200, {"ok": True, **devices.listing(dev["id"]), "addresses": local_ips(), "remote": REMOTE.info(),
                             "via_internet": bool(getattr(self.server, "remote", False))})
            return

        if path == "/screen":
            try:
                width = int(query.get("w") or 1280)
                quality = int(query.get("q") or 60)
            except ValueError:
                width, quality = 1280, 60
            try:
                img, mime = screen.capture(width, quality)
            except Exception as exc:
                traceback.print_exc()
                self._json(500, {"ok": False, "error": f"screenshot failed: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(img)
            return

        if path == "/config":
            self._json(200, config_payload())
            return

        if path == "/game/status":
            self._json(200, {"ok": True, **GAME.status()})
            return

        if path == "/agent/status":  # the phone shows it while it waits for the assistant
            self._json(200, {"ok": True, **AGENT.status(dev["id"])})
            return

        if path == "/control/status":  # one screen-control task (the given one or the one that matters now) + all tasks
            self._json(200, {"ok": True, **CONTROL.status(query.get("task") or None)})
            return

        if path == "/tasks":  # the organiser's list: who has the screen, who waits, who is frozen
            self._json(200, {"ok": True, "tasks": TASKS.listing(), "turn": TASKS.owner_title()})
            return

        if path == "/terminal/ls":
            self._json(200, terminal.listing(dev["id"]))
            return

        if path == "/agent/memory":
            self._json(200, {"ok": True, **memory.stats(dev["id"])})
            return

        if path == "/events":
            try:
                since = int(query.get("since") or 0)
                wait = float(query.get("wait") or 0)
            except ValueError:
                since, wait = 0, 0.0
            if query.get("boot") != BOOT:  # first poll or the server restarted: start from now
                self._json(200, {"ok": True, "boot": BOOT, "last": EVENTS.last_id(), "events": []})
                return
            events = EVENTS.since(since, wait)
            self._json(200, {"ok": True, "boot": BOOT, "last": max([since] + [e["id"] for e in events]),
                             "events": events})
            return

        if path == "/watch/status":
            self._json(200, {"ok": True, **WATCH.status()})
            return

        self._json(404, {"ok": False, "error": "not found"})

    def _post(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._json(413, {"ok": False, "error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            self._json(400, {"ok": False, "error": "bad json"})
            return

        if path in ("/pair", "/pair/check", "/reconnect") and getattr(self.server, "remote", False):
            self._json(403, {"ok": False, "error": tr("الاقتران وإدخال رمز PIN على الواي فاي فقط، لأمانك",
                                                      "Pairing and PIN entry work on the Wi-Fi only, for your safety")})
            return
        if path in ("/pair", "/pair/check", "/reconnect"):
            ip = self.client_address[0]
            try:
                if path == "/pair/check":  # step 1: the code only
                    self._json(200, {"ok": True, **devices.check(str(body.get("code", "")), ip, str(body.get("device_key", "")),
                                                                  str(body.get("name", "")))})
                    return
                if path == "/pair":
                    if not body.get("pin"):  # the v2.4 app did not ask for a name and PIN
                        raise devices.PairError(tr("حدّث تطبيق الجوال: الاقتران يحتاج اسم الجهاز ورمز PIN",
                                                   "Update the phone app: pairing needs a device name and a PIN"))
                    result = devices.pair(str(body.get("code", "")), str(body.get("name", "")), str(body["pin"]), ip,
                                          str(body.get("replace", "")), str(body.get("device_key", "")),
                                          str(body.get("model", "")))
                    print(f"[pair] paired «{result['name']}» ({devices.count()}/{devices.MAX_DEVICES})"
                          + (f", replacing «{result['replaced']}»" if result.get("replaced") else ""))
                else:
                    result = devices.reconnect(str(body.get("name", "")), str(body.get("pin", "")),
                                               str(body.get("device_id", "")), ip)
                    print(f"[pair] «{result['name']}» reconnected")
            except devices.PairError as exc:
                self._json(exc.status, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, **result, "pc_name": PC_NAME, "pc_id": PC_ID, "version": VERSION,
                             "addresses": local_ips(), "remote": REMOTE.info()})
            return

        token = self._token(body)
        dev = devices.device_for_token(token)
        if not dev:
            self._json(401, {"ok": False, "error": "not paired"})
            return
        me = dev["id"]

        try:
            if path == "/cmd":
                action = str(body.get("action", ""))
                payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
                if DEBUG:
                    print(f"[cmd] {action} {json.dumps(payload, ensure_ascii=False)[:200]}")
                result = run_action(action, payload)
                self._json(200 if result.get("ok") else 400, result)
            elif path == "/agent":
                text = str(body.get("text", ""))
                print(f"[agent] {text[:120]}")
                self._json(200, AGENT.handle(me, text, str(body.get("source", "text")), ask=bool(body.get("ask", True))))
            elif path == "/agent/confirm":
                choice = body.get("choice")
                self._json(200, AGENT.confirm(me, str(body.get("plan_id", "")), bool(body.get("approve")),
                                              int(choice) if choice is not None else None,
                                              full=bool(body.get("full_access")),
                                              answer=str(body["answer"]) if body.get("answer") is not None else None))
            elif path == "/config":
                update_config(body)
                self._json(200, config_payload())
            elif path == "/remote/check":  # "check again" after changing the router settings
                self._json(200, {"ok": True, "remote": REMOTE.check_now()})
            elif path == "/ai/test":
                msg = ai.test()
                notes = ai.take_notices()
                self._json(200, {"ok": True, "message": msg + ("\n" + "\n".join(notes) if notes else "")})
            elif path == "/ai/models":
                pid = str(body.get("provider") or settings.get()["ai"].get("provider") or "none")
                c = ai.config_for(pid, {"base_url": body.get("base_url") or "", "api_key": body.get("api_key") or ""})
                error = ""
                try:
                    models = [m for m in ai.list_models(c) if m]
                except ai.AIError as exc:  # the saved names are still offered
                    models, error = [], str(exc)
                current = str(body.get("model") or c["model"] or "")
                self._json(200, {"ok": True, "models": models, "saved": saved_models(pid), "error": error,
                                 "suggested": current if current in models + saved_models(pid)
                                 else ai.exact_model(current, models) or ""})
            elif path == "/ai/models/forget":  # remove a model name the user saved before
                pid = str(body.get("provider") or settings.get()["ai"].get("provider") or "none")
                name = str(body.get("model") or "")
                settings.update({"ai": {"custom_models": {pid: [m for m in saved_models(pid) if m != name]}}})
                self._json(200, {"ok": True, "saved": saved_models(pid)})
            elif path == "/control/start":  # screen control: rounds with a screenshot until the task is done
                self._json(200, {"ok": True, **CONTROL.start(me, str(body.get("goal", "")), str(body.get("app", "")))})
            elif path == "/control/answer":
                choice = body.get("choice")
                self._json(200, {"ok": True, **CONTROL.answer(str(body.get("answer") or ""),
                                                             int(choice) if choice is not None else None,
                                                             str(body.get("task") or "") or None)})
            elif path == "/control/stop":
                self._json(200, {"ok": True, **CONTROL.stop(str(body.get("task") or "") or None)})
            elif path == "/control/pause":  # the user does a step on the PC themselves
                self._json(200, {"ok": True, **CONTROL.pause(str(body.get("task") or "") or None)})
            elif path == "/control/resume":
                self._json(200, {"ok": True, **CONTROL.resume(str(body.get("task") or "") or None)})
            elif path == "/terminal/run":  # the phone's built-in terminal
                cmd = str(body.get("command", ""))
                print(f"[terminal] {cmd[:120]}")
                self._json(200, terminal.run(me, cmd, str(body.get("shell") or "cmd"), bool(body.get("confirm")),
                                             bool(body.get("window"))))
            elif path == "/terminal/cd":  # browse view: into a folder / up
                terminal.set_cwd(me, str(body.get("path", "")))
                self._json(200, terminal.listing(me))
            elif path == "/terminal/open":  # browse view: open a file on the PC
                target = os.path.join(terminal.cwd(me), str(body.get("name", "")))
                self._json(200, open_target(target))
            elif path == "/agent/memory/clear":  # the phone's "clear memory" button
                AGENT.clear_memory(me)
                self._json(200, {"ok": True, **memory.stats(me)})
            elif path == "/game/start":
                st = GAME.start(str(body.get("goal") or "play the game"), body.get("keys"),
                                int(body.get("seconds", 120) or 0))  # 0 = until stopped
                self._json(200, {"ok": True, **st})
            elif path == "/game/stop":
                self._json(200, {"ok": True, **GAME.stop()})
            elif path == "/watch/start":
                st = WATCH.start(str(body.get("label") or tr("الشاشة", "the screen")), float(body.get("minutes") or 45),
                                 float(body.get("quiet") or 20))
                self._json(200, {"ok": True, **st})
            elif path == "/watch/stop":
                WATCH.stop()
                self._json(200, {"ok": True, **WATCH.status()})
            elif path == "/unpair":  # remove this device completely
                name = devices.remove(me)
                print(f"[pair] «{name}» removed itself")
                self._json(200, {"ok": True})
            elif path == "/disconnect":  # end the session; reconnect later with the PIN
                devices.disconnect(me)
                self._json(200, {"ok": True})
            elif path == "/devices/invite":
                inv = devices.invite(str(body.get("name", "")))
                print(f"[pair] invite made by «{dev['name']}» for «{inv['name']}»")
                self._json(200, {"ok": True, **inv})
            elif path == "/devices/remove":
                name = devices.remove(str(body.get("id", "")))
                if name:
                    print(f"[pair] «{dev['name']}» removed «{name}»")
                self._json(200 if name else 404, {"ok": bool(name), **devices.listing(me)} if name
                           else {"ok": False, "error": tr("الجهاز غير موجود", "Device not found")})
            elif path == "/devices/pin":
                devices.set_pin(me, str(body.get("pin", "")), str(body.get("old_pin", "")))
                self._json(200, {"ok": True, **devices.listing(me)})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except devices.PairError as exc:
            self._json(exc.status, {"ok": False, "error": str(exc)})
        except ai.AIError as exc:
            self._json(200, {"ok": False, "error": str(exc), "ai_error": ai.describe(exc)})
        except (ValueError, RuntimeError, OSError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # keep the server alive whatever happens
            traceback.print_exc()
            self._json(500, {"ok": False, "error": str(exc)})


def config_payload() -> dict[str, Any]:
    eff = ai.resolved()
    pub = settings.public(eff["api_key"])
    return {
        "ok": True,
        "settings": pub,
        "ai_ready": ai.enabled(),
        "ai_effective": {"provider": eff["provider"], "base_url": eff["base_url"], "model": eff["model"],
                         "has_key": bool(eff["api_key"])},
        "providers": [{**{k: p.get(k, "") for k in ("id", "base_url", "model")}, "label": ai.label_of(p)} for p in ai.PRESETS],
        # what is saved for every provider (so switching providers on the phone shows its own model)
        "providers_state": {p["id"]: _provider_state(p["id"]) for p in ai.PRESETS if p["id"] != "none"},
        "configured": ai.configured(),
        "ides": ide.summary(),
        "remote": REMOTE.info(),
    }


def _provider_state(pid: str) -> dict[str, Any]:
    saved = settings.get()["ai"]
    c = ai.config_for(pid)
    key = c["api_key"]
    return {"model": (saved.get("models") or {}).get(pid, ""), "base_url": (saved.get("bases") or {}).get(pid, ""),
            "has_key": bool(key), "key_hint": ("…" + key[-4:]) if len(key) >= 8 else ""}


def update_config(body: dict[str, Any]) -> None:
    patch: dict[str, Any] = {}
    if isinstance(body.get("ai"), dict):
        a = body["ai"]
        ai_patch: dict[str, Any] = {}
        if "provider" in a:
            if a["provider"] not in {p["id"] for p in ai.PRESETS}:
                raise ValueError(tr("مزود غير معروف", "Unknown provider"))
            ai_patch["provider"] = a["provider"]
        provider = ai_patch.get("provider") or settings.get()["ai"].get("provider") or "none"
        if "effort" in a:
            ai_patch["effort"] = str(a["effort"] or "medium")
        if provider != "none":
            if "base_url" in a:
                ai_patch["bases"] = {provider: str(a["base_url"] or "").strip()}
            if "model" in a:
                name = str(a["model"] or "").strip()
                ai_patch["models"] = {provider: name}  # exactly as typed: never replaced by another name
                if name and name.lower() not in (m.lower() for m in saved_models(provider)):
                    ai_patch["custom_models"] = {provider: saved_models(provider) + [name]}
            if a.get("api_key"):  # empty = keep the stored key
                ai_patch["keys"] = {provider: str(a["api_key"]).strip()}
            if a.get("clear_key"):
                ai_patch["keys"] = {provider: ""}
        if isinstance(a.get("fallback"), bool):
            ai_patch["fallback"] = a["fallback"]
        patch["ai"] = ai_patch
    if body.get("confirm_mode") in ("smart", "always"):
        patch["confirm_mode"] = body["confirm_mode"]
    if body.get("default_shell") in ("powershell", "cmd"):
        patch["default_shell"] = body["default_shell"]
    if isinstance(body.get("watch_after_ide_chat"), bool):
        patch["watch_after_ide_chat"] = body["watch_after_ide_chat"]
    if "preferred_ide" in body:
        patch["preferred_ide"] = str(body["preferred_ide"] or "")
    if isinstance(body.get("game_keys"), list):
        keys = [winapi.key_name(k) for k in body["game_keys"]]
        patch["game_keys"] = [k for k in keys if k in winapi.VK][:30]
    if isinstance(body.get("remote_access"), bool):
        patch["remote_access"] = body["remote_access"]
    if patch:
        settings.update(patch)
    if "remote_access" in patch:
        apply_remote_access()


def saved_models(provider: str) -> list[str]:
    """Model names the user typed for a provider (kept in the model list, deletable from the phone)."""
    return [str(m) for m in (settings.get()["ai"].get("custom_models") or {}).get(provider, []) if m]


def apply_remote_access() -> None:
    """Open / close the encrypted mobile-data port to match the setting."""
    if settings.get().get("remote_access"):
        try:
            REMOTE.start(Handler, CONFIG_DIR)
        except OSError as exc:
            print(f"[remote] cannot open port: {exc}")
    else:
        REMOTE.stop()


# ---------------------------------------------------------------------------
# UDP discovery
# ---------------------------------------------------------------------------


def udp_discover_loop(stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    sock.bind(("", UDP_DISCOVER_PORT))
    sock.settimeout(0.5)
    print(f"[udp] discovery on port {UDP_DISCOVER_PORT}")
    reply = json.dumps({"magic": "WFREMOTE_HERE", "v": 2, "id": PC_ID, "name": PC_NAME,
                        "http_port": HTTP_PORT, "udp_port": UDP_DISCOVER_PORT}).encode("utf-8")
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            if json.loads(data.decode("utf-8")).get("magic") == "WFREMOTE_DISCOVER":
                sock.sendto(reply, addr)
        except Exception:
            continue
    sock.close()


def local_ips() -> list[str]:
    """LAN addresses of this PC. Sent only to paired phones."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if not info[4][0].startswith(("127.", "169.254.")):
                ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


PAIRING_URL = f"http://127.0.0.1:{HTTP_PORT}/pairing"

# colours in the PC window (Windows 10+ consoles understand them once "virtual terminal" mode is on)
_COLORS = {"code": "\x1b[1;30;106m", "addr": "\x1b[1;93m", "ok": "\x1b[92m", "warn": "\x1b[93m", "dim": "\x1b[90m",
           "title": "\x1b[1;97m"}
_color_on: bool | None = None


def _enable_colors() -> bool:
    if not sys.stdout.isatty():
        return False
    if not winapi.IS_WIN:
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def paint(text: str, kind: str) -> str:
    """Colour a piece of the PC window text (plain text when the console cannot show colours)."""
    global _color_on
    if _color_on is None:
        _color_on = _enable_colors()
    return f"{_COLORS[kind]}{text}\x1b[0m" if _color_on else text


def show_banner() -> None:
    """The PC window: pair code and address, easy to copy to the phone."""
    eff = ai.resolved()
    names = devices.names()
    ips = local_ips()
    print("\n" + "=" * 60)
    print(paint(f"  Wi-Fi Remote {VERSION}  |  PC: {PC_NAME}", "title"))
    print()
    print("  >>> Pair code:   " + paint(f"  {' '.join(devices.pair_code())}  ", "code") + "   <- type it on the phone")
    print("  >>> PC address:  " + paint(ips[0] if ips else "unknown", "addr")
          + (paint(f"   (also: {', '.join(ips[1:])})", "dim") if len(ips) > 1 else "")
          + paint("   only if the phone does not find the PC", "dim"))
    print()
    print(f"  Paired devices: {len(names)}/{devices.MAX_DEVICES}" + (f"  ({', '.join(names)})" if names else ""))
    if devices.pairing_state() == "full":
        print("  2 devices paired: the code can replace one of them (the phone will ask which).")
    r = REMOTE.info()
    print("  Mobile data: " + paint({"off": "off (turn it on in the app: Settings)", "starting": "on, checking the router…",
                                      "ok": "on, router port opened", "cgnat": "on, but the internet provider blocks incoming "
                                      "connections (CGNAT)", "no_upnp": "on, but the router did not open the port (UPnP)"}
                                     .get(r["state"], r["state"]), "ok" if r["state"] == "ok" else "dim" if r["state"] == "off"
                                     else "warn"))
    if r["state"] in ("no_upnp", "cgnat"):
        print(router_guide())
    print(f"  Pairing page with big code: {PAIRING_URL}   (or run open_pairing_page.bat)")
    print(f"  AI provider: {eff['provider']}" + (f" / {eff['model']}" if eff['style'] != 'none' else "")
          + ("" if ai.enabled() or eff["style"] == "none" else "  (not ready: set key/model in the app)"))
    print(paint("  Commands: page | devices | add NAME | remove NAME | reset all | ip | code | router | help", "dim"))
    print("=" * 60 + "\n")


def router_guide() -> str:
    """What to change in the router when it did not open the mobile-data port by itself (PC window only)."""
    h = REMOTE.router_help()
    if h["state"] == "ok":
        return "  " + paint(f"Router: port {h['port']} is open ({h['method']}). Mobile data is ready.", "ok")
    if h["state"] == "off":
        return "  Mobile data is off. Turn it on in the app: Settings -> Allow connecting from outside home."
    router = f"http://{h['router']}" if h["router"] else "http://192.168.1.1  (or 192.168.0.1)"
    lines = ["  " + paint("To use the app over mobile data, the router must forward one port to this PC:", "warn"),
             "   1. On this PC open the router page:  " + paint(router, "addr")
             + paint("   (login is usually on a sticker under the router)", "dim"),
             "   2. Turn ON  " + paint("UPnP", "title") + "  (Advanced / Network / NAT / Application)  -> Save",
             "      or add a Port Forwarding / Virtual Server rule:",
             "      protocol " + paint("TCP", "title") + "   outside port " + paint(str(h["port"]), "title")
             + "   inside port " + paint(str(h["port"]), "title") + "   to " + paint(h["local_ip"] or "this PC", "addr"),
             "   3. In the app tap  \"Check again\"  (or type  router  here)."]
    if h["state"] == "cgnat":
        lines = ["  " + paint("The internet provider shares one address between many homes (CGNAT): "
                              "ask it for a public IP address.", "warn"),
                 "   Until then the phone can connect only over IPv6, if both networks have it."]
    if h["error"]:
        lines.append(paint(f"   (details: {h['error'][:150]})", "dim"))
    return "\n".join(lines)


def _router_changed(_state: dict) -> None:
    print("\n[remote] router check:")
    print(router_guide())


def pairing_page() -> str:
    """Local page (127.0.0.1 only) with the code in big digits, for people who are not technical."""
    import html

    ips = local_ips()
    info = devices.listing()
    rows = "".join(f"<li>{html.escape(d['name'])}</li>" for d in info["devices"]) or "<li>—</li>"
    code = " ".join(devices.pair_code())
    addr = html.escape(ips[0] if ips else "?")
    full = info["pairing"] == "full"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="5">
<title>Wi-Fi Remote — {html.escape(PC_NAME)}</title>
<style>
body{{margin:0;background:#0f172a;color:#f8fafc;font-family:Segoe UI,Tahoma,Arial,sans-serif}}
.wrap{{max-width:760px;margin:0 auto;padding:28px 18px}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:#94a3b8;font-size:14px}}
.code{{font-size:72px;font-weight:700;letter-spacing:10px;color:#7dd3fc;text-align:center;margin:18px 0 6px;direction:ltr}}
.box{{background:#1e293b;border-radius:16px;padding:18px;margin-top:16px}}
.addr{{font-size:26px;direction:ltr;text-align:center;color:#e2e8f0}}
ol{{line-height:1.9;margin:6px 0 0;padding-inline-start:22px}} .ar{{direction:rtl;text-align:right}}
.warn{{color:#fbbf24}}
</style></head><body><div class="wrap">
<h1>Wi-Fi Remote {VERSION} — {html.escape(PC_NAME)}</h1>
<div class="muted">{len(info['devices'])}/{info['max']} · <span dir="rtl">الأجهزة المقترنة</span> / paired devices</div>
<div class="box"><div class="muted" style="text-align:center">رمز الاقتران · Pair code</div>
<div class="code">{code}</div>
<div class="muted" style="text-align:center">يتغيّر بعد كل استخدام — هذه الصفحة تتحدّث وحدها · changes after each use — this page updates itself</div></div>
<div class="box ar"><b>طريقة الربط</b><ol>
<li>الجوال على نفس الواي فاي، وافتح تطبيق ريموت الواي فاي.</li>
<li>اكتب الرمز أعلاه واضغط <b>التالي</b>.</li>
<li>اختر رمز PIN (من 4 إلى 12 أرقام / حروف إنجليزية)، ثم <b>إضافة هذا الجوال</b>. اسم الجوال اختياري.</li>
{"<li class='warn'>يوجد جهازان مقترنان (الحد الأقصى): سيسألك الجوال أي جهاز يحل محله.</li>" if full else ""}
<li>إذا لم يجد الجوال الكمبيوتر: «إدخال عنوان الكمبيوتر يدوياً» واكتب العنوان أدناه.</li></ol></div>
<div class="box"><b>How to connect</b><ol>
<li>Phone on the same Wi-Fi, open the Wi-Fi Remote app.</li>
<li>Enter the code above and tap <b>Next</b>.</li>
<li>Choose a PIN (4-12 digits / English letters), then <b>Add this phone</b>. The phone name is optional.</li>
{"<li class='warn'>2 devices are paired (the maximum): the phone will ask which one to replace.</li>" if full else ""}
<li>If the phone does not find the PC: “Enter the PC address manually” and type the address below.</li></ol></div>
<div class="box"><div class="muted" style="text-align:center">عنوان الكمبيوتر · PC address (port {HTTP_PORT})</div>
<div class="addr">{addr}</div></div>
<div class="box"><div class="muted">الأجهزة · Devices</div><ul>{rows}</ul></div>
</div></body></html>"""


def open_pairing_page() -> None:
    try:
        webbrowser.open(PAIRING_URL)
    except Exception:
        pass


CONSOLE_HELP = """
  page           open the pairing page (big code + address) in the browser
  devices        list the paired devices
  add NAME       one-time code (15 min) for a new device called NAME
  remove NAME    unpair that device (its PIN stops working)
  reset all      unpair every device; the pair code works again
  ip             show this PC's addresses (for "enter address manually")
  code           show the pair code again
  router         check the router again (mobile data) and show what to change
"""


def console_command(line: str) -> str:
    """Commands typed in the PC window. Returns the text to print."""
    i18n.set_lang("en")  # the PC window is English (Arabic shows reversed in cmd)
    cmd, _, arg = line.strip().partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    if not cmd:
        return ""
    if cmd in ("help", "?", "h"):
        return CONSOLE_HELP
    if cmd in ("devices", "d", "list"):
        info = devices.listing()
        rows = [f"  - {d['name']}" + ("" if d["has_pin"] else "  (no PIN yet)") + ("" if d["connected"] else "  (disconnected)")
                for d in info["devices"]]
        return f"  {len(rows)}/{info['max']} devices\n" + ("\n".join(rows) if rows else "  (none)")
    if cmd == "add":
        try:
            inv = devices.invite(arg)
        except devices.PairError as exc:
            return f"  {exc}"
        return (f"  Invite code for «{inv['name']}»:  " + paint(f"  {' '.join(inv['code'])}  ", "code")
                + f"   (valid {inv['expires_in'] // 60} min, one use)\n"
                f"  On the new device: enter this code, the name «{inv['name']}» and a PIN.")
    if cmd == "remove":
        name = devices.remove_by_name(arg)
        return f"  Removed «{name}»." if name else f"  No device called «{arg}». Type: devices"
    if cmd == "reset":
        if arg.lower() != "all":
            return "  This unpairs every device. Type:  reset all"
        devices.reset()
        return "  All devices were unpaired."
    if cmd == "ip":
        return "  PC address: " + paint(", ".join(local_ips()) or "unknown", "addr") + f"  (port {HTTP_PORT})"
    if cmd == "code":
        return "  Pair code: " + paint(f"  {' '.join(devices.pair_code())}  ", "code")
    if cmd in ("page", "p"):
        open_pairing_page()
        return f"  Opening {PAIRING_URL}"
    if cmd == "router":
        if not REMOTE.running:
            return router_guide()
        REMOTE.on_state, hook = None, REMOTE.on_state  # printed once, below
        try:
            REMOTE.check_now()
        finally:
            REMOTE.on_state = hook
        return router_guide()
    return "  Unknown command. Type: help"


def console_loop(stop: threading.Event) -> None:
    try:
        for line in sys.stdin:
            if stop.is_set():
                break
            out = console_command(line)
            if out:
                print(out)
    except (OSError, ValueError):
        pass  # no console (started hidden)


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _running_status() -> dict[str, Any] | None:
    """/status of whatever listens on our port, if it is a Wi-Fi Remote (v1 has no "version")."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/status", timeout=2) as r:
            info = json.loads(r.read().decode("utf-8"))
        return info if isinstance(info, dict) and info.get("id") else None
    except Exception:
        return None


def _running_version() -> str:
    info = _running_status()
    return str(info.get("version") or "") if info else ""


def parse_netstat_pid(output: str, port: int) -> int | None:
    """PID of the process listening on ``port`` from `netstat -ano -p TCP` (state text may be localised)."""
    for line in output.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}")
                and parts[2] in ("0.0.0.0:0", "[::]:0", "*:*")):
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None


def _win(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=15,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _listener_pid(port: int) -> int | None:
    if not winapi.IS_WIN:
        return None
    try:
        return parse_netstat_pid(_win(["netstat", "-ano", "-p", "TCP"]).stdout.decode("mbcs", "replace"), port)
    except Exception:
        return None


def _process_name(pid: int) -> str:
    try:
        out = _win(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).stdout.decode("mbcs", "replace")
        return out.strip().split(",")[0].strip('"') or "?"
    except Exception:
        return "?"


def _wait_port_free(seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not _port_in_use(HTTP_PORT):
            return True
        time.sleep(0.5)
    return not _port_in_use(HTTP_PORT)


def claim_port() -> None:
    """Make sure this window owns the port: replace an older/other Wi-Fi Remote, or wait until it is free."""
    if not _port_in_use(HTTP_PORT):
        return
    info = _running_status()
    pid = _listener_pid(HTTP_PORT)
    if info is not None:
        ver = info.get("version") or "1.x (old)"
        print(f"[start] Another Wi-Fi Remote {ver} is running" + (f" (PID {pid})" if pid else "") + " - replacing it...")
        if pid and pid != os.getpid():
            try:
                _win(["taskkill", "/PID", str(pid), "/F"])
            except Exception:
                pass
        if _wait_port_free(6):
            print("[start] Old server stopped.")
            return
    else:
        who = f"{_process_name(pid)} (PID {pid})" if pid else "another program"
        print(f"[start] Port {HTTP_PORT} is used by {who}.")

    print("\n" + "!" * 64)
    print(f"  Port {HTTP_PORT} is still busy, so the phone would talk to the other program.")
    print("  Fix: close the other Wi-Fi Remote window, or end python.exe in Task Manager.")
    print("  If it was started as administrator: right-click start_pc_remote.bat -> Run as administrator.")
    print("  This window keeps waiting and starts by itself once the port is free (Ctrl+C to quit).")
    print("!" * 64 + "\n")
    while _port_in_use(HTTP_PORT):
        time.sleep(2)
    print("[start] Port is free - starting.")


def _warm_apps() -> None:
    import files

    files.warm_cache()


def main() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    if not winapi.IS_WIN:
        print("Note: this companion targets Windows; input/screen calls are simulated on this OS.")
    print(f"Wi-Fi Remote {VERSION} - starting...")
    claim_port()
    winapi.set_dpi_aware()
    settings.load()
    load_devices()
    stop = threading.Event()
    threading.Thread(target=udp_discover_loop, args=(stop,), daemon=True).start()
    devices.on_change(show_banner)
    REMOTE.on_state = _router_changed
    apply_remote_access()
    show_banner()
    threading.Thread(target=console_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=ide.detect, daemon=True).start()  # warm the editor cache
    threading.Thread(target=_warm_apps, daemon=True).start()  # programs & games list (Get-StartApps, Steam…)

    try:
        httpd = ThreadingHTTPServer((BIND_HOST, HTTP_PORT), Handler)
    except OSError as exc:
        print(f"\n[error] Cannot open port {HTTP_PORT}: {exc}")
        if getattr(exc, "winerror", None) == 10013:
            print("  Windows reserved this port (Hyper-V / WSL / Docker). Check with:")
            print("  netsh interface ipv4 show excludedportrange protocol=tcp")
        raise
    httpd.daemon_threads = True
    print(f"[http] API on port {HTTP_PORT}")
    if devices.count() == 0:  # first run: show the code big in the browser
        threading.Timer(1.0, open_pairing_page).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        GAME.stop()
        CONTROL.stop_all()
        WATCH.stop()
        REMOTE.stop()  # also removes the router port forward
        stop.set()
        httpd.server_close()


def _crash_report() -> None:
    """Keep the error visible and saved: the window must never just disappear."""
    text = traceback.format_exc()
    print("\n" + "=" * 64)
    print(text)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        log = CONFIG_DIR / "error.log"
        with open(log, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" v{VERSION}\n{text}\n")
        print(f"Wi-Fi Remote stopped because of an error. Details saved to: {log}")
    except OSError:
        print("Wi-Fi Remote stopped because of an error (see above).")
    print("Take a photo of this window and send it so it can be fixed.")
    print("=" * 64)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        _crash_report()
        sys.exit(1)
