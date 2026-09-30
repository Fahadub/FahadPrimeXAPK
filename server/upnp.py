"""Open a port on the home router automatically, standard library only.

* UPnP IGD: discover() finds the router (asked on every network adapter, and directly at the default
  gateway, since a VPN / virtual adapter often swallows the multicast), map_port() asks it to forward an
  outside port to this PC and external_ip() asks it for its internet address.
* NAT-PMP (RFC 6886): the same with routers that speak it instead (Apple, many open-source firmwares).

If both are off, or the internet provider shares one address between many homes (CGNAT), the phone cannot
reach the PC from outside by itself; the status says why and the PC window shows what to forward by hand.
"""

from __future__ import annotations

import ipaddress
import re
import select
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
WAN_SERVICES = ("WANIPConnection", "WANPPPConnection")


class UPnPError(Exception):
    pass


# the router is on the home network: never go through a proxy set up for the internet
_lan_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


# description pages of common routers, tried when nobody answers the search (it can be blocked)
KNOWN_DESCRIPTIONS = ((5000, "/rootDesc.xml"), (49000, "/igddesc.xml"), (52869, "/picsdesc.xml"),
                      (1900, "/igd.xml"), (49152, "/gatedesc.xml"), (37215, "/desc.xml"))


def local_ipv4s() -> list[str]:
    """This PC's IPv4 addresses (no loopback / link-local)."""
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet is sent; it only picks the outgoing adapter
        ip = s.getsockname()[0]
        s.close()
        if ip in ips:
            ips.remove(ip)
        ips.insert(0, ip)
    except OSError:
        pass
    return ips


def default_gateways() -> list[str]:
    """The router address(es): from the routing table, else the usual x.x.x.1 of each network."""
    out: list[str] = []
    try:
        if sys.platform == "win32":
            text = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True, text=True, timeout=6,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            out += re.findall(r"(?m)^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s", text)
        else:
            with open("/proc/net/route", encoding="ascii") as f:
                for line in f.read().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) > 2 and parts[1] == "00000000":
                        out.append(socket.inet_ntoa(struct.pack("<I", int(parts[2], 16))))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    for ip in local_ipv4s():
        guess = ip.rsplit(".", 1)[0] + ".1"
        if guess != ip:
            out.append(guess)
    uniq: list[str] = []
    for ip in out:
        if ip not in uniq and ip != "0.0.0.0":
            uniq.append(ip)
    return uniq


def discover(timeout: float = 3.0) -> list[str]:
    """Description URLs (LOCATION) of the gateways that answered."""
    found: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        msgs = [("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
                 f"MX: 2\r\nST: {st}\r\n\r\n").encode() for st in SEARCH_TARGETS]
        for ip in local_ipv4s() or [""]:  # the search goes out on every adapter, not only the default one
            try:
                if ip:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                for msg in msgs:
                    sock.sendto(msg, SSDP_ADDR)
            except OSError:
                pass
        for gw in default_gateways()[:3]:  # and straight to the router
            for msg in msgs:
                try:
                    sock.sendto(msg, (gw, 1900))
                except OSError:
                    pass
        end = time.time() + timeout
        while time.time() < end:
            ready, _, _ = select.select([sock], [], [], max(0.0, min(0.5, end - time.time())))
            if not ready:
                continue
            try:
                data, _addr = sock.recvfrom(4096)
            except OSError:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                loc = m.group(1).decode("ascii", "replace")
                if loc not in found:
                    found.append(loc)
    finally:
        sock.close()
    return found


def probe_known(gateways: list[str], timeout: float = 1.5) -> list[str]:
    """Description pages that answer at the router's usual addresses."""
    found: list[str] = []
    for gw in gateways[:2]:
        for port, path in KNOWN_DESCRIPTIONS:
            url = f"http://{gw}:{port}{path}"
            try:
                with _lan_open(url, timeout=timeout) as r:
                    if b"InternetGatewayDevice" in r.read(65536):
                        found.append(url)
            except (urllib.error.URLError, OSError, ValueError):
                continue
    return found


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def find_service(location: str, timeout: float = 5.0) -> tuple[str, str] | None:
    """(control URL, service type) of the router's WAN connection service."""
    with _lan_open(location, timeout=timeout) as r:
        root = ET.fromstring(r.read())
    base = location
    for el in root.iter():
        if _strip_ns(el.tag) == "URLBase" and (el.text or "").strip():
            base = el.text.strip()
    for svc in root.iter():
        if _strip_ns(svc.tag) != "service":
            continue
        fields = {_strip_ns(c.tag): (c.text or "").strip() for c in svc}
        stype = fields.get("serviceType", "")
        if any(w in stype for w in WAN_SERVICES) and fields.get("controlURL"):
            return urllib.parse.urljoin(base, fields["controlURL"]), stype
    return None


def _soap(control: str, stype: str, action: str, args: dict[str, Any], timeout: float = 6.0) -> dict[str, str]:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{stype}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(control, data=envelope, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{stype}#{action}"'})
    try:
        with _lan_open(req, timeout=timeout) as r:
            root = ET.fromstring(r.read())
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        code = re.search(r"<errorCode>(\d+)</errorCode>", text)
        desc = re.search(r"<errorDescription>([^<]*)</errorDescription>", text)
        raise UPnPError(f"{action} failed: {code.group(1) if code else exc.code} "
                        f"{desc.group(1) if desc else ''}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UPnPError(f"{action} failed: {exc}") from exc
    out: dict[str, str] = {}
    for el in root.iter():
        if not list(el) and _strip_ns(el.tag).startswith("New"):
            out[_strip_ns(el.tag)] = (el.text or "").strip()
    return out


def local_ip_towards(url: str) -> str:
    """This PC's address on the network the router is on."""
    host = urllib.parse.urlsplit(url).hostname or "192.168.1.1"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 1900))
        return s.getsockname()[0]
    finally:
        s.close()


