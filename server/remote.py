"""Reaching the PC over mobile data with nothing else installed: an encrypted port that the router forwards.

* HTTPS on REMOTE_PORT with a self-signed certificate (tlscert.py); phones pin its fingerprint.
* The router is asked to forward that port (upnp.py: UPnP or NAT-PMP) and tells us its internet address.
  When it cannot (UPnP off), the PC window shows what to forward by hand, and the home internet address is
  read once from a public "what is my address" page so a hand-made forward works too.
* Public IPv6 addresses of the PC are offered too (no forwarding needed when the router lets them in).
* Only phones that are already paired can use it: pairing and PIN reconnects are refused on this port.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import tlscert
import upnp

REMOTE_PORT = 47803
RENEW_SECONDS = 20 * 60
RETRY_SECONDS = 5 * 60
IP_ECHO = ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip")


def public_ip() -> str:
    """The home internet address as seen from outside (only asked when the router does not tell it)."""
    for url in IP_ECHO:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read(64).decode("ascii", "replace").strip()
            if upnp.is_public(ip):
                return ip
        except (OSError, ValueError):
            continue
    return ""


class TLSServer(ThreadingHTTPServer):
    """HTTPS server; the TLS handshake runs in the request thread so a slow client blocks nobody."""

    daemon_threads = True
    remote = True  # the handler refuses pairing / PIN on this server

    def __init__(self, port: int, handler: Any, ctx: ssl.SSLContext):
        self.ctx = ctx
        try:
            self.address_family = socket.AF_INET6
            super().__init__(("::", port), handler)
        except OSError:
            self.address_family = socket.AF_INET
            super().__init__(("0.0.0.0", port), handler)

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # IPv4 and IPv6
            except (OSError, AttributeError):
                pass
        super().server_bind()

    def get_request(self):  # type: ignore[override]
        sock, addr = self.socket.accept()
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def finish_request(self, request: Any, client_address: Any) -> None:
        try:
            request.settimeout(15)
            request.do_handshake()
            request.settimeout(None)
        except (ssl.SSLError, OSError):
            return  # internet scanners, wrong protocol…
        super().finish_request(request, client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # stay quiet about random connections from the internet


class RemoteAccess:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server: TLSServer | None = None
        self._stop = threading.Event()
        self._gw: upnp.Gateway | upnp.NatPmp | None = None
        self._check_lock = threading.Lock()
        self._fingerprint = ""
        self._state: dict[str, Any] = {"state": "off"}
        self.on_state: Any = None  # called with the new state after each router check (the PC window)

    # ------------------------------------------------------------------ control

    def start(self, handler: Any, cert_dir: Path, port: int = REMOTE_PORT, router: bool = True) -> None:
        with self._lock:
            if self._server:
                return
            cert, key, fp = tlscert.ensure(cert_dir)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key)
            self._server = TLSServer(port, handler, ctx)
            self._fingerprint = fp
            self._state = {"state": "starting", "port": self._server.server_address[1]}
            self._stop.clear()
            threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
            if router:
                threading.Thread(target=self._router_loop, daemon=True).start()
            print(f"[remote] encrypted port {self.port} open for mobile data")

    def stop(self) -> None:
        with self._lock:
            srv, self._server = self._server, None
            port = srv.server_address[1] if srv else REMOTE_PORT
            ext = int(self._state.get("ext_port") or port) if self._state.get("mapped") else 0
            self._stop.set()
            gw, self._gw = self._gw, None
            self._state = {"state": "off"}
        if srv:
            srv.shutdown()
            srv.server_close()
        if gw and ext:
            try:
                gw.unmap_port(ext, port)
            except (upnp.UPnPError, OSError):
                pass

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        srv = self._server
        return srv.server_address[1] if srv else REMOTE_PORT

    # ------------------------------------------------------------------ router

    def check_router(self) -> None:
        """Ask the router to forward the port and for its internet address; sets the state."""
        with self._check_lock:
            self._check_router()

    def _check_router(self) -> None:
        ipv6 = upnp.global_ipv6()
        gw = self._gw
        try:
            if gw is None:
                gw = upnp.find_gateway()
            want = int(self._state.get("ext_port") or self.port) if self._state.get("mapped") else self.port
            ext_port = gw.map_port(want, self.port, lease=3600)
            ext = gw.external_ip()
            if upnp.is_public(ext):
                st: dict[str, Any] = {"state": "ok", "public_ip": ext, "ext_port": ext_port}
            else:
                st = {"state": "cgnat", "router_ip": ext}
            st.update(method=gw.method, mapped=True, router=gw.address, local_ip=gw.local_ip)
        except (upnp.UPnPError, OSError, ValueError) as exc:
            gw = None
            gws = upnp.default_gateways()
            ips = upnp.local_ipv4s()
            st = {"state": "no_upnp", "error": str(exc)[:200], "router": gws[0] if gws else "",
                  "local_ip": ips[0] if ips else ""}
            pub = public_ip()
            if pub:  # a port forwarded by hand in the router works with this address
                st.update(public_ip=pub, ext_port=self.port, manual=True)
        changed = False
        with self._lock:
            if self._server:
                self._gw = gw
                old = self._state
                self._state = {**st, "port": self.port, "ipv6": ipv6, "checked": int(time.time())}
                changed = old.get("state") != st["state"] or old.get("public_ip") != st.get("public_ip")
        if changed and self.on_state:
            try:
                self.on_state(dict(self._state))
            except Exception:
                pass

    def check_now(self) -> dict[str, Any]:
        """The app's "check again" button."""
        if self.running:
            self.check_router()
        return self.info()

    def router_help(self) -> dict[str, Any]:
        """For the PC window only (never sent to the phone): where to forward the port by hand."""
        st = self._state
        return {"router": st.get("router", ""), "local_ip": st.get("local_ip", ""), "port": self.port,
                "state": st.get("state", "off"), "error": st.get("error", ""), "method": st.get("method", "")}

    def _router_loop(self) -> None:
        while not self._stop.is_set():
            self.check_router()
            ok = self._state.get("state") == "ok"
            self._stop.wait(RENEW_SECONDS if ok else RETRY_SECONDS)

    # ------------------------------------------------------------------ for the phone

    def urls(self) -> list[str]:
        st = self._state
        out = []
        if st.get("public_ip"):
            out.append(f"https://{st['public_ip']}:{st.get('ext_port') or self.port}")
        out += [f"https://[{ip}]:{self.port}" for ip in st.get("ipv6", [])]
        return out

    def info(self) -> dict[str, Any]:
        """What a paired phone needs: state, certificate fingerprint and the addresses to try."""
        if not self.running:
            return {"enabled": False, "state": "off"}
        st = self._state
        return {"enabled": True, "state": st.get("state", "starting"), "fingerprint": self._fingerprint,
                "urls": self.urls(), "ipv6": bool(st.get("ipv6")), "port": self.port,
                "method": st.get("method", ""), "manual": bool(st.get("manual"))}


REMOTE = RemoteAccess()
