"""GOAT on the phone (2026-10-17, his order): the phone's browser talks to
GOAT over the home Wi-Fi now, and over Tailscale later.

HTTPS is not optional. Phone browsers only give the microphone to secure
pages, so a self-signed certificate is made for every address this laptop
has (LAN, and the 100.x Tailscale one once it exists). The phone accepts
the warning once per certificate. A new address (e.g. Tailscale installed)
triggers a new certificate on the next start.

Nothing is opened to the internet: no router or UPnP changes, and any request
that doesn't come from a private, loopback or Tailscale (100.64/10) address is
refused before it reaches a handler. GOAT controls this whole laptop, so the
page is also locked behind a login token (typed once on the page, or the ?k=
link); after that a Secure HttpOnly cookie carries it.

Everything runs on GOAT's own asyncio loop, and the brain doesn't know the
words came from the phone.
Wire: phone -> one finished utterance per binary message (int16 16kHz mono;
the phone does its own endpointing), or JSON {type: text|stop|live}.
laptop -> JSON {type: you|say|reply|status|turn_done|cancel}; each "say"
with audio is followed by one binary message (int16 16kHz mono).
"""
import asyncio
import datetime
import hmac
import ipaddress
import json
import os
import secrets
import socket
import ssl
import time

import numpy as np

PORT = 8797
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(os.path.dirname(HERE), ".phone")   # gitignored
PAGE = os.path.join(HERE, "phone.html")
COOKIE = "goat_k"
SR = 16000
MIN_SAMPLES = SR // 4
MAX_SAMPLES = SR * 60
_TAILNET = ipaddress.ip_network("100.64.0.0/10")
_TOKEN_ABC = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o/1/l/i: typed on a phone


