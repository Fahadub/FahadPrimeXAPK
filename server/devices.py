"""Paired phones: at most MAX_DEVICES, each with a name the user wrote and its own PIN.

How a phone gets in
  * The 6-digit code shown on the PC (window + local pairing page). It changes after each use.
    When 2 phones are already paired, the PC code can replace one of them (e.g. a reinstalled app);
    the same name as an existing phone replaces that phone.
  * Or a one-time invite code made for a NAME from an already paired phone (Devices -> Add) or by typing
    ``add NAME`` in the PC window. The new phone must type the same name.
  * Every phone chooses a PIN (4-12 English letters / digits, Arabic digits accepted) when it pairs.

Coming back
  * "Disconnect" on the phone drops its session token; the device stays registered.
  * It reconnects with its name + PIN, no new code needed, as long as it was not removed.
  * Wrong PINs lock that device for a while (1 min, doubling up to 15 min).

devices.json v2: {"version": 2, "devices": {id: {name, token_sha, pin, created, last_seen}}}
Session tokens and PINs are stored only as hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any

import settings
from i18n import t as tr
from textnorm import norm

MAX_DEVICES = 2
INVITE_TTL = 15 * 60
PIN_RE = re.compile(r"^[a-z0-9]{4,12}$")
PIN_ITER = 120_000
NAME_MAX = 32
LOCK_AFTER = 5          # wrong PINs before a lock
LOCK_BASE = 60          # first lock, seconds (doubles each time)
LOCK_MAX = 15 * 60
IP_TRIES_PER_MIN = 5

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

_lock = threading.RLock()
_devices: dict[str, dict[str, Any]] = {}
_by_token: dict[str, str] = {}           # token sha -> device id
_invites: dict[str, dict[str, Any]] = {}  # code -> {"name", "expires"}
_pair_code: str | None = None
_ip_fails: dict[str, list[float]] = {}
_pin_fails: dict[str, dict[str, Any]] = {}  # device id -> {"count", "until", "locks"}
_on_change: list[Any] = []


class PairError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _file():
    return settings.CONFIG_DIR / "devices.json"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- text rules

def norm_digits(s: str) -> str:
    return (s or "").translate(_ARABIC_DIGITS)


def norm_code(s: str) -> str:
    return "".join(c for c in norm_digits(s) if c.isdigit())


def norm_pin(s: str) -> str:
    """Arabic or English digits, English letters (case ignored), spaces dropped."""
    return re.sub(r"\s+", "", norm_digits(s)).lower()


def pin_error(pin: str) -> str | None:
    if not PIN_RE.match(norm_pin(pin)):
        return tr("رمز PIN من 4 إلى 12 خانة: أرقام وحروف إنجليزية فقط",
                  "The PIN must be 4-12 characters: digits and English letters only")
    return None


def clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or ""))
    return re.sub(r"\s+", " ", name).strip()[:NAME_MAX]


def name_key(name: str) -> str:
    return norm(clean_name(name))


def _hash_pin(pin: str, salt: bytes | None = None) -> dict[str, Any]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", norm_pin(pin).encode("ascii"), salt, PIN_ITER)
    return {"salt": salt.hex(), "hash": digest.hex(), "iter": PIN_ITER}


def _pin_ok(rec: dict[str, Any], pin: str) -> bool:
    p = rec.get("pin")
    if not p:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", norm_pin(pin).encode("ascii"), bytes.fromhex(p["salt"]), int(p["iter"]))
    return secrets.compare_digest(digest.hex(), p["hash"])


# --------------------------------------------------------------------------- storage

def load() -> None:
    """Read devices.json; v2.4 files ({token: {name, created}}) are converted (tokens keep working)."""
    global _pair_code
    with _lock:
        _devices.clear()
        _by_token.clear()
        _invites.clear()
        data: dict[str, Any] = {}
        try:
            if _file().exists():
                data = json.loads(_file().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        raw = data.get("devices") if isinstance(data.get("devices"), dict) else {}
        if data.get("version") == 2:
            for did, rec in raw.items():
                if isinstance(rec, dict) and rec.get("name"):
                    _devices[did] = rec
        else:
            for token, rec in raw.items():  # legacy: the key is the token itself
                rec = rec if isinstance(rec, dict) else {}
                did = "d" + secrets.token_hex(5)
                _devices[did] = {"name": _unique_name(clean_name(rec.get("name") or "Phone") or "Phone"),
                                 "token_sha": _sha(token), "pin": None,
                                 "created": rec.get("created") or time.time(), "last_seen": 0}
            if raw:
                _save()
        for did, rec in _devices.items():
            if rec.get("token_sha"):
                _by_token[rec["token_sha"]] = did
        _pair_code = _new_code()


def _save() -> None:
    settings.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 2, "devices": _devices}, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_file())


def _changed() -> None:
    _save()
    for fn in list(_on_change):
        try:
            fn()
        except Exception:
            pass


def on_change(fn: Any) -> None:
    """Called after a device is added / removed (the PC window reprints its banner)."""
    _on_change.append(fn)


def _unique_name(name: str) -> str:
    taken = {name_key(r["name"]) for r in _devices.values()}
    if name_key(name) not in taken:
        return name
    n = 2
    while name_key(f"{name} ({n})") in taken:
        n += 1
    return f"{name} ({n})"


def _new_code() -> str:
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        if code not in _invites:
            return code


# --------------------------------------------------------------------------- state

def pairing_state() -> str:
    """"open": a phone can be added; "full": the PC code can only replace one of the paired phones."""
    with _lock:
        return "full" if len(_devices) >= MAX_DEVICES else "open"


def pair_code() -> str:
    """The code shown on the PC. It works as long as the program runs and changes after each use."""
    global _pair_code
    with _lock:
        if not _pair_code:
            _pair_code = _new_code()
        return _pair_code


def new_pair_code() -> str:
    global _pair_code
    with _lock:
        _pair_code = _new_code()
        return _pair_code


def count() -> int:
    with _lock:
        return len(_devices)


def names() -> list[str]:
    with _lock:
        return [r["name"] for r in _devices.values()]


def device_for_token(token: Any) -> dict[str, Any] | None:
    """The paired device this session token belongs to (with its "id"), or None."""
    if not token or not isinstance(token, str):
        return None
    sha = _sha(token)
    with _lock:
        did = _by_token.get(sha)
        rec = _devices.get(did) if did else None
        if not rec or rec.get("token_sha") != sha:
            return None
        rec["last_seen"] = time.time()
        return {"id": did, **rec}


def listing(me: str | None = None) -> dict[str, Any]:
    now = time.time()
    with _lock:
        for code in [c for c, v in _invites.items() if v["expires"] < now]:
            _invites.pop(code, None)
        return {
            "max": MAX_DEVICES,
            "pairing": pairing_state(),
            "devices": [{"id": did, "name": r["name"], "me": did == me, "has_pin": bool(r.get("pin")),
                         "connected": bool(r.get("token_sha")), "last_seen": int(r.get("last_seen") or 0)}
                        for did, r in sorted(_devices.items(), key=lambda kv: kv[1].get("created") or 0)],
            "invites": [{"name": v["name"], "expires_in": int(v["expires"] - now)} for v in _invites.values()],
        }


# --------------------------------------------------------------------------- rate limits

def _ip_check(ip: str) -> None:
    now = time.time()
    with _lock:
        fails = [t for t in _ip_fails.get(ip, []) if now - t < 60]
        _ip_fails[ip] = fails
        if len(fails) >= IP_TRIES_PER_MIN:
            raise PairError(tr("محاولات كثيرة، انتظر دقيقة", "Too many attempts, wait a minute"), 429)


def _ip_fail(ip: str) -> None:
    global _pair_code
    with _lock:
        _ip_fails.setdefault(ip, []).append(time.time())
        if sum(len(v) for v in _ip_fails.values()) >= 30 and _pair_code:  # guessing on the network
            _ip_fails.clear()
            _pair_code = _new_code()
            print("[pair] many wrong codes on the network — the PC code was changed")
            for fn in list(_on_change):
                try:
                    fn()
                except Exception:
                    pass


def _locked_for(did: str) -> int:
    st = _pin_fails.get(did)
    return max(0, int(st["until"] - time.time())) if st else 0


def _pin_fail(did: str) -> None:
    st = _pin_fails.setdefault(did, {"count": 0, "until": 0.0, "locks": 0})
    st["count"] += 1
    if st["count"] >= LOCK_AFTER:
        st["count"] = 0
        st["until"] = time.time() + min(LOCK_MAX, LOCK_BASE * (2 ** st["locks"]))
        st["locks"] += 1


def _wait_msg(seconds: int) -> str:
    mins = max(1, (seconds + 59) // 60)
    return tr(f"رمز PIN خطأ عدة مرات — انتظر {mins} دقيقة", f"Too many wrong PINs — wait {mins} min")


# --------------------------------------------------------------------------- actions

def _issue(did: str) -> str:
    rec = _devices[did]
    if rec.get("token_sha"):
        _by_token.pop(rec["token_sha"], None)
    token = secrets.token_urlsafe(24)
    rec["token_sha"] = _sha(token)
    rec["last_seen"] = time.time()
    _by_token[rec["token_sha"]] = did
    return token


def _match_code(code: str) -> tuple[str, dict[str, Any] | None] | None:
    """("invite", invite) or ("pc", None) for a valid code, else None. Call with the lock held."""
    invite = _invites.get(code)
    if invite and invite["expires"] < time.time():
        _invites.pop(code, None)
        invite = None
    if invite:
        return "invite", invite
    if _pair_code and len(code) == 6 and secrets.compare_digest(code, _pair_code):
        return "pc", None
    return None


def _wrong_code() -> PairError:
    return PairError(tr("الرمز غير صحيح أو تغيّر — انظر للرمز الظاهر الآن على الكمبيوتر",
                        "Wrong or changed code — look at the code shown on the PC now"), 401)


def _clean_key(key: str) -> str:
    """The phone's own id (a hash of its Android ID): not secret, only says "this is the same phone"."""
    return re.sub(r"[^0-9a-f]", "", str(key or "").lower())[:64]