class Gateway:
    """A UPnP router."""

    method = "UPnP"

    def __init__(self, location: str):
        svc = find_service(location)
        if not svc:
            raise UPnPError("the router does not offer port forwarding (no WAN service)")
        self.location = location
        self.control, self.stype = svc
        self.local_ip = local_ip_towards(location)
        self.address = urllib.parse.urlsplit(location).hostname or ""

    def external_ip(self) -> str:
        return _soap(self.control, self.stype, "GetExternalIPAddress", {}).get("NewExternalIPAddress", "")

    def _add(self, external: int, internal: int, lease: int, description: str) -> None:
        args = {"NewRemoteHost": "", "NewExternalPort": external, "NewProtocol": "TCP", "NewInternalPort": internal,
                "NewInternalClient": self.local_ip, "NewEnabled": 1, "NewPortMappingDescription": description,
                "NewLeaseDuration": lease}
        try:
            _soap(self.control, self.stype, "AddPortMapping", args)
        except UPnPError as exc:
            if lease and "725" in str(exc):  # OnlyPermanentLeasesSupported
                _soap(self.control, self.stype, "AddPortMapping", {**args, "NewLeaseDuration": 0})
            else:
                raise

    def map_port(self, external: int, internal: int, lease: int = 3600, description: str = "Wi-Fi Remote") -> int:
        """Forward ``external`` (or a nearby free port) to this PC; returns the outside port in use."""
        try:
            self._add(external, internal, lease, description)
            return external
        except UPnPError as exc:
            if "718" not in str(exc):  # ConflictInMappingEntry: the port is forwarded to another device
                raise
        last: UPnPError | None = None  # leave that forward alone (maybe another PC) and use a nearby port
        for alt in range(external + 1, external + 7):
            try:
                self._add(alt, internal, lease, description)
                return alt
            except UPnPError as exc:
                last = exc
        raise last or UPnPError("AddPortMapping failed: 718 ConflictInMappingEntry")

    def unmap_port(self, external: int, internal: int = 0) -> None:
        _soap(self.control, self.stype, "DeletePortMapping",
              {"NewRemoteHost": "", "NewExternalPort": external, "NewProtocol": "TCP"})


class NatPmp:
    """A router speaking NAT-PMP (RFC 6886) on UDP 5351."""

    method = "NAT-PMP"

    def __init__(self, address: str):
        self.address = address
        self.local_ip = local_ip_towards(f"http://{address}/")
        self._public = ""
        self._ask(b"\x00\x00", 12)  # raises when the router does not speak it

    def _ask(self, packet: bytes, size: int) -> bytes:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            wait = 0.25
            for _ in range(3):
                s.sendto(packet, (self.address, 5351))
                s.settimeout(wait)
                try:
                    data, addr = s.recvfrom(64)
                except socket.timeout:
                    wait *= 2
                    continue
                if addr[0] != self.address or len(data) < size or data[1] != packet[1] + 128:
                    continue
                result = struct.unpack("!H", data[2:4])[0]
                if result:
                    raise UPnPError(f"NAT-PMP refused (result {result})")
                if packet[1] == 0:
                    self._public = socket.inet_ntoa(data[8:12])
                return data
        finally:
            s.close()
        raise UPnPError("no answer to NAT-PMP")

    def external_ip(self) -> str:
        self._ask(b"\x00\x00", 12)
        return self._public

    def map_port(self, external: int, internal: int, lease: int = 3600, description: str = "") -> int:
        data = self._ask(struct.pack("!BBHHHI", 0, 2, 0, internal, external, lease), 16)
        return struct.unpack("!H", data[10:12])[0] or external  # the router may pick another outside port

    def unmap_port(self, external: int, internal: int = 0) -> None:
        self._ask(struct.pack("!BBHHHI", 0, 2, 0, internal or external, 0, 0), 16)


def find_gateway(timeout: float = 3.0) -> Gateway | NatPmp:
    """The home router, by UPnP (search, then its usual addresses) or NAT-PMP."""
    errors = []
    gateways = default_gateways()
    locations = discover(timeout)
    if not locations:
        locations = probe_known(gateways)
    for loc in locations:
        try:
            return Gateway(loc)
        except (UPnPError, OSError, ET.ParseError, ValueError) as exc:
            errors.append(str(exc))
    for gw in gateways[:2]:
        try:
            return NatPmp(gw)
        except (UPnPError, OSError) as exc:
            errors.append(f"{gw}: {exc}")
    raise UPnPError("no router answered UPnP or NAT-PMP" + (f" ({'; '.join(errors[:3])})" if errors else ""))


def is_public(ip: str) -> bool:
    """False for private, CGNAT (100.64.0.0/10), link-local and similar addresses."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return a.is_global


def global_ipv6() -> list[str]:
    """This PC's public IPv6 addresses (reachable from a phone with IPv6, when the router lets it in)."""
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            ip = info[4][0].split("%")[0]
            if is_public(ip) and ip not in out:
                out.append(ip)
    except OSError:
        pass
    try:  # the address used for going out, when name lookup does not list it
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        ip = s.getsockname()[0].split("%")[0]
        s.close()
        if is_public(ip) and ip not in out:
            out.append(ip)
    except OSError:
        pass
    return out