def allowed_remote(addr) -> bool:
    """Home network, this laptop, or the tailnet. Never the open internet."""
    try:
        ip = ipaddress.ip_address((addr or "").split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip in _TAILNET:
        return True
    if ip.version == 6 and ip in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
        return True   # Tailscale's IPv6 range
    return ip.is_private and not ip.is_unspecified


def local_ips() -> list:
    """Every IPv4 address of this laptop that a phone could reach."""
    found = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # picks the route; no packet sent
        found.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for a in found:
        ip = ipaddress.ip_address(a)
        if a not in out and not ip.is_loopback and not ip.is_link_local \
                and allowed_remote(a):
            out.append(a)
    return out


def load_token() -> str:
    path = os.path.join(DIR, "token.txt")
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        if len(t) >= 10:
            return t
    except OSError:
        pass
    t = "".join(secrets.choice(_TOKEN_ABC) for _ in range(10))
    os.makedirs(DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(t)
    return t


def ensure_cert(ips: list) -> tuple:
    """(cert, key) paths, valid for every address in `ips`."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    crt, key = os.path.join(DIR, "cert.pem"), os.path.join(DIR, "key.pem")
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with open(crt, "rb") as f:
            c = x509.load_pem_x509_certificate(f.read())
        have = c.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
        until = getattr(c, "not_valid_after_utc", None) or \
            c.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        if os.path.exists(key) and until > now + datetime.timedelta(days=30) \
                and all(ipaddress.ip_address(a) in have for a in ips):
            return crt, key
    except Exception:  # noqa: BLE001 — missing/odd cert: make a fresh one
        pass
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GOAT phone")])
    host = socket.gethostname().lower()
    sans = [x509.DNSName("localhost"), x509.DNSName(host),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    sans += [x509.IPAddress(ipaddress.ip_address(a)) for a in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(k.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=800))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .sign(k, hashes.SHA256()))
    os.makedirs(DIR, exist_ok=True)
    with open(key, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))
    with open(crt, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return crt, key


_LOGIN = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GOAT</title><style>
html,body{margin:0;height:100%;background:#0b0f14;color:#e6edf3;
font:16px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{display:flex;align-items:center;justify-content:center}
form{display:flex;flex-direction:column;gap:12px;width:80%;max-width:320px}
h1{margin:0 0 8px;letter-spacing:.12em;font-size:22px;text-align:center}
input{background:#161b22;border:1px solid #30363d;color:inherit;
border-radius:12px;padding:14px;font-size:20px;text-align:center;
letter-spacing:.15em}
button{background:#1f6feb;border:none;color:#fff;border-radius:12px;
padding:14px;font-size:17px}
p{color:#f85149;text-align:center;min-height:20px;margin:0}
</style></head><body><form method="post" action="/login">
<h1>GOAT</h1><input name="k" placeholder="login token" autocomplete="off"
autocapitalize="off" autocorrect="off" spellcheck="false" autofocus>
<button>Log in</button><p>%MSG%</p></form></body></html>"""


class PhoneServer:
    def __init__(self, app, port: int = PORT):
        self.app = app
        self.port = port
        self.loop = None
        self.token = ""
        self.urls = []
        self.clients = {}      # ws -> {"q": asyncio.Queue, "live": bool}
        self._fails = {}       # ip -> [count, first_fail_time]
        self._runner = None

    # ---- lifecycle ----
    async def start(self) -> list:
        from aiohttp import web
        self.loop = asyncio.get_running_loop()
        ips = local_ips()
        self.token = load_token()
        crt, key = await asyncio.to_thread(ensure_cert, ips)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(crt, key)
        wapp = web.Application(client_max_size=64 * 1024,
                               middlewares=[self._gate])
        wapp.router.add_get("/", self._page)
        wapp.router.add_post("/login", self._login)
        wapp.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(wapp, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port,
                          ssl_context=ctx).start()
        self.urls = [f"https://{a}:{self.port}" for a in ips]
        self._hook()
        try:
            with open(os.path.join(DIR, "url.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(self.urls) + f"\ntoken: {self.token}\n")
        except OSError:
            pass
        print(f"[phone] ready on {', '.join(self.urls) or 'no LAN address'}")
        return self.urls

    async def stop(self):
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- http ----
    def _authed(self, request) -> bool:
        c = request.cookies.get(COOKIE, "")
        return bool(c) and hmac.compare_digest(c.encode(), self.token.encode())

    def _logged_in(self):
        from aiohttp import web
        r = web.Response(status=303, headers={"Location": "/"})
        r.set_cookie(COOKIE, self.token, max_age=400 * 86400, secure=True,
                     httponly=True, samesite="Strict")
        return r

    @property
    def _gate(self):
        from aiohttp import web

        @web.middleware
        async def gate(request, handler):
            if not allowed_remote(request.remote):
                return web.Response(status=403, text="home network only")
            return await handler(request)
        return gate

    async def _page(self, request):
        from aiohttp import web
        k = request.query.get("k", "")
        if k and hmac.compare_digest(k.encode(), self.token.encode()):
            return self._logged_in()
        if not self._authed(request):
            return web.Response(text=_LOGIN.replace("%MSG%", ""),
                                content_type="text/html")
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-store"})

    async def _login(self, request):
        from aiohttp import web
        ip = request.remote or "?"
        n, t0 = self._fails.get(ip, (0, 0.0))
        if n >= 5 and time.monotonic() - t0 < 60:
            return web.Response(text=_LOGIN.replace(
                "%MSG%", "Too many tries. Wait a minute."),
                content_type="text/html", status=429)
        form = await request.post()
        k = str(form.get("k", "")).strip().lower()
        if k and hmac.compare_digest(k.encode(), self.token.encode()):
            self._fails.pop(ip, None)
            return self._logged_in()
        if n >= 5:
            n, t0 = 0, 0.0
        self._fails[ip] = (n + 1, t0 or time.monotonic())
        return web.Response(text=_LOGIN.replace("%MSG%", "Wrong token."),
                            content_type="text/html", status=401)

    async def _ws(self, request):
        from aiohttp import WSMsgType, web
        if not self._authed(request):
            return web.Response(status=403)
        origin = request.headers.get("Origin", "")
        if origin and origin.split("://", 1)[-1] != request.host:
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_SAMPLES * 2 + 64)
        await ws.prepare(request)
        st = {"q": asyncio.Queue(maxsize=400), "live": False}
        self.clients[ws] = st
        writer = asyncio.create_task(self._writer(ws, st["q"]))
        st["q"].put_nowait(json.dumps({"type": "status", "text": "connected"}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self._utterance(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    self._command(st, msg.data)
        finally:
            self.clients.pop(ws, None)
            writer.cancel()
            self._sync_live()
        return ws

    async def _writer(self, ws, q):
        try:
            while True:
                item = await q.get()
                if isinstance(item, bytes):
                    await ws.send_bytes(item)
                else:
                    await ws.send_str(item)
        except (asyncio.CancelledError, ConnectionError, RuntimeError):
            pass

    # ---- phone -> GOAT ----
    def _utterance(self, data: bytes):
        n = len(data) // 2
        if n < MIN_SAMPLES or n > MAX_SAMPLES:
            return
        audio = np.frombuffer(data[:n * 2], dtype="<i2").astype(np.float32) / 32768.0
        try:
            self.app._lat_start()
        except Exception:  # noqa: BLE001 — the ledger is optional
            pass
        asyncio.create_task(self.app._handle_utterance(audio, None, None))

    def _command(self, st, raw: str):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        kind = m.get("type")
        if kind == "text":
            text = str(m.get("text", "")).strip()[:4000]
            if text:
                self.app.submit_text(text)
        elif kind == "stop":
            self.app.tts.cancel()
            self.app._say_buf = ""
        elif kind == "live":
            st["live"] = bool(m.get("on"))
            self._sync_live()

    def _sync_live(self):
        """While the phone's mic is live the laptop stops listening and goes
        silent; otherwise its mic hears him twice and its speaker barges
        into the phone's turn."""
        live = any(c["live"] for c in self.clients.values())
        self.app.phone_live = live
        self.app.tts.local_mute = live

    # ---- GOAT -> phone ----
    def _post(self, items):
        for st in list(self.clients.values()):
            for item in items:
                try:
                    st["q"].put_nowait(item)
                except asyncio.QueueFull:
                    break

    def _send(self, *items):
        if self.clients and self.loop is not None:
            self.loop.call_soon_threadsafe(self._post, items)

    def _hook(self):
        orig = self.app.emit

        def emit(kind, *args, **kw):
            orig(kind, *args, **kw)
            if not self.clients:
                return
            try:
                data = args[0] if args else ""
                if kind == "you":
                    self._send(json.dumps({"type": "you", "text": str(data)}))
                elif kind == "delta" and not data:
                    self._send(json.dumps({"type": "reply"}))
                elif kind == "turn_done":
                    self._send(json.dumps({"type": "turn_done"}))
                elif kind == "status":
                    self._send(json.dumps({"type": "status",
                                           "text": str(data)[:140]}))
            except Exception:  # noqa: BLE001 — the phone must never break GOAT
                pass

        self.app.emit = emit
        self.app.tts.on_sentence = self._on_sentence
        self.app.tts.on_cancel = lambda: self._send(json.dumps({"type": "cancel"}))

    def _on_sentence(self, text: str, samples):
        if not self.clients:
            return
        items = [json.dumps({"type": "say", "text": text,
                             "audio": samples is not None and len(samples) > 0})]
        if samples is not None and len(samples):
            items.append((np.clip(samples, -1.0, 1.0) * 32767)
                         .astype("<i2").tobytes())
        self._send(*items)