def _same_phone(key: str, nm: str) -> str | None:
    """Record of this phone: same device key, or (records from before v2.9) the same name. Lock held."""
    if key:
        hit = next((d for d, r in _devices.items() if r.get("key") == key), None)
        if hit:
            return hit
    if nm:
        return next((d for d, r in _devices.items() if not r.get("key") and name_key(r["name"]) == name_key(nm)), None)
    return None


def check(code: str, ip: str, device_key: str = "", name: str = "") -> dict[str, Any]:
    """Step 1 on the phone: is this code good, and what comes next (PIN, maybe replace a phone)?"""
    _ip_check(ip)
    code = norm_code(code)
    with _lock:
        m = _match_code(code)
        if not m:
            _ip_fail(ip)
            raise _wrong_code()
        kind, invite = m
        again = _same_phone(_clean_key(device_key), clean_name(name))
        out: dict[str, Any] = {"kind": kind, "max": MAX_DEVICES,
                               "full": len(_devices) >= MAX_DEVICES and not again,
                               "devices": [{"id": d, "name": r["name"]} for d, r in _devices.items()]}
        if again:
            out["replaces"] = _devices[again]["name"]  # this phone was paired before: its old entry is reused
        if invite:
            out["name"] = invite["name"]
        return out


def pair(code: str, name: str, pin: str, ip: str, replace: str = "", device_key: str = "",
         model: str = "") -> dict[str, Any]:
    """Step 2: add this phone (or replace one with the PC code). Raises PairError with a message for the phone.

    The name is optional (default: the phone model). The same phone pairing again replaces its old entry;
    another phone of the same model gets a numbered name ("Galaxy S23 (2)")."""
    global _pair_code
    _ip_check(ip)
    code = norm_code(code)
    key = _clean_key(device_key)
    with _lock:
        m = _match_code(code)
        if not m:
            _ip_fail(ip)
            raise _wrong_code()
        kind, invite = m
        nm = clean_name(name)
        if invite:
            if nm and name_key(nm) != name_key(invite["name"]):
                _ip_fail(ip)
                raise PairError(tr("اسم الجهاز لا يطابق الاسم المكتوب في الدعوة",
                                   "The device name does not match the name in the invite"), 401)
            nm = invite["name"]
        nm = nm or clean_name(model) or "Phone"
        if len(nm) < 2:
            raise PairError(tr("اسم الجوال قصير جداً (حرفان على الأقل)", "The phone name is too short (2+ characters)"))
        # the PC code proves the user is at the PC: it may replace a chosen phone; any code reuses this phone's entry
        target = _same_phone(key, nm)
        if not invite and replace in _devices:
            target = replace
        if len(_devices) >= MAX_DEVICES and not target:
            raise PairError(tr(f"على هذا الكمبيوتر {MAX_DEVICES} أجهزة (الحد الأقصى) — اختر جهازاً ليحل هذا الجوال مكانه",
                               f"This PC already has {MAX_DEVICES} devices (the maximum) — choose one for this phone to replace"),
                            409)
        err = pin_error(pin)
        if err:
            raise PairError(err)
        replaced = None
        if target:
            old = _devices.pop(target)
            replaced = old["name"]
            if old.get("token_sha"):
                _by_token.pop(old["token_sha"], None)
            _pin_fails.pop(target, None)
        nm = _unique_name(nm)  # another phone of the same model: "(2)"
        did = "d" + secrets.token_hex(5)
        _devices[did] = {"name": nm, "key": key, "token_sha": None, "pin": _hash_pin(pin),
                         "created": time.time(), "last_seen": 0}
        token = _issue(did)
        if invite:
            _invites.pop(code, None)
        else:
            _pair_code = _new_code()  # one use: a code seen on the screen cannot be used again
        _ip_fails.pop(ip, None)
        _changed()
        return {"token": token, "device_id": did, "name": nm, "replaced": replaced}


