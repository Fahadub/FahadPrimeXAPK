"""A self-signed TLS certificate made with the Python standard library only (no OpenSSL tools, no packages).

The internet port uses it for encryption. Phones do not trust it through a certificate authority: they
learn its SHA-256 fingerprint while paired on the Wi-Fi and accept only that exact certificate (pinning).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import math
import os
import secrets
from pathlib import Path

_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % d for d in range(3, int(math.isqrt(p)) + 1, 2))]


# --------------------------------------------------------------------------- RSA

def _probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, e: int) -> int:
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1  # top two bits: n has full length
        if (c - 1) % e and _probable_prime(c):
            return c


def rsa_key(bits: int = 2048, e: int = 65537) -> dict[str, int]:
    while True:
        p, q = _prime(bits // 2, e), _prime(bits // 2, e)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        d = pow(e, -1, (p - 1) * (q - 1))
        return {"n": n, "e": e, "d": d, "p": p, "q": q, "dp": d % (p - 1), "dq": d % (q - 1), "qi": pow(q, -1, p)}


def _sign_sha256(key: dict[str, int], data: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256."""
    k = (key["n"].bit_length() + 7) // 8
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(data).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    m = int.from_bytes(em, "big")
    # CRT
    s1 = pow(m, key["dp"], key["p"])
    s2 = pow(m, key["dq"], key["q"])
    h = (key["qi"] * (s1 - s2)) % key["p"]
    s = s2 + h * key["q"]
    return s.to_bytes(k, "big")


# --------------------------------------------------------------------------- DER

def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _int(v: int) -> bytes:
    b = v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big")  # leading 0 keeps it positive
    return _tlv(0x02, b)


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


_NULL = b"\x05\x00"
_SHA256_RSA = _seq(_oid("1.2.840.113549.1.1.11"), _NULL)
_RSA = _seq(_oid("1.2.840.113549.1.1.1"), _NULL)


def _name(cn: str) -> bytes:
    return _seq(_tlv(0x31, _seq(_oid("2.5.4.3"), _tlv(0x0C, cn.encode("utf-8")))))


def _time(t: datetime.datetime) -> bytes:
    if t.year < 2050:
        return _tlv(0x17, t.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, t.strftime("%Y%m%d%H%M%SZ").encode())


def self_signed(key: dict[str, int], common_name: str = "Wi-Fi Remote", days: int = 3650) -> bytes:
    """DER certificate (X.509 v3, no extensions)."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    spki = _seq(_RSA, _tlv(0x03, b"\x00" + _seq(_int(key["n"]), _int(key["e"]))))
    tbs = _seq(
        _tlv(0xA0, _int(2)),                            # version 3
        _int(secrets.randbits(63) | 1),                 # serial
        _SHA256_RSA,
        _name(common_name),
        _seq(_time(now - datetime.timedelta(days=1)), _time(now + datetime.timedelta(days=days))),
        _name(common_name),
        spki,
    )
    return _seq(tbs, _SHA256_RSA, _tlv(0x03, b"\x00" + _sign_sha256(key, tbs)))


def private_key_der(key: dict[str, int]) -> bytes:
    """PKCS#1 RSAPrivateKey."""
    return _seq(_int(0), *(_int(key[k]) for k in ("n", "e", "d", "p", "q", "dp", "dq", "qi")))


def _pem(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode()
    return f"-----BEGIN {label}-----\n" + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64)) + \
        f"\n-----END {label}-----\n"


def fingerprint(cert_der: bytes) -> str:
    return hashlib.sha256(cert_der).hexdigest()


def ensure(directory: Path, common_name: str = "Wi-Fi Remote") -> tuple[Path, Path, str]:
    """(cert.pem, key.pem, sha256 fingerprint); made once and kept, so paired phones keep trusting it."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "remote_cert.pem", directory / "remote_key.pem"
    if cert_path.exists() and key_path.exists():
        pem = cert_path.read_text(encoding="ascii")
        der = base64.b64decode("".join(line for line in pem.splitlines() if "-----" not in line))
        return cert_path, key_path, fingerprint(der)
    key = rsa_key()
    der = self_signed(key, common_name)
    tmp = key_path.with_suffix(".tmp")
    tmp.write_text(_pem("RSA PRIVATE KEY", private_key_der(key)), encoding="ascii")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(key_path)
    cert_path.write_text(_pem("CERTIFICATE", der), encoding="ascii")
    return cert_path, key_path, fingerprint(der)
