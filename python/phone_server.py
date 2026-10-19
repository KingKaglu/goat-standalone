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
import base64
import threading
import datetime
import hmac
import ipaddress
import json
import os
import sys
import secrets
import socket
import subprocess
import ssl
import time
from collections import deque

import numpy as np

PORT = 8797
HERE = os.path.dirname(os.path.abspath(__file__))
ECHO_TAIL_S = 0.8   # room echo after my voice ends; clips recorded inside it are me
# Android calls ANY playback "music active", my own voice included, and the
# panel's audio context runs ~1.5 s past my last word (parkCtx). A bubble clip
# starting inside this window after my voice is not "another app playing".
SELF_AUDIO_S = 3.0
# A phone transcript is my own voice coming back (bug 2026-10-18 14:1x: the
# panel mic sent "Checking what the panel does to audio" as his line) when
# it repeats my words from this window, in order (see is_echo).
ECHO_WINDOW_S = 20.0
ECHO_RUN = 4
ECHO_SHARE = 0.6
STREAM_TTL_S = 30.0   # a stream with no chunk and no clip for this long is dead
BARGE_MARK_S = 40.0   # a paused page's next clip (<=30 s of him) is the cut-in
BARGE_HEAR_S = 2.5    # paused for him and the streaming ear has no word yet: carry on
BOOT_GRACE_S = 12   # after a restart, wait this long for the phone to return (was 90: left the laptop mute when the phone was off)
DIR = os.path.join(os.path.dirname(HERE), ".phone")   # gitignored
PAGE = os.path.join(HERE, "phone.html")
COOKIE = "goat_k"
SR = 16000
MIN_SAMPLES = SR // 4
MAX_SAMPLES = SR * 60
_TAILNET = ipaddress.ip_network("100.64.0.0/10")
# Fallback for goat_app.WAKE_RE (tests run without goat_app loaded).
import re as _re
_WAKE = _re.compile(r"\b(goat|goats|goad|goot|gote|coat|გოატ|გოუთ|გოთ)\b", _re.I)
# What "stop talking" sounds like when it's all that's left of a clip once my
# own words are taken out (stop_inside_echo).
_STOP_LEFT = _re.compile(
    r"\b(?:stop|wait|enough|hold on|shut up|be quiet|quiet|pause|cancel)\b|"
    r"\bno no\b|გაჩერდი|მოიცა|გაჩუმდი|კმარა|შეწყვიტე", _re.I)
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
            t = f.read().strip().lower()
        if len(t) >= 4:    # he picks his own (2026-10-17: "kaglu")
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


FW_RULE = "GOAT phone 8797"


def ensure_firewall(port: int):
    """Windows blocks inbound on Private networks (Tailscale counts as one)
    unless a rule allows the port. Make sure that rule exists; if not, ask
    once for admin and create it. Persistent, so this is normally a no-op."""
    if os.name != "nt":
        return
    try:
        ok = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={FW_RULE}"],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000).returncode == 0
        if ok:
            return
        cmd = (f"New-NetFirewallRule -DisplayName '{FW_RULE}' -Direction Inbound "
               f"-Protocol TCP -LocalPort {port} -Action Allow -Profile Any "
               "-RemoteAddress 192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,100.64.0.0/10")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait "
                        f"-ArgumentList '-NoProfile','-Command',\"{cmd}\""],
                       timeout=120, creationflags=0x08000000)
        print("[phone] firewall rule created")
    except Exception as e:  # noqa: BLE001
        print(f"[phone] firewall check failed: {e}")


TS_EXE = r"C:\Program Files\Tailscale\tailscale.exe"
TS_RECHECK_S = 12 * 3600      # renew well before Let's Encrypt's 90 days


def _cert_fresh(crt: str, key: str, name: str, days: int = 30) -> bool:
    """True if `crt` names `name` and is good for at least `days` more."""
    from cryptography import x509
    try:
        with open(crt, "rb") as f:
            c = x509.load_pem_x509_certificate(f.read())
        names = c.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        until = getattr(c, "not_valid_after_utc", None) or \
            c.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return os.path.exists(key) and name in names and \
            until > now + datetime.timedelta(days=days)
    except Exception:  # noqa: BLE001 — missing/odd file = not fresh
        return False


def ts_cert():
    """(name, crt, key): a real Let's Encrypt certificate for this laptop's
    Tailscale name, so Chrome sees a proper secure site (installable app,
    service worker). A self-signed cert only ever gets "Add to Home screen".
    None until HTTPS Certificates are on for the tailnet (admin > DNS)."""
    import shutil
    import subprocess
    exe = TS_EXE if os.path.exists(TS_EXE) else shutil.which("tailscale")
    if not exe:
        return None
    nowin = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True,
                           timeout=15, creationflags=nowin)
        st = json.loads(r.stdout or b"{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    name = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    domains = [d.rstrip(".").lower() for d in (st.get("CertDomains") or [])]
    if not name or name not in domains:
        return None
    crt, key = os.path.join(DIR, "ts.crt"), os.path.join(DIR, "ts.key")
    if not _cert_fresh(crt, key, name):
        os.makedirs(DIR, exist_ok=True)
        try:
            r = subprocess.run([exe, "cert", "--cert-file", crt,
                                "--key-file", key, name], capture_output=True,
                               timeout=180, creationflags=nowin)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[phone] tailscale cert failed: {e}")
            return None
        if not _cert_fresh(crt, key, name, days=1):
            msg = (r.stderr or r.stdout or b"").decode(errors="replace").strip()
            print(f"[phone] tailscale cert failed: {msg[:200]}")
            return None
    return name, crt, key


def icon_png(size: int, maskable: bool = False) -> bytes:
    """The orb icon as PNG. Android wants 192/512 PNGs to build a real app;
    the maskable one is full-bleed with the orb inside the 80% safe zone."""
    import io
    from PIL import Image, ImageDraw
    k = 4                                  # draw big, shrink = smooth edges
    s = size * k
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    bg = (14, 14, 15, 255)
    if maskable:
        d.rectangle((0, 0, s, s), fill=bg)
    else:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.215), fill=bg)
    c, f = s / 2, s / 512
    r1, w = 150 * f, 28 * f
    d.ellipse((c - r1 - w / 2, c - r1 - w / 2, c + r1 + w / 2, c + r1 + w / 2),
              outline=(240, 180, 41, 255), width=int(w))
    r2 = 70 * f
    d.ellipse((c - r2, c - r2, c + r2, c + r2), fill=(236, 234, 228, 255))
    out = io.BytesIO()
    im.resize((size, size), Image.LANCZOS).save(out, "PNG")
    return out.getvalue()


