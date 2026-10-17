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

The page also mirrors the laptop's work panel (2026-10-17, his order: "see
as much as possible of the GOAT processes"): brain/effort per turn, the live
work steps and thinking, context fill, plan usage, and every few seconds the
laptop's vitals. A phone that connects mid-task gets a snapshot first.
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
from collections import deque

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
ROOT = os.path.dirname(HERE)
SHOT = os.path.join(HERE, "live-shot.png")       # screen_tools.SHOT_PATH
VITALS_S = 3.0
_SKIP_TITLES = {"Program Manager", "Windows Input Experience", "GOAT",
                "GOAT live"}


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


def _read_plan():
    """Last plan-usage reading GOAT saved (goat_app._pull_plan_usage)."""
    try:
        with open(os.path.join(ROOT, ".plan_usage.json"), encoding="utf-8") as f:
            d = json.load(f)
        return {"fh": float(d.get("five_hour") or 0),
                "wk": float(d.get("seven_day") or 0),
                "reset": d.get("five_hour_resets") or ""}
    except (OSError, ValueError, TypeError):
        return None


def grab_screen(max_w: int = 1280) -> bytes:
    """The laptop's main screen as a small JPEG."""
    import io

    from PIL import ImageGrab
    img = ImageGrab.grab().convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=68)
    return buf.getvalue()


_VK = {"volup": 0xAF, "voldown": 0xAE, "mute": 0xAD, "playpause": 0xB3,
       "next": 0xB0, "prev": 0xB1, "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
       "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "back": 0x08,
       "space": 0x20, "pgup": 0x21, "pgdn": 0x22}


def _key(vk: int, times: int = 1):
    import ctypes
    u = ctypes.windll.user32
    for _ in range(times):
        u.keybd_event(vk, 0, 0, 0)
        u.keybd_event(vk, 0, 2, 0)


