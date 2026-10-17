"""Phone client smoke test: real HTTPS + WebSocket against a fake GOAT.
Run: py -3.13 test_phone_server.py"""
import asyncio
import json
import ssl
import sys

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

    def emit(self, kind, data=""):
        self.events.append((kind, data))

    def submit_text(self, text):
        self.texts.append(text)
        self.emit("you", text)

    def _lat_start(self):
        pass

    async def _handle_utterance(self, audio, rt=None, gap=None):
        self.audio.append(audio)


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
            await ws.send_str(json.dumps({"type": "live", "on": True}))
            await asyncio.sleep(0.1)
            check("live -> laptop mic+speaker off",
                  app.phone_live and app.tts.local_mute)
            await ws.send_str(json.dumps({"type": "text", "text": "hello goat"}))
            m = json.loads((await ws.receive()).data)
            check("typed text reaches GOAT and echoes",
                  app.texts == ["hello goat"] and m == {"type": "you", "text": "hello goat"})
            pcm = (np.sin(np.arange(16000) / 5) * 8000).astype("<i2").tobytes()
            await ws.send_bytes(pcm)
            await asyncio.sleep(0.2)
            check("phone audio reaches the ear",
                  len(app.audio) == 1 and len(app.audio[0]) == 16000)
            app.tts.on_sentence("Hi there.", np.zeros(800, dtype=np.float32) + 0.25)
            m = json.loads((await ws.receive()).data)
            b = await ws.receive()
            check("GOAT's sentence arrives as text then audio",
                  m["type"] == "say" and m["text"] == "Hi there."
                  and len(b.data) == 1600)
            await ws.send_str(json.dumps({"type": "stop"}))
            m = json.loads((await ws.receive()).data)
            check("tap-to-stop cancels the voice",
                  app.tts.cancelled == 1 and m["type"] == "cancel")
        await asyncio.sleep(0.2)
        check("phone gone -> laptop mic back", not app.phone_live and not app.tts.local_mute)
        check("foreign origin refused", await ws_refused(
            s, base + "/ws", sslc, origin="https://evil.example"))
    await srv.stop()


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    sys.exit(1 if fails else 0)