def reconnect(name: str, pin: str, device_id: str, ip: str) -> dict[str, Any]:
    """A registered phone comes back with its name + PIN (no new code)."""
    _ip_check(ip)
    wrong = tr("اسم الجهاز أو رمز PIN غير صحيح", "Wrong device name or PIN")
    with _lock:
        did = device_id if device_id in _devices else None
        if not did:
            key = name_key(name)
            did = next((d for d, r in _devices.items() if name_key(r["name"]) == key), None)
        if not did:
            _ip_fail(ip)
            raise PairError(wrong, 401)
        wait = _locked_for(did)
        if wait:
            raise PairError(_wait_msg(wait), 429)
        rec = _devices[did]
        if not rec.get("pin"):
            raise PairError(tr("هذا الجهاز بلا رمز PIN — اقترن من جديد", "This device has no PIN — pair it again"), 401)
        if not _pin_ok(rec, pin):
            _ip_fail(ip)
            _pin_fail(did)
            wait = _locked_for(did)
            raise PairError(_wait_msg(wait) if wait else wrong, 429 if wait else 401)
        _pin_fails.pop(did, None)
        token = _issue(did)
        _save()
        return {"token": token, "device_id": did, "name": rec["name"]}


def disconnect(did: str) -> None:
    """End this phone's session; it stays registered and can come back with its PIN."""
    with _lock:
        rec = _devices.get(did)
        if rec and rec.get("token_sha"):
            _by_token.pop(rec["token_sha"], None)
            rec["token_sha"] = None
            _save()