_SW = """// GOAT phone service worker: makes the page an installable app and shows
// a calm note instead of Chrome's dino when the laptop is out of reach.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("push", e => {
  let d = {title: "GOAT", body: ""};
  try { d = e.data.json(); } catch (x) {}
  e.waitUntil(self.registration.showNotification(d.title || "GOAT", {
    body: d.body || "", tag: d.tag || "goat", icon: "/icon-192.png",
    badge: "/icon-192.png", vibrate: [120, 60, 120], renotify: true}));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({type: "window"}).then(cs =>
    cs.length ? cs[0].focus() : self.clients.openWindow("/")));
});
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<body style="margin:0;background:#0b0f14;color:#c9d1d9;font:16px system-ui;' +
    'display:grid;place-items:center;height:100vh;text-align:center">' +
    '<div>GOAT is out of reach.<br><small style="color:#8b949e">Check Wi-Fi or ' +
    'Tailscale, then pull down to retry.</small></div>',
    {headers: {"Content-Type": "text/html; charset=utf-8"}})));
});
"""


def _read_pref() -> str:
    try:
        with open(os.path.join(DIR, "voice.json"), encoding="utf-8") as f:
            v = json.load(f).get("speaks", "auto")
        return v if v in ("auto", "phone", "laptop") else "auto"
    except (OSError, ValueError, AttributeError):
        return "auto"


def _write_pref(v: str):
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(os.path.join(DIR, "voice.json"), "w", encoding="utf-8") as f:
            json.dump({"speaks": v}, f)
    except OSError:
        pass


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


def grab_screen(max_w: int = 1280, quality: int = 68,
                cursor: bool = True) -> bytes:
    """The laptop's main screen as a small JPEG, with the mouse pointer drawn
    in (a screen grab leaves it out, and a remote is lost without it)."""
    import ctypes
    import io

    from PIL import ImageDraw, ImageGrab
    img = ImageGrab.grab().convert("RGB")
    sw, sh = img.size
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)))
    if cursor:
        pt = (ctypes.c_long * 2)()
        if ctypes.windll.user32.GetCursorPos(pt) and 0 <= pt[0] < sw                 and 0 <= pt[1] < sh:
            k = img.width / sw
            x, y = pt[0] * k, pt[1] * k
            s = max(1.0, img.width / 1280)
            arrow = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10.5, 18.8),
                     (7.6, 12), (13, 12)]
            ImageDraw.Draw(img).polygon(
                [(x + ax * s, y + ay * s) for ax, ay in arrow],
                fill=(255, 255, 255), outline=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(30, min(90, int(quality))))
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


_MEDIA = {"volup", "voldown", "mute", "playpause", "next", "prev"}
_NAV = {"enter": "enter", "esc": "esc", "tab": "tab", "left": "left",
        "up": "up", "right": "right", "down": "down", "back": "backspace",
        "space": "space", "pgup": "pageup", "pgdn": "pagedown"}


def do_action(d: dict):
    """Run one phone-remote action on the laptop. Returns a short message,
    or a dict (message under "msg") when the phone needs fresh state."""
    import ctypes

    import phone_ctl
    u = ctypes.windll.user32
    a = str(d.get("a", ""))
    if a == "vol":
        return {"msg": "volume", **phone_ctl.set_volume(d.get("v", 50))}
    if a == "mute":
        try:
            return {"msg": "mute", **phone_ctl.set_mute(d.get("on"))}
        except Exception:  # noqa: BLE001 — no endpoint: the media key still works
            _key(_VK["mute"])
            return "mute"
    if a in _MEDIA:
        _key(_VK[a], 5 if a in ("volup", "voldown") else 1)
        if a in ("volup", "voldown"):
            return {"msg": a, **phone_ctl.volume()}
        return a
    if a == "bright":
        return {"msg": "brightness", "bright": phone_ctl.set_brightness(d.get("v", 50))}
    if a in _NAV:
        return phone_ctl.chord(_NAV[a])
    if a == "key":
        return phone_ctl.chord(d.get("k", ""))
    if a == "focus":
        return phone_ctl.focus(d["h"])
    if a == "min":
        return phone_ctl.minimize(d["h"])
    if a == "screenoff":
        return phone_ctl.screen_off()
    if a == "lock":
        u.LockWorkStation()
        return "locked"
    if a in ("click", "dclick", "rclick", "scroll"):
        w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        if "x" in d and "y" in d:
            x = int(max(0.0, min(1.0, float(d["x"]))) * (w - 1))
            y = int(max(0.0, min(1.0, float(d["y"]))) * (h - 1))
            u.SetCursorPos(x, y)
        if a == "scroll":
            u.mouse_event(0x800, 0, 0, int(max(-15, min(15, int(d.get("dy", 0))))) * 120, 0)
            return "scroll"
        dn, up = (0x8, 0x10) if a == "rclick" else (0x2, 0x4)
        for _ in range(2 if a == "dclick" else 1):
            u.mouse_event(dn, 0, 0, 0, 0)
            u.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.05)
        return a
    if a == "type":
        return phone_ctl.type_text(d.get("text", ""), bool(d.get("enter")))
    raise ValueError("unknown action")