def do_action(d: dict) -> str:
    """Run one phone-remote action on the laptop."""
    import ctypes
    u = ctypes.windll.user32
    a = str(d.get("a", ""))
    if a in _VK:
        _key(_VK[a], 5 if a in ("volup", "voldown") else 1)
        return a
    if a == "lock":
        u.LockWorkStation()
        return "locked"
    if a in ("click", "dclick", "rclick"):
        w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        x = int(max(0.0, min(1.0, float(d["x"]))) * (w - 1))
        y = int(max(0.0, min(1.0, float(d["y"]))) * (h - 1))
        u.SetCursorPos(x, y)
        dn, up = (0x8, 0x10) if a == "rclick" else (0x2, 0x4)
        for _ in range(2 if a == "dclick" else 1):
            u.mouse_event(dn, 0, 0, 0, 0)
            u.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.05)
        return f"{a} {x},{y}"
    if a == "scroll":
        u.mouse_event(0x800, 0, 0, int(d.get("dy", 0)) * 120, 0)
        return "scroll"
    if a == "type":
        import subprocess
        txt = str(d.get("text", ""))[:500]
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Set-Clipboard -Value $env:GT"],
                       env={**os.environ, "GT": txt}, timeout=10,
                       creationflags=0x08000000)
        u.keybd_event(0x11, 0, 0, 0)
        u.keybd_event(0x56, 0, 0, 0)
        u.keybd_event(0x56, 0, 2, 0)
        u.keybd_event(0x11, 0, 2, 0)
        return "typed"
    raise ValueError("unknown action")


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
        self._vitals_task = None
        self._names = {}       # pid -> process name, for the window list
        self._think_buf = ""
        self._think_due = False
        self.booted = time.time()
        # Kept current even with no phone connected, so one that connects
        # mid-task catches up from the snapshot.
        self.state = {"brain": None, "model": None, "effort": None,
                      "task": None, "ctx": None, "plan": _read_plan(),
                      "usage": None, "claude": "ok", "latency": None,
                      "shot": 0, "vitals": None}
        self.done_tasks = deque(maxlen=8)

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
        wapp.router.add_get("/shot", self._shot)
        wapp.router.add_get("/screen", self._screen)
        wapp.router.add_post("/act", self._act)
        self._runner = web.AppRunner(wapp, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port,
                          ssl_context=ctx).start()
        self.urls = [f"https://{a}:{self.port}" for a in ips]
        self._hook()
        self._vitals_task = asyncio.create_task(self._vitals_loop())
        try:
            with open(os.path.join(DIR, "url.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(self.urls) + f"\ntoken: {self.token}\n")
        except OSError:
            pass
        print(f"[phone] ready on {', '.join(self.urls) or 'no LAN address'}")
        return self.urls

    async def stop(self):
        if self._vitals_task is not None:
            self._vitals_task.cancel()
            self._vitals_task = None
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
        st["q"].put_nowait(json.dumps(self._snapshot()))
        try:
            v = await asyncio.to_thread(self._vitals)
            self.state["vitals"] = v
            st["q"].put_nowait(json.dumps({
                "type": "vitals", "now": time.time(),
                "busy": bool(getattr(self.app, "busy", False)), **v}))
        except Exception:  # noqa: BLE001
            pass
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

    async def _shot(self, request):
        """The last screenshot the brain looked at (screen_tools)."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=403)
        if not os.path.exists(SHOT):
            return web.Response(status=404)
        return web.FileResponse(SHOT, headers={"Cache-Control": "no-store"})

    async def _screen(self, request):
        """A fresh look at the laptop's screen, shrunk for a phone line."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=403)
        try:
            data = await asyncio.to_thread(grab_screen)
        except Exception as e:  # noqa: BLE001
            return web.Response(status=500, text=f"capture failed: {e}")
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def _act(self, request):
        """Remote control: quick actions, tap-to-click, typing."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=403)
        try:
            d = await request.json()
            r = await asyncio.to_thread(do_action, d)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "err": str(e)})
        return web.json_response({"ok": True, "msg": r})

    def _snapshot(self) -> dict:
        hist = []
        for pair in list(getattr(self.app, "_exchanges", None) or []):
            try:
                u, r = str(pair[0]), str(pair[1])
            except (TypeError, IndexError):
                continue
            if u and not u.startswith("["):
                hist.append([u[:300], r[:300]])
        return {"type": "snapshot", "now": time.time(), "booted": self.booted,
                "state": self.state, "done": list(self.done_tasks),
                "history": hist,
                "busy": bool(getattr(self.app, "busy", False))}

    # ---- vitals ----
    async def _vitals_loop(self):
        while True:
            await asyncio.sleep(VITALS_S)
            if not self.clients:
                continue
            try:
                v = await asyncio.to_thread(self._vitals)
            except Exception:  # noqa: BLE001 — the phone must never break GOAT
                continue
            self.state["vitals"] = v
            self._post([json.dumps({"type": "vitals", "now": time.time(),
                                    "busy": bool(getattr(self.app, "busy", False)),
                                    **v})])

    def _vitals(self) -> dict:
        import psutil
        out = {"cpu": psutil.cpu_percent(None)}
        vm = psutil.virtual_memory()
        out["ram"] = vm.percent
        out["ram_gb"] = [round(vm.used / 2**30, 1), round(vm.total / 2**30, 1)]
        bat = psutil.sensors_battery()
        if bat is not None:
            out["bat"] = round(bat.percent)
            out["plugged"] = bool(bat.power_plugged)
        try:
            out["disk_free_gb"] = round(psutil.disk_usage("C:\\").free / 2**30)
        except OSError:
            pass
        try:
            out["goat_mb"] = psutil.Process(os.getpid()).memory_info().rss // 2**20
        except psutil.Error:
            pass
        try:
            import live_view
            out["top"] = [[n, round(c, 1), m] for c, m, n in
                          list(live_view._rows_cache)[:6]]
        except Exception:  # noqa: BLE001
            pass
        try:
            import screen_hands
            wins = []
            for w in screen_hands.windows():
                if w["pid"] == os.getpid() or w["title"] in _SKIP_TITLES:
                    continue
                pid = w["pid"]
                if pid not in self._names:
                    try:
                        self._names[pid] = psutil.Process(pid).name() \
                            .removesuffix(".exe")
                    except psutil.Error:
                        self._names[pid] = "?"
                wins.append([w["title"][:80], self._names[pid],
                             bool(w.get("minimized"))])
                if len(wins) >= 12:
                    break
            out["windows"] = wins
        except Exception:  # noqa: BLE001
            pass
        return out

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
        elif kind == "cut":
            # The phone's Stop button: end the running turn, like talking
            # over it on the laptop.
            self.app.tts.cancel()
            self.app._say_buf = ""
            cut = getattr(self.app, "_cut_turn", None)
            if cut is not None and getattr(self.app, "busy", False):
                asyncio.create_task(cut(None))

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
            data = args[0] if args else ""
            try:
                self._mirror(kind, str(data))
            except Exception:  # noqa: BLE001 — the phone must never break GOAT
                pass
            if not self.clients:
                return
            try:
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

    def _mirror(self, kind: str, data: str):
        """The laptop's work panel (ui_qt on_event), as phone messages."""
        st = self.state
        now = time.time()
        msg = None
        if kind == "brain":
            p = (data.split("|") + ["", "", ""])[:3]
            st["brain"] = p
            msg = {"type": "brain", "model": p[0], "tier": p[1], "effort": p[2]}
        elif kind == "model":
            st["model"] = data
            msg = {"type": "model", "name": data}
        elif kind == "effort":
            st["effort"] = data
            msg = {"type": "effort", "level": data}
        elif kind == "work_start":
            model, _, task = data.partition("|")
            self._think_buf = ""
            st["task"] = {"model": model.strip(), "text": task[:600], "t": now,
                          "steps": [], "think": "", "state": "run",
                          "brain": st["brain"]}
            msg = {"type": "work", "ev": "start", "task": st["task"]}
        elif kind in ("work_tool", "work_step", "work_add"):
            step = [data[:240], now]
            if st["task"] is not None:
                st["task"]["steps"] = (st["task"]["steps"] + [step])[-40:]
            msg = {"type": "work", "ev": "step", "step": step}
        elif kind == "work_think":
            if st["task"] is not None:
                st["task"]["think"] = (st["task"]["think"] + data)[-2000:]
            if self.clients and self.loop is not None:
                self._think_buf += data
                if not self._think_due:
                    self._think_due = True
                    self.loop.call_soon_threadsafe(
                        self.loop.call_later, 0.4, self._flush_think)
        elif kind in ("work_done", "work_fail"):
            t = st["task"]
            if t is not None and t["state"] == "run":
                t["state"] = "fail" if kind == "work_fail" else "done"
                t["end"] = now
                if kind == "work_fail":
                    t["fail"] = data[:200]
                self.done_tasks.appendleft(
                    {k: t.get(k) for k in ("text", "model", "t", "end",
                                           "state", "fail")}
                    | {"steps": len(t["steps"])})
            msg = {"type": "work", "ev": "fail" if kind == "work_fail"
                   else "done", "text": data[:200], "t": now}
        elif kind == "work_ctx":
            used, _, trim = data.partition("|")
            st["ctx"] = [int(used or 0), int(trim or 0)]
            msg = {"type": "ctx", "used": st["ctx"][0], "trim": st["ctx"][1]}
        elif kind == "plan":
            fh, wk, rs = (data.split("|") + ["", "", ""])[:3]
            st["plan"] = {"fh": float(fh or 0), "wk": float(wk or 0), "reset": rs}
            msg = {"type": "plan", **st["plan"]}
        elif kind == "usage":
            a, _, b = data.partition("|")
            st["usage"] = [int(a or 0), int(b or 0)]
            msg = {"type": "usage", "in": st["usage"][0], "out": st["usage"][1]}
        elif kind == "claude":
            st["claude"] = data
            msg = {"type": "claude", "state": data}
        elif kind == "limit":
            msg = {"type": "notice", "text": data[:300]}
        elif kind == "latency":
            st["latency"] = data
            msg = {"type": "latency", "ms": data}
        elif kind == "work_shot":
            st["shot"] += 1
            msg = {"type": "shot", "v": st["shot"]}
        if msg is not None and self.clients:
            if kind in ("work_done", "work_fail"):
                self._flush_think()
            self._send(json.dumps(msg))

    def _flush_think(self):
        self._think_due = False
        text, self._think_buf = self._think_buf, ""
        if text:
            self._send(json.dumps({"type": "work", "ev": "think", "text": text}))

    def _on_sentence(self, text: str, samples):
        if not self.clients:
            return
        items = [json.dumps({"type": "say", "text": text,
                             "audio": samples is not None and len(samples) > 0})]
        if samples is not None and len(samples):
            items.append((np.clip(samples, -1.0, 1.0) * 32767)
                         .astype("<i2").tobytes())
        self._send(*items)