def remove(did: str) -> str | None:
    """Unregister a device (its token and PIN stop working). Returns its name."""
    with _lock:
        rec = _devices.pop(did, None)
        if not rec:
            return None
        if rec.get("token_sha"):
            _by_token.pop(rec["token_sha"], None)
        _pin_fails.pop(did, None)
        _changed()
        return rec["name"]


def remove_by_name(name: str) -> str | None:
    with _lock:
        key = name_key(name)
        did = next((d for d, r in _devices.items() if name_key(r["name"]) == key), None)
        return remove(did) if did else None


def reset() -> None:
    """Forget every device (from the PC window); the PC code works again."""
    with _lock:
        _devices.clear()
        _by_token.clear()
        _invites.clear()
        _pin_fails.clear()
        new_pair_code()
        _changed()


def invite(name: str) -> dict[str, Any]:
    """One-time code for a device that will be called ``name``."""
    nm = clean_name(name)
    with _lock:
        now = time.time()
        for c in [c for c, v in _invites.items() if v["expires"] < now]:
            _invites.pop(c, None)
        if len(nm) < 2:
            raise PairError(tr("اكتب اسم الجهاز الجديد (حرفان على الأقل)", "Write the new device's name (2+ characters)"))
        if len(_devices) >= MAX_DEVICES:
            raise PairError(tr(f"وصلت للحد: {MAX_DEVICES} أجهزة فقط — احذف جهازاً أولاً",
                               f"Limit reached: only {MAX_DEVICES} devices — remove one first"), 403)
        key = name_key(nm)
        if any(name_key(r["name"]) == key for r in _devices.values()):
            raise PairError(tr("يوجد جهاز بنفس الاسم — اختر اسماً آخر", "A device with this name exists — choose another name"))
        for c in [c for c, v in _invites.items() if name_key(v["name"]) == key]:
            _invites.pop(c, None)  # a new invite for the same name replaces the old one
        code = _new_code()
        _invites[code] = {"name": nm, "expires": now + INVITE_TTL}
        return {"code": code, "name": nm, "expires_in": INVITE_TTL}


def set_pin(did: str, new_pin: str, old_pin: str = "") -> None:
    with _lock:
        rec = _devices.get(did)
        if not rec:
            raise PairError(tr("الجهاز غير موجود", "Device not found"), 404)
        if rec.get("pin"):
            wait = _locked_for(did)
            if wait:
                raise PairError(_wait_msg(wait), 429)
            if not _pin_ok(rec, old_pin):
                _pin_fail(did)
                raise PairError(tr("رمز PIN الحالي غير صحيح", "The current PIN is wrong"), 401)
        err = pin_error(new_pin)
        if err:
            raise PairError(err)
        rec["pin"] = _hash_pin(new_pin)
        _pin_fails.pop(did, None)
        _save()