_LOGIN = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GOAT</title><style>
html,body{margin:0;height:100%;background:#0e0e0f;color:#eceae4;
font:16px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{display:flex;align-items:center;justify-content:center}
form{display:flex;flex-direction:column;gap:12px;width:80%;max-width:320px}
h1{margin:0 0 8px;letter-spacing:.12em;font-size:22px;text-align:center}
input{background:#161618;border:1px solid #ffffff1f;color:inherit;
border-radius:12px;padding:14px;font-size:20px;text-align:center;
letter-spacing:.15em}
button{background:#f0b429;border:none;color:#1a1405;border-radius:12px;
padding:14px;font-size:17px}
p{color:#ff7a6b;text-align:center;min-height:20px;margin:0}
</style></head><body><form method="post" action="/login">
<h1>GOAT</h1><input name="k" placeholder="login token" autocomplete="off"
autocapitalize="off" autocorrect="off" spellcheck="false" autofocus>
<button>Log in</button><p>%MSG%</p></form></body></html>"""


def _page_ver() -> str:
    """Changes whenever phone.html does. An open page that reconnects to a
    newer GOAT reloads itself — before this, fixes to the page never reached
    a phone that stayed open (the bubble app keeps its page alive)."""
    try:
        return str(int(os.path.getmtime(PAGE)))
    except OSError:
        return ""


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
        self._ts_task = None
        self._ts_ctx = None    # real cert for the Tailscale name, once issued
        self.ts_host = None
        self._lan_urls = []
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
        # Which device voices replies: "auto" (the one he last used), or
        # pinned to "phone" / "laptop" from the Control tab. Survives restarts.
        self.pref = _read_pref()
        self._spoke = None     # last route sent to the phones
        self._voice_end = 0.0  # when my queued speech finishes playing (echo guard)
        self._chatlog = deque(maxlen=60)  # [his text, my words]: every turn, even cut ones
        self._streams = {}     # stream id -> {"rt": realtime ear session, "t": last chunk}
        self._said = deque(maxlen=40)   # my last sentences, to tell echo from him
        self._barge = None   # (time, words he'd heard) when a page paused for him
        self.aec_last = {}   # echo canceller reports: src -> (time, line)
        self._you_n = 0        # how many of his messages the app has taken

    # ---- lifecycle ----
    async def start(self) -> list:
        from aiohttp import web
        self.loop = asyncio.get_running_loop()
        self._sync_route()   # boot grace: laptop quiet if phone was last
        self.loop.call_later(BOOT_GRACE_S + 1, self._sync_route)
        ips = local_ips()
        self.token = load_token()
        crt, key = await asyncio.to_thread(ensure_cert, ips)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(crt, key)
        ctx.sni_callback = self._sni
        wapp = web.Application(client_max_size=8 * 1024 * 1024,
                               middlewares=[self._gate])
        wapp.router.add_get("/", self._page)
        wapp.router.add_post("/login", self._login)
        wapp.router.add_get("/ws", self._ws)
        wapp.router.add_get("/shot", self._shot)
        wapp.router.add_get("/screen", self._screen)
        wapp.router.add_post("/act", self._act)
        wapp.router.add_get("/ctl", self._ctl)
        wapp.router.add_post("/upload", self._upload)
        wapp.router.add_get("/manifest.json", self._manifest)
        wapp.router.add_get("/icon.svg", self._icon)
        wapp.router.add_get("/icon-{size:192|512}{mask:(?:-maskable)?}.png",
                            self._icon_png)
        wapp.router.add_get("/sw.js", self._sw)
        wapp.router.add_get("/goat.apk", self._apk)
        wapp.router.add_get("/phone/poll", self._hands_poll)
        wapp.router.add_post("/phone/result", self._hands_result)
        wapp.router.add_post("/phone/do", self._hands_do)
        wapp.router.add_post("/phone/utt", self._bg_utt)
        wapp.router.add_post("/phone/chunk", self._bg_chunk)
        wapp.router.add_post("/phone/aec", self._bg_aec)
        wapp.router.add_get("/push/key", self._push_key)
        wapp.router.add_post("/push/sub", self._push_sub)
        self._runner = web.AppRunner(wapp, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port,
                          ssl_context=ctx).start()
        self._lan_urls = [f"https://{a}:{self.port}" for a in ips]
        self.urls = list(self._lan_urls)
        self._hook()
        asyncio.create_task(asyncio.to_thread(ensure_firewall, self.port))
        self._vitals_task = asyncio.create_task(self._vitals_loop())
        self._ts_task = asyncio.create_task(self._ts_loop())
        self._write_urls()
        print(f"[phone] ready on {', '.join(self.urls) or 'no LAN address'}")
        return self.urls

    def _write_urls(self):
        try:
            with open(os.path.join(DIR, "url.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(self.urls) + f"\ntoken: {self.token}\n")
        except OSError:
            pass

    def _sni(self, sock, server_name, _ctx):
        """Same port, two certificates: the Tailscale name gets the real one,
        bare IPs keep the self-signed one."""
        if server_name and self._ts_ctx is not None \
                and server_name.lower() == self.ts_host:
            sock.context = self._ts_ctx
        return None

    def use_ts_cert(self, name: str, crt: str, key: str):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(crt, key)
        self._ts_ctx, self.ts_host = ctx, name
        self.urls = [f"https://{name}:{self.port}"] + self._lan_urls
        self._write_urls()

    async def _ts_loop(self):
        """Pick up (and keep renewing) the real Tailscale certificate. Runs in
        the background so a slow first issue never delays GOAT's boot."""
        announced = None
        while True:
            try:
                got = await asyncio.to_thread(ts_cert)
            except Exception as e:  # noqa: BLE001
                got = None
                print(f"[phone] tailscale cert check failed: {e}")
            if got:
                self.use_ts_cert(*got)
                if announced != got[0]:
                    announced = got[0]
                    print(f"[phone] installable app at https://{got[0]}:{self.port}")
            await asyncio.sleep(TS_RECHECK_S if got else 120)

    async def stop(self):
        for t in (self._vitals_task, self._ts_task):
            if t is not None:
                t.cancel()
        self._vitals_task = self._ts_task = None
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
        k = request.query.get("k", "").strip().lower()
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
        st = {"q": asyncio.Queue(maxsize=400), "live": False, "t_in": 0.0}
        self.clients[ws] = st
        self._sync_route()
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
                    self._utterance(msg.data, st, sid=st.pop("sid", ""))
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
        q = request.query
        try:
            w = max(480, min(1920, int(q.get("w", 1280))))
            qual = max(35, min(85, int(q.get("q", 68))))
        except ValueError:
            w, qual = 1280, 68
        try:
            data = await asyncio.to_thread(grab_screen, w, qual)
        except Exception as e:  # noqa: BLE001
            return web.Response(status=500, text=f"capture failed: {e}")
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def _manifest(self, request):
        from aiohttp import web
        return web.json_response({
            "name": "GOAT", "short_name": "GOAT", "start_url": "/",
            "id": "/", "scope": "/", "description": "Talk to GOAT, your laptop's AI",
            "display": "standalone", "orientation": "portrait",
            "background_color": "#0e0e0f", "theme_color": "#0e0e0f",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192",
                 "type": "image/png", "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "any"},
                {"src": "/icon-512-maskable.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
                {"src": "/icon.svg", "sizes": "any",
                 "type": "image/svg+xml", "purpose": "any"}]})

    async def _icon_png(self, request):
        from aiohttp import web
        key = request.match_info["size"] + request.match_info["mask"]
        cache = self.__dict__.setdefault("_png_cache", {})
        if key not in cache:
            cache[key] = await asyncio.to_thread(
                icon_png, int(request.match_info["size"]),
                bool(request.match_info["mask"]))
        return web.Response(body=cache[key], content_type="image/png",
                            headers={"Cache-Control": "max-age=86400"})

    async def _apk(self, request):
        """The Android bubble app (workspace/goat-bubble), for logged-in phones."""
        from aiohttp import web
        k = request.query.get("k", "")
        if not self._authed(request) and not (
                k and hmac.compare_digest(k.encode(), self.token.encode())):
            return web.Response(status=401, text="log in first")
        apk = os.path.join(os.path.dirname(DIR), "workspace", "goat-bubble",
                           "out", "goat-bubble.apk")
        if not os.path.exists(apk):
            return web.Response(status=404, text="not built yet")
        return web.FileResponse(apk, headers={
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Disposition": 'attachment; filename="goat-bubble.apk"',
            "Cache-Control": "no-store"})

    # ---- web push: pings his phone when a task ends, even with the app closed
    def _vapid(self):
        from py_vapid import Vapid
        pem = os.path.join(DIR, "vapid.pem")
        v = Vapid()
        if os.path.exists(pem):
            v = Vapid.from_file(pem)
        else:
            v.generate_keys()
            v.save_key(pem)
        return v

    def _push_subs(self) -> list:
        try:
            with open(os.path.join(DIR, "push.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _push_save(self, subs: list):
        with open(os.path.join(DIR, "push.json"), "w", encoding="utf-8") as f:
            json.dump(subs, f)

    # ---- phone hands: the app's accessibility service long-polls for commands
    async def _hands_poll(self, request):
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=401)
        q = self.__dict__.setdefault("_hq", asyncio.Queue())
        self._hands_seen = time.monotonic()
        while True:
            try:
                cmd = await asyncio.wait_for(q.get(), 25)
            except asyncio.TimeoutError:
                return web.json_response({})
            # A command whose caller already gave up (phone was offline) must
            # not fire when the phone comes back — "open Facebook" an hour
            # late, over whatever he is doing.
            if cmd.get("id") in self.__dict__.get("_hw", {}):
                break
        self._hands_seen = time.monotonic()
        return web.json_response({"cmd": cmd})

    async def _bg_utt(self, request):
        """Bubble-mode mic: one raw int16 16kHz utterance per POST."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=401)
        data = await request.read()
        # X-Media: 1 = another app was playing sound while this was recorded
        # (he's scrolling videos). Then only a line with my name counts.
        # X-Barge: 1 = he talked over my voice; the page has paused it.
        self._utterance(data, media=request.headers.get("X-Media") == "1",
                        barge=request.headers.get("X-Barge") == "1",
                        sid=request.headers.get("X-Stream", ""))
        return web.Response(text="ok")

    async def _hands_result(self, request):
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=401)
        try:
            r = await request.json()
        except Exception:
            return web.Response(status=400)
        fut = self.__dict__.setdefault("_hw", {}).pop(str(r.get("id")), None)
        if fut and not fut.done():
            fut.set_result(r)
        return web.json_response({"ok": True})

    async def _hands_do(self, request):
        """Laptop-side entry (phone_hands.py): queue a command, wait for the phone."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=401)
        try:
            cmd = await request.json()
        except Exception:
            return web.Response(status=400)
        # The app long-polls every <=25 s; silent for 40 s = offline. Say so
        # now instead of holding a voice command for the full 20 s.
        if time.monotonic() - getattr(self, "_hands_seen", -1e9) > 40:
            return web.json_response(
                {"ok": False, "error": "phone offline (GOAT app not connected)"})
        cmd["id"] = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        self.__dict__.setdefault("_hw", {})[cmd["id"]] = fut
        await self.__dict__.setdefault("_hq", asyncio.Queue()).put(cmd)
        try:
            res = await asyncio.wait_for(fut, 20)
        except asyncio.TimeoutError:
            self._hw.pop(cmd["id"], None)
            res = {"ok": False, "error": "phone didn't answer (accessibility on? app online?)"}
        return web.json_response(res)

    async def _push_key(self, request):
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=401)
        import base64
        from cryptography.hazmat.primitives import serialization as ser
        pub = self._vapid().public_key.public_bytes(
            ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)
        return web.json_response(
            {"key": base64.urlsafe_b64encode(pub).rstrip(b"=").decode()})

    async def _push_sub(self, request):
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=401)
        sub = await request.json()
        if not isinstance(sub, dict) or not sub.get("endpoint"):
            return web.Response(status=400)
        subs = [x for x in self._push_subs() if x.get("endpoint") != sub["endpoint"]]
        subs.append(sub)
        self._push_save(subs[-5:])
        return web.json_response({"ok": True})

    def notify(self, title: str, body: str, tag: str = "goat"):
        """Fire-and-forget phone notification (never raises)."""
        subs = self._push_subs()
        if not subs:
            return

        def run():
            try:
                from pywebpush import webpush, WebPushException
            except ImportError:
                return
            keep = []
            for sub in subs:
                try:
                    webpush(sub, json.dumps({"title": title, "body": body[:180],
                                             "tag": tag}),
                            vapid_private_key=os.path.join(DIR, "vapid.pem"),
                            vapid_claims={"sub": "mailto:giorgigzirishvili2004@gmail.com"},
                            ttl=3600)
                    keep.append(sub)
                except WebPushException as e:
                    code = getattr(getattr(e, "response", None), "status_code", 0)
                    if code not in (404, 410):
                        keep.append(sub)
                except Exception:  # noqa: BLE001
                    keep.append(sub)
            if len(keep) != len(subs):
                try:
                    self._push_save(keep)
                except OSError:
                    pass
        threading.Thread(target=run, daemon=True).start()

    async def _sw(self, request):
        from aiohttp import web
        return web.Response(text=_SW, content_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    async def _icon(self, request):
        from aiohttp import web
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
               '<rect width="512" height="512" rx="110" fill="#0e0e0f"/>'
               '<circle cx="256" cy="256" r="150" fill="none" stroke="#f0b429" '
               'stroke-width="28"/><circle cx="256" cy="256" r="70" '
               'fill="#eceae4"/></svg>')
        return web.Response(text=svg, content_type="image/svg+xml")

    async def _upload(self, request):
        """Photos/files from the phone go straight to GOAT as an order."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=403)
        up = os.path.join(os.path.dirname(HERE), "workspace", "phone-uploads")
        os.makedirs(up, exist_ok=True)
        paths, note, total = [], "", 0
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "note":
                note = (await part.text())[:1000]
            elif part.name == "file" and part.filename:
                ext = os.path.splitext(part.filename)[1][:8]
                ext = "".join(c for c in ext if c.isalnum() or c == ".")
                fp = os.path.join(up, time.strftime("%Y%m%d-%H%M%S-")
                                  + f"{len(paths)}{ext}")
                with open(fp, "wb") as f:
                    while True:
                        ch = await part.read_chunk(1 << 16)
                        if not ch:
                            break
                        total += len(ch)
                        if total > 40 << 20:
                            f.close()
                            os.remove(fp)
                            return web.json_response(
                                {"ok": False, "err": "too big (40 MB max)"})
                        f.write(ch)
                paths.append(fp)
        if not paths:
            return web.json_response({"ok": False, "err": "no file"})
        self.app.submit_files(paths, note, origin="phone")
        return web.json_response({"ok": True, "n": len(paths)})

    async def _act(self, request):
        """Remote control: quick actions, tap-to-click, typing."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=403)
        try:
            d = await request.json()
            if d.get("a") == "speaks":
                return web.json_response({"ok": True, **self.set_pref(d.get("v"))})
            r = await asyncio.to_thread(do_action, d)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "err": str(e)[:160]})
        if isinstance(r, dict):
            return web.json_response({"ok": True, **r})
        return web.json_response({"ok": True, "msg": r})

    async def _ctl(self, request):
        """The Control tab's live state: volume, mute, brightness, now
        playing, open windows, and which device speaks."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=403)
        import phone_ctl
        try:
            st = await asyncio.to_thread(phone_ctl.state)
        except Exception as e:  # noqa: BLE001
            st = {"err": str(e)[:160]}
        st.update(self._route_info())
        return web.json_response(st, headers={"Cache-Control": "no-store"})

    def _snapshot(self) -> dict:
        hist = []
        for pair in list(getattr(self.app, "_exchanges", None) or []):
            try:
                u, r = str(pair[0]), str(pair[1])
            except (TypeError, IndexError):
                continue
            if u and not u.startswith("["):
                hist.append([u[:300], r[:300]])
        mine = {u for u, _ in self._chatlog}
        hist = [h for h in hist if h[0] not in mine] + [list(c) for c in self._chatlog]
        return {"type": "snapshot", "now": time.time(), "booted": self.booted,
                "state": self.state, "done": list(self.done_tasks),
                "history": hist, "route": self._route_info(),
                "busy": bool(getattr(self.app, "busy", False)),
                "ver": _page_ver()}

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
    # ---- streaming ear for phone audio ------------------------------------
    # The phone used to send a clip only after 0.7 s of his silence, and the
    # batch ear then spent ~1.1 s on it. Now the page/bubble also stream the
    # audio WHILE he talks (chunks tagged with a stream id), the laptop's
    # realtime ear transcribes along, and the final clip only closes it:
    # 240-310 ms after the end instead of ~1.1 s (measured 2026-10-18). The
    # final clip still carries all the audio, so a failed stream costs
    # nothing but the old batch time.
    def _stream_chunk(self, sid: str, pcm: bytes = b"", drop: bool = False):
        if not sid:
            return
        now = time.monotonic()
        s = self._streams.get(sid)
        if drop:
            if s is not None:
                self._streams.pop(sid, None)
                self._end_rt(s.get("rt"))
            return
        if s is None:
            for k, old in list(self._streams.items()):
                if now - old["t"] > STREAM_TTL_S:
                    self._streams.pop(k, None)
                    self._end_rt(old.get("rt"))
            # He just started talking: dial the voice socket under his words.
            try:
                import tts_edge
                tts_edge.prime()
            except Exception:  # noqa: BLE001 — a cold socket only costs time
                pass
            rt = None
            try:
                import stt_realtime
                rt = stt_realtime.start_threadsafe(self.app._rt_lang(), self.loop)
            except Exception as e:  # noqa: BLE001 — batch ear still has the clip
                print(f"[phone] streaming ear not started: {e!r}", flush=True)
            s = self._streams[sid] = {"rt": rt, "t": now}
        s["t"] = now
        rt = s.get("rt")
        if rt is not None and len(pcm) >= 2:
            try:
                rt.feed(np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2")
                        .astype(np.float32) / 32768.0)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _end_rt(rt):
        try:
            if rt is not None:
                rt.end()
        except Exception:  # noqa: BLE001
            pass

    async def _barge_watch(self, mark):
        """The page paused my voice for "him". Room noise holds its VAD open
        for 4-20 s at a time (2026-10-19: every reply opened with "paused for
        him (heard up to '')", then a noise clip, an empty ear and "carrying
        on"). His real words reach the streaming ear within a second; if it
        has heard nothing by now, tell the page to carry on. His clip still
        arrives as a cut-in and is judged as usual."""
        await asyncio.sleep(BARGE_HEAR_S)
        if self._barge is not mark:
            return                      # his clip is in: its verdict decides
        now = time.monotonic()
        live = [x for x in self._streams.values() if now - x["t"] < 1.0]
        if not live:
            return                      # not streamed: no way to tell
        rt = max(live, key=lambda x: x["t"]).get("rt")
        if rt is None or (getattr(rt, "last_partial", "") or "").strip():
            return
        print("[phone] paused for him, but no words in "
              f"{BARGE_HEAR_S:.1f}s: carrying on", flush=True)
        self._send(json.dumps({"type": "carry"}))

    def _take_stream(self, sid):
        """The clip is complete: commit its stream and hand the session on."""
        s = self._streams.pop(sid, None) if sid else None
        rt = s.get("rt") if s else None
        self._end_rt(rt)
        return rt

    async def _bg_chunk(self, request):
        """Bubble-mode mic, while he is still talking: raw int16 16 kHz."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=401)
        data = await request.read()
        self._stream_chunk(request.headers.get("X-Stream", ""), data,
                           drop=request.headers.get("X-Drop") == "1")
        return web.Response(text="ok")

    async def _bg_aec(self, request):
        """The bubble mic's echo canceller reporting in (one line, ~every 5 s
        while I talk): lets me check from here that it works on the phone."""
        from aiohttp import web
        if not self._authed(request):
            return web.Response(status=401)
        line = (await request.text())[:300]
        self._aec_log("bubble", line)
        return web.Response(text="ok")

    def _aec_log(self, src: str, line: str):
        self.aec_last[src] = (time.time(), line)
        print(f"[phone-aec] {src}: {line}", flush=True)

    def _utterance(self, data: bytes, st=None, media: bool = False,
                   barge: bool = False, sid: str = ""):
        rt = self._take_stream(sid)
        # Dial the voice socket now, under the time the ear takes on this
        # clip (a streamed clip already primed it when he started talking).
        # The laptop mic primes when he starts talking; phone clips never did,
        # so a phone answer's first clause paid the ~950 ms dial (2026-10-18).
        if rt is None:
            try:
                import tts_edge
                tts_edge.prime()
            except Exception:  # noqa: BLE001 — a cold socket only costs time
                pass
        n = len(data) // 2
        mark, self._barge = self._barge, None
        if mark is not None and time.time() - mark[0] > BARGE_MARK_S:
            mark = None
        if n < MIN_SAMPLES or n > MAX_SAMPLES:
            if mark is not None:
                self._send(json.dumps({"type": "idle"}))   # page: carry on
            return
        if barge or mark is not None:
            # He talked over me and my voice is paused for him: this clip is
            # HIM (recorded after the pause, a breath of my echo at most), so
            # none of the overlap / media / pinned-laptop rules apply. The
            # ear decides; real words -> "you" and goat_app drops the rest of
            # that answer, nothing -> "idle" and the page carries on.
            audio = np.frombuffer(data[:n * 2], dtype="<i2").astype(np.float32) / 32768.0
            pk = float(np.max(np.abs(audio))) if n else 0.0
            if 0.002 < pk < 0.5:
                audio = np.clip(audio * min(12.0, 0.6 / pk), -1.0, 1.0)
            if st is not None:
                st["t_in"] = time.time()
            print(f"[phone] cut-in {n / 16000:.1f}s peak {pk:.3f}", flush=True)
            try:
                self.app._lat_start()
            except Exception:  # noqa: BLE001
                pass
            self._from_phone()
            asyncio.create_task(self._take_clip(
                audio, cut_in=mark[1] if mark is not None else "", rt=rt))
            return
        if not self._phone_speaks() and self._laptop_talking():
            # Pinned to the laptop and it's mid-sentence: what the phone mic
            # caught is the laptop's own voice. Tap Stop to cut it off.
            return
        # Echo of my own voice (bug 2026-10-18 12:41): the bubble mic closes an
        # utterance ~0.7 s after the sound stops, so it lands AFTER I finished
        # talking and the check above passes. Judge by when the clip was
        # recorded: if it started before my voice ended (+ room tail), it is me.
        now = time.time()
        started = now - n / SR
        overlap = st is None and started < self._voice_end + ECHO_TAIL_S
        if st is not None:
            st["t_in"] = time.time()
        audio = np.frombuffer(data[:n * 2], dtype="<i2").astype(np.float32) / 32768.0
        # Bubble/WebView mics arrive quiet (no auto-gain): lift to a healthy
        # level so Whisper hears short, soft words. Capped so hiss isn't boosted.
        # No loudness gate: the bubble mic records raw, so his own voice peaks
        # 0.1-0.3 there, and a 0.16 "faint = far away" cut threw away whole
        # requests (2026-10-18 14:05, "Please restore them"). Far voices are
        # the voice lock's job.
        pk = float(np.max(np.abs(audio))) if n else 0.0
        if 0.002 < pk < 0.5:
            audio = np.clip(audio * min(12.0, 0.6 / pk), -1.0, 1.0)
        media = media and started >= self._voice_end + SELF_AUDIO_S
        if overlap or media:
            # Dropping every overlapping clip also ate his barge-ins (his
            # report 2026-10-18 12:45: "I could not interrupt you"). Hear it
            # first: my own words = echo, anything new = him cutting in.
            # Media playing (his order 2026-10-18: "the phone must not hear me
            # scrolling through apps"): a video's voice is not him — only a
            # line that names me gets through.
            asyncio.create_task(self._overlap_clip(audio, overlap=overlap,
                                                   media=media, peak=pk, rt=rt))
            return
        print(f"[phone] utterance {n / 16000:.1f}s peak {pk:.3f}", flush=True)
        try:
            self.app._lat_start()
        except Exception:  # noqa: BLE001 — the ledger is optional
            pass
        self._from_phone()
        asyncio.create_task(self._take_clip(audio, rt=rt))

    async def _take_clip(self, audio, cut_in: str | None = None, rt=None):
        """Run one phone clip through the ear and the brain. A clip that
        produces no message (silence, a hallucinated "you", a voice-lock
        reject) used to leave the phone saying "thinking" forever — tell it
        to go back to listening (and a paused voice to carry on).

        `cut_in`: he talked over me; what he'd heard of my answer so far.
        goat_app._talk reads it off the app to stop that answer for good."""
        n0 = self._you_n
        if cut_in is not None:
            self.app._cut_in = (time.monotonic(), cut_in)
        try:
            await self.app._handle_utterance(audio, rt, None, source="phone")
        except Exception as e:  # noqa: BLE001 — one bad clip must not kill the socket
            print(f"[phone] clip failed: {e!r}", flush=True)
        finally:
            if cut_in is not None:
                self.app._cut_in = None
            if self._you_n == n0:
                try:
                    self.app._lat_restore()
                except Exception:  # noqa: BLE001 — the ledger is optional
                    pass
                if cut_in is not None:
                    print("[phone] cut-in was not words for me: carrying on",
                          flush=True)
                self._send(json.dumps({"type": "idle"}))

    def is_echo(self, text: str, overlap: bool = False) -> bool:
        """Is this phone transcript my own recent words coming back? Checked
        on EVERY phone clip (goat_app._heard): the panel's mic path used to
        skip it and my sentences arrived as his messages.

        Recorded over my voice (`overlap`): most of its words being mine is
        enough. Otherwise it must repeat my words IN ORDER, a run of at least
        ECHO_RUN of them, so "yes, close the tabs" after "want me to close the
        tabs?" is still him."""
        import difflib
        import re
        words = re.findall(r"[a-z']+", (text or "").lower())
        if not words:
            return True
        # A long reply is queued whole but plays for many seconds, so a clip
        # recorded over its tail quotes sentences older than ECHO_WINDOW_S
        # (bug 2026-10-18 17:39: my own paragraph came back as his message).
        cut = time.time() - (ECHO_WINDOW_S * 3 if overlap else ECHO_WINDOW_S)
        mine = re.findall(r"[a-z']+", " ".join(
            t for at, t in self._said if at >= cut).lower())
        if overlap:
            m = set(mine)
            if sum(w in m for w in words) / len(words) >= 0.5:
                return True
            import difflib as _d
            run = sum(b.size for b in _d.SequenceMatcher(
                None, mine, words, autojunk=False).get_matching_blocks())
            return run >= ECHO_RUN and run / len(words) >= 0.4
        got = sum(b.size for b in difflib.SequenceMatcher(
            None, mine, words, autojunk=False).get_matching_blocks())
        return got >= ECHO_RUN and got / len(words) >= ECHO_SHARE

    def _echo_words(self, text: str, overlap: bool):
        """(heard words, my recent words). A heard word one letter or two off
        a word I said is that word: the ear mishears my echo ("up the
        cliffs, and Elio" came back "off a cliff, and Emilio", 2026-10-19,
        and passed as his request). Stop words stay exact."""
        import difflib
        import re
        words = re.findall(r"[a-z']+|[ა-ჿ]+", (text or "").lower())
        cut = time.time() - (ECHO_WINDOW_S * 3 if overlap else ECHO_WINDOW_S)
        mine = re.findall(r"[a-z']+|[ა-ჿ]+", " ".join(
            t for at, t in self._said if at >= cut).lower())
        known = set(mine)
        out = []
        for w in words:
            if w not in known and len(w) >= 3 and not _STOP_LEFT.fullmatch(w):
                near = difflib.get_close_matches(w, known, n=1, cutoff=0.75)
                if near:
                    w = near[0]
            out.append(w)
        return out, mine

    def leftover(self, text: str, overlap: bool = True) -> str:
        """His words inside a clip that is mostly my own voice: every run of
        2+ words that repeats what I said recently (in order) is my echo; the
        rest is what's left over — usually him."""
        import difflib
        import re
        words, mine = self._echo_words(text, overlap)
        taken = set()
        for b in difflib.SequenceMatcher(None, mine, words,
                                         autojunk=False).get_matching_blocks():
            if b.size >= 2:
                taken.update(range(b.b, b.b + b.size))
        return " ".join(w for i, w in enumerate(words) if i not in taken)

    def his_run(self, text: str, overlap: bool = True) -> str | None:
        """A real request riding on my echo, or None: the longest stretch of
        4+ words in a row that aren't a replay of mine, most of them words I
        haven't said. 2026-10-19 10:57: "…you can test talking over me
        whenever you like. No, could you please tell me a story so I can test
        it out." was dropped whole as my own voice."""
        import difflib
        import re
        words, mine = self._echo_words(text, overlap)
        taken = set()
        for b in difflib.SequenceMatcher(None, mine, words,
                                         autojunk=False).get_matching_blocks():
            if b.size >= 2:
                taken.update(range(b.b, b.b + b.size))
        best, cur = [], []
        for i, w in enumerate(words):
            if i in taken:
                cur = []
                continue
            cur.append(w)
            if len(cur) > len(best):
                best = list(cur)
        if len(best) < 4:
            return None
        known = set(mine)
        if sum(w in known for w in best) / len(best) > 0.75:
            return None     # my words, just shuffled by the ear
        return " ".join(best)

    def stop_inside_echo(self, text: str, overlap: bool = True) -> str | None:
        """His "stop" buried in my echo, or None. 2026-10-19 10:58: "Okay,
        stop … No, no, no, no … Stop." rode a 13 s clip of my story and the
        whole clip was dropped as my own voice — he could not stop me. A stop
        word I said myself only counts when he says it twice."""
        import re
        left = self.leftover(text, overlap)
        hits = [m.group(0).lower() for m in _STOP_LEFT.finditer(left)]
        if not hits:
            return None
        cut = time.time() - ECHO_WINDOW_S * 3
        mine = set(re.findall(r"[a-z']+|[ა-ჿ]+", " ".join(
            t for at, t in self._said if at >= cut).lower()))
        if len(hits) < 2 and hits[0].split()[0] in mine:
            return None
        return left

    async def _overlap_clip(self, audio, overlap: bool, media: bool, peak: float,
                            rt=None):
        """A clip recorded while I was talking (echo of me, or him cutting
        in?) or while another app played sound (a video, or him?)."""
        import stt_whisper
        text = None
        if rt is not None:
            # The streaming cloud ear writes down BOTH voices; local Whisper
            # keeps only the louder one, and over my echo that is me — his
            # "stop, stop, wait" vanished whenever my echo was as loud as him
            # (measured 2026-10-19 on testdata/aec).
            try:
                text = await rt.result()
            except Exception:  # noqa: BLE001
                text = None
        try:
            if not text:
                text = await asyncio.to_thread(stt_whisper.transcribe, audio)
        except Exception:  # noqa: BLE001
            self._send(json.dumps({"type": "idle"}))
            return
        if not text:
            self._send(json.dumps({"type": "idle"}))
            return
        # Inside my voice the bar is lower: it IS mostly echo.
        if overlap and self.is_echo(text, overlap=True):
            stop = self.stop_inside_echo(text, overlap=True)
            if stop:
                print(f"[barge] his stop inside my echo: {stop!r} in {text[:80]!r}",
                      flush=True)
                await self.app._echo_stop(stop)
                return
            run = self.his_run(text, overlap=True)
            if not run:
                print(f"[phone] dropped clip: my own voice ({text!r})", flush=True)
                self._send(json.dumps({"type": "idle"}))
                return
            print(f"[phone] his words inside my echo: {run!r}", flush=True)
            text = run
        if media:
            wake = getattr(sys.modules.get("goat_app"), "WAKE_RE", None) or _WAKE
            if not wake.search(text):
                print(f"[phone] other audio playing, not addressed, ignored "
                      f"(peak {peak:.3f}): {text!r}", flush=True)
                self._send(json.dumps({"type": "idle"}))
                return
        if not overlap:
            print(f"[phone] heard over other audio: {text!r}", flush=True)
            self._from_phone()
            await self.app._heard(text, None, None, "phone")
            return
        ga = sys.modules.get(type(self.app).__module__)
        bare, core = (getattr(ga, "BARE_STOP_RE", None),
                      getattr(ga, "BARE_STOP_CORE", None))
        if bare is not None and bare.match(text.strip()) and core.search(text):
            # Only a stop: stop, no reply (2026-10-19: "Stop, wait a minute."
            # cut my story here, then cost a model turn for "Sir, stopped.").
            print(f"[barge] he said stop over my voice: {text!r}", flush=True)
            self._voice_end = 0.0
            await self.app._echo_stop(text)
            return
        print(f"[phone] barge-in over my voice: {text!r}", flush=True)
        self._voice_end = 0.0
        self.app.tts.cancel()
        self.app._say_buf = ""
        self._from_phone()
        await self.app._heard(text, None, None, "phone")

    def _command(self, st, raw: str):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        kind = m.get("type")
        if kind == "text":
            text = str(m.get("text", "")).strip()[:4000]
            if text:
                st["t_in"] = time.time()
                self.app.submit_text(text, origin="phone")
        elif kind == "live":
            st["live"] = bool(m.get("on"))
            if st["live"]:
                st["t_in"] = time.time()
                self._from_phone()   # mic on = he's on the phone now
            self._sync_live()
        elif kind == "chunk":
            # His words so far, while he is still talking (streaming ear).
            try:
                pcm = base64.b64decode(str(m.get("pcm", "")))
            except Exception:  # noqa: BLE001
                pcm = b""
            self._stream_chunk(str(m.get("sid", ""))[:40], pcm)
        elif kind == "utt_end":
            st["sid"] = str(m.get("sid", ""))[:40]   # the next binary clip closes it
        elif kind == "utt_drop":
            self._stream_chunk(str(m.get("sid", ""))[:40], drop=True)
        elif kind == "aec":
            self._aec_log("page", str(m.get("line", ""))[:300])
        elif kind == "barge":
            # He started talking over me and the page paused my voice (it
            # resumes on "idle", drops the rest on "you"). His clip follows,
            # from this page's mic or the bubble's (X-Barge).
            self._barge = (time.time(), str(m.get("heard", ""))[:300])
            print(f"[phone] paused for him (heard up to {self._barge[1][:60]!r})",
                  flush=True)
            asyncio.create_task(self._barge_watch(self._barge))
        elif kind == "cut":
            # The phone's Stop button: end the running turn, like talking
            # over it on the laptop.
            self._voice_end = 0.0
            self.app.tts.cancel()
            self.app._say_buf = ""
            cut = getattr(self.app, "_cut_turn", None)
            if cut is not None and getattr(self.app, "busy", False):
                asyncio.create_task(cut(None))

    def _sync_live(self):
        """While the phone's mic is live the laptop stops listening and goes
        silent; otherwise its mic hears him twice and its speaker barges
        into the phone's turn."""
        self.app.phone_live = any(c["live"] for c in self.clients.values())
        self._sync_route()

    def _from_phone(self):
        route = getattr(self.app, "_route", None)
        if route is not None:
            route("phone")
        else:
            self.app.reply_to = "phone"
            self._sync_route()

    def _phone_speaks(self) -> bool:
        """Replies are voiced on ONE device: the phone while its mic is live
        or when he last spoke/typed there (and it's still connected);
        otherwise the laptop. Never both (his order 2026-10-17). The Control
        tab can pin it to one device."""
        if not self.clients:
            # just restarted and the phone hasn't reconnected yet: keep the
            # laptop quiet for a short grace if the phone was his device
            app = self.app
            if time.time() - getattr(app, "boot_t", 0) < BOOT_GRACE_S:
                return (self.pref == "phone" or (self.pref == "auto" and
                        getattr(app, "reply_to", "laptop") == "phone"))
            return False
        if self.pref in ("phone", "laptop"):
            return self.pref == "phone"
        return bool(getattr(self.app, "phone_live", False)
                    or getattr(self.app, "reply_to", "laptop") == "phone")

    def _laptop_talking(self) -> bool:
        if getattr(getattr(self.app, "audio", None), "is_tts_playing", False):
            return True
        try:
            return bool(self.app.tts.speaking())
        except Exception:  # noqa: BLE001
            return False

    def _route_info(self) -> dict:
        return {"speaks": "phone" if self._phone_speaks() else "laptop",
                "pref": self.pref}

    def _sync_route(self):
        self.app.tts.local_mute = self._phone_speaks()
        info = self._route_info()
        if info != self._spoke:
            self._spoke = info
            self._send(json.dumps({"type": "route", **info}))

    def set_pref(self, v) -> dict:
        v = str(v or "auto").lower()
        if v not in ("auto", "phone", "laptop"):
            raise ValueError("speaks must be auto, phone or laptop")
        self.pref = v
        _write_pref(v)
        self._sync_route()
        return self._route_info()

    def _voice_targets(self) -> list:
        """The one phone that gets the audio: a live mic first, else the one
        he used last. (Two open pages must not talk over each other.)"""
        live = [w for w, c in self.clients.items() if c["live"]]
        if live:
            return live[-1:]
        best = max(self.clients.items(), key=lambda kv: kv[1]["t_in"],
                   default=None)
        return [best[0]] if best else []

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
            if kind == "you":
                self._you_n += 1
                self._chatlog.append([str(data)[:300], ""])
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
        self.app.on_route = self._sync_route
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
            if t is not None and now - t.get("t", now) > 15:
                self.notify("GOAT failed" if kind == "work_fail" else "GOAT is done",
                            (t.get("text") or data)[:140], "task")
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
            self.notify("GOAT hit the usage limit", data[:140], "limit")
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
        self._said.append((time.time(), text))
        if self._chatlog:
            self._chatlog[-1][1] = (self._chatlog[-1][1] + " " + text).strip()[:300]
        try:
            import stt_whisper
            stt_whisper.note_chatting()
        except Exception:  # noqa: BLE001
            pass
        if samples is not None and len(samples) > 0:
            # my voice will play for len/SR seconds, queued after what's already out
            self._voice_end = max(self._voice_end, time.time()) + len(samples) / SR
        if not self.clients:
            return
        # Laptop's turn: the phone still shows the words, but stays silent.
        voiced = (self._phone_speaks() and samples is not None
                  and len(samples) > 0)
        quiet = json.dumps({"type": "say", "text": text, "audio": False})
        if not voiced:
            self._send(quiet)
            return
        loud = [json.dumps({"type": "say", "text": text, "audio": True}),
                (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()]
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._post_voice, loud, quiet)

    def _post_voice(self, loud, quiet):
        targets = set(self._voice_targets())
        for ws, st in list(self.clients.items()):
            for item in (loud if ws in targets else [quiet]):
                try:
                    st["q"].put_nowait(item)
                except asyncio.QueueFull:
                    break
