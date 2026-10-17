"""Phone client smoke test: real HTTPS + WebSocket against a fake GOAT.
Run: py -3.13 test_phone_server.py"""
import asyncio
import json
import ssl
import sys
import tempfile

import numpy as np

import phone_server as ps

PORT = 8798
fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


class FakeTts:
    def __init__(self):
        self.on_sentence = self.on_cancel = None
        self.local_mute = False
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1
        if self.on_cancel:
            self.on_cancel()


class FakeApp:
    def __init__(self):
        self.tts = FakeTts()
        self.phone_live = False
        self._say_buf = ""
        self.texts, self.audio, self.events = [], [], []
        self.busy = False
        self.cuts = 0
        self._exchanges = [("what's up", "Not much."), ("[warm-up] x", "OK")]
        self.reply_to = "laptop"
        self.on_route = None

    def _route(self, origin):          # same contract as GoatApp._route
        self.reply_to = "phone" if origin == "phone" else "laptop"
        if self.on_route:
            self.on_route()

    def emit(self, kind, data=""):
        self.events.append((kind, data))

    def submit_text(self, text, origin="laptop"):
        self._route(origin)
        self.texts.append(text)
        self.emit("you", text)

    def _lat_start(self):
        pass

    async def _handle_utterance(self, audio, rt=None, gap=None):
        self.audio.append(audio)

    async def _cut_turn(self, then):
        self.cuts += 1
        self.busy = False


async def recv(ws, kind, skip=("vitals", "snapshot")):
    """Next JSON message, stepping over the periodic/catch-up ones."""
    while True:
        m = await asyncio.wait_for(ws.receive(), 5)
        if m.type != 1:            # binary
            return m
        d = json.loads(m.data)
        if d.get("type") == kind or d.get("type") not in skip:
            return d


async def ws_refused(s, url, sslc, origin=None):
    import aiohttp
    try:
        async with s.ws_connect(url, ssl=sslc, origin=origin):
            return False
    except aiohttp.WSServerHandshakeError as e:
        return e.status == 403


async def main():
    import aiohttp
    check("tailnet allowed", ps.allowed_remote("100.101.2.3"))
    check("LAN allowed", ps.allowed_remote("192.168.8.20"))
    check("internet refused", not ps.allowed_remote("8.8.8.8"))
    check("garbage refused", not ps.allowed_remote("nope"))

    app = FakeApp()
    ps.VITALS_S = 0.3
    ps.ts_cert = lambda: None   # never touch the real tailnet from a test
    srv = ps.PhoneServer(app, port=PORT)
    urls = await srv.start()
    check("server up with LAN url", any("192.168." in u or "10." in u for u in urls))
    base = f"https://127.0.0.1:{PORT}"
    sslc = ssl.create_default_context()
    sslc.check_hostname = False
    sslc.verify_mode = ssl.CERT_NONE
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as s:
        async with s.get(base + "/", ssl=sslc) as r:
            body = await r.text()
            check("no cookie -> login page", "login token" in body)
        check("ws without login refused", await ws_refused(s, base + "/ws", sslc))
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        async with s.post(base + "/login", data={"k": "wrongtoken1"}, ssl=sslc) as r:
            check("wrong token refused", r.status == 401)
        async with s.post(base + "/login", data={"k": srv.token.upper()},
                          ssl=sslc) as r:
            body = await r.text()
            check("right token -> app page", r.status == 200 and 'id="orb"' in body)
        async with s.ws_connect(base + "/ws", ssl=sslc,
                                origin=f"https://127.0.0.1:{PORT}") as ws:
            m = json.loads((await ws.receive()).data)
            check("ws connected", m.get("type") == "status")
            m = await recv(ws, "snapshot")
            check("snapshot on connect, warm-up hidden",
                  m["type"] == "snapshot" and m["history"] == [["what's up", "Not much."]])
            await ws.send_str(json.dumps({"type": "live", "on": True}))
            await asyncio.sleep(0.1)
            check("live -> laptop mic+speaker off",
                  app.phone_live and app.tts.local_mute)
            await ws.send_str(json.dumps({"type": "text", "text": "hello goat"}))
            m = await recv(ws, "you")
            check("typed text reaches GOAT and echoes",
                  app.texts == ["hello goat"] and m == {"type": "you", "text": "hello goat"})
            pcm = (np.sin(np.arange(16000) / 5) * 8000).astype("<i2").tobytes()
            await ws.send_bytes(pcm)
            await asyncio.sleep(0.2)
            check("phone audio reaches the ear",
                  len(app.audio) == 1 and len(app.audio[0]) == 16000)
            app.tts.on_sentence("Hi there.", np.zeros(800, dtype=np.float32) + 0.25)
            m = await recv(ws, "say")
            b = await recv(ws, "-")
            check("GOAT's sentence arrives as text then audio",
                  m["type"] == "say" and m["text"] == "Hi there."
                  and len(b.data) == 1600)
            # One device speaks, never both (his order 2026-10-17).
            await ws.send_str(json.dumps({"type": "live", "on": False}))
            await asyncio.sleep(0.1)
            check("mic off, last input phone -> laptop stays silent",
                  not app.phone_live and app.tts.local_mute)
            app._route("laptop")       # he talks/types on the laptop
            check("laptop input -> laptop speaks", not app.tts.local_mute)
            app.tts.on_sentence("Laptop reply.", np.zeros(800, dtype=np.float32) + 0.25)
            m = await recv(ws, "say")
            app.emit("status", "marker")
            nxt = await recv(ws, "status")
            check("laptop turn -> phone gets text, no audio",
                  m["text"] == "Laptop reply." and m["audio"] is False
                  and not isinstance(nxt, aiohttp.WSMessage)
                  and nxt.get("text") == "marker")
            await ws.send_str(json.dumps({"type": "text", "text": "typed on phone"}))
            await recv(ws, "you")
            check("phone typing -> laptop silent again",
                  app.reply_to == "phone" and app.tts.local_mute)
            app.tts.on_sentence("Phone reply.", np.zeros(800, dtype=np.float32) + 0.25)
            m = await recv(ws, "say")
            b = await recv(ws, "-")
            check("phone turn -> phone gets audio",
                  m["audio"] is True and len(b.data) == 1600)
            await ws.send_str(json.dumps({"type": "stop"}))
            m = await recv(ws, "cancel")
            check("tap-to-stop cancels the voice",
                  app.tts.cancelled == 1 and m["type"] == "cancel")
            app.emit("brain", "Opus 5.5|deep|high")
            m = await recv(ws, "brain")
            check("brain pick mirrored", m == {"type": "brain", "model": "Opus 5.5",
                                               "tier": "deep", "effort": "high"})
            app.busy = True
            app.emit("work_start", "Opus 5.5|fix the phone page")
            m = await recv(ws, "work")
            check("work start mirrored", m["ev"] == "start"
                  and m["task"]["text"] == "fix the phone page")
            app.emit("work_tool", "Read phone.html")
            m = await recv(ws, "work")
            check("work step mirrored", m["ev"] == "step" and m["step"][0] == "Read phone.html")
            app.emit("work_think", "Looking at ")
            app.emit("work_think", "the page.")
            m = await recv(ws, "work")
            check("thinking coalesced", m["ev"] == "think" and m["text"] == "Looking at the page.")
            app.emit("work_ctx", "21000|44000")
            m = await recv(ws, "ctx")
            check("context mirrored", m["used"] == 21000 and m["trim"] == 44000)
            m = await recv(ws, "vitals", skip=("snapshot",))
            check("vitals arrive", m["type"] == "vitals" and "ram" in m and "cpu" in m)
            await ws.send_str(json.dumps({"type": "cut"}))
            await asyncio.sleep(0.1)
            check("phone Stop cuts the running turn", app.cuts == 1)
            app.emit("work_done", "")
            m = await recv(ws, "work", skip=("vitals", "cancel"))
            check("work done mirrored", m["ev"] == "done" and len(srv.done_tasks) == 1)
        async with s.get(base + "/screen", ssl=sslc) as r:
            data = await r.read()
            check("fresh screen capture is a jpeg", r.status == 200 and data[:2] == bytes([255, 216]))
        await asyncio.sleep(0.2)
        check("phone gone -> laptop mic back", not app.phone_live and not app.tts.local_mute)
        check("foreign origin refused", await ws_refused(
            s, base + "/ws", sslc, origin="https://evil.example"))
    async with aiohttp.ClientSession() as s:
        async with s.get(base + "/screen", ssl=sslc) as r:
            check("screen without login refused", r.status == 403)
        # Installable app: Chrome wants PNG icons, a service worker, a manifest.
        async with s.get(base + "/manifest.json", ssl=sslc) as r:
            man = await r.json(content_type=None)
            sizes = {i["sizes"] for i in man["icons"] if i["type"] == "image/png"}
            check("manifest has 192+512 PNG icons and an id",
                  {"192x192", "512x512"} <= sizes and man.get("id") == "/")
        for path in ("/icon-192.png", "/icon-512.png", "/icon-512-maskable.png"):
            async with s.get(base + path, ssl=sslc) as r:
                data = await r.read()
                check(f"{path} is a PNG", r.status == 200 and data[:4] == b"\x89PNG")
        async with s.get(base + "/icon-64.png", ssl=sslc) as r:
            check("odd icon size 404s", r.status == 404)
        async with s.get(base + "/sw.js", ssl=sslc) as r:
            body = await r.text()
            check("service worker served as JS with a fetch handler",
                  "javascript" in r.headers.get("Content-Type", "")
                  and '"fetch"' in body)

    # Real-cert address: SNI with the Tailscale name gets that certificate,
    # a bare IP keeps the self-signed one.
    tmp = tempfile.mkdtemp()
    name = "goat.tailtest.ts.net"
    crt, key = make_cert(tmp, name)
    check("fresh cert recognised", ps._cert_fresh(crt, key, name))
    check("wrong-name cert not fresh", not ps._cert_fresh(crt, key, "other.ts.net"))
    srv.use_ts_cert(name, crt, key)
    check("ts url listed first", srv.urls[0] == f"https://{name}:{PORT}")
    check("SNI with ts name -> real cert", name in await served_names(name))
    check("bare IP -> self-signed cert", name not in await served_names(None))
    await srv.stop()


def make_cert(d, name):
    import datetime
    import os
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n)
         .public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=89))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), False)
         .sign(k, hashes.SHA256()))
    crt, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    with open(crt, "wb") as f:
        f.write(c.public_bytes(serialization.Encoding.PEM))
    with open(key, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))
    return crt, key


async def served_names(sni):
    from cryptography import x509
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    _r, w = await asyncio.open_connection("127.0.0.1", PORT, ssl=c,
                                          server_hostname=sni)
    der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
    w.close()
    cert = x509.load_der_x509_certificate(der)
    return cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    sys.exit(1 if fails else 0)
