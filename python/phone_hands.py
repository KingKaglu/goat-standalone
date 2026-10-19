"""GOAT's hands on Giorgi's phone. Usage:
  python phone_hands.py screen | shot | apps | back | home | recents | notifications
  python phone_hands.py tap X Y | hold X Y | swipe X1 Y1 X2 Y2 | scroll [up]
  python phone_hands.py alarm HH MM [label] | timer SECONDS [label] | dial NUMBER | sms NUMBER "text"
  python phone_hands.py url URL | maps "place" | search "query" | settings [wifi|bluetooth|display|sound|battery|apps|location]
  python phone_hands.py intent '{"action":..,"data":..,"package":..,"extras":{..}}'  (any Android intent, instant)
  python phone_hands.py tap_text "Send" | type "hello" [submit] | open "WhatsApp"
  python phone_hands.py volume up|down|mute|unmute|set 40 | media play_pause|next|previous | torch [on|off]
`screen` lists on-screen text with tap coordinates; `shot` saves a screenshot to
workspace/phone-uploads/phone-shot.jpg. Needs the GOAT app's accessibility
service switched on (Settings > Accessibility > GOAT)."""
import base64, json, ssl, sys, urllib.request, urllib.parse
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tok = (ROOT / ".phone" / "token.txt").read_text().strip()
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def call(cmd):
    req = urllib.request.Request("https://127.0.0.1:8797/phone/do",
                                 json.dumps(cmd).encode(),
                                 {"Cookie": "goat_k=" + tok, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, context=ctx, timeout=30))


def _i(action, data=None, **kw):
    d = {"op": "intent", "action": action}
    if data: d["data"] = data
    d.update(kw); return d


def _q(x): return urllib.parse.quote(x)


SETS = {"wifi": "WIFI", "bluetooth": "BLUETOOTH", "display": "DISPLAY", "sound": "SOUND", "battery": "BATTERY_SAVER",
        "apps": "APPLICATION", "location": "LOCATION_SOURCE", "airplane": "AIRPLANE_MODE", "data": "DATA_ROAMING"}
SHORT = {
    "alarm": lambda r: _i("android.intent.action.SET_ALARM", extras={
        "android.intent.extra.alarm.HOUR": int(r[0]), "android.intent.extra.alarm.MINUTES": int(r[1]),
        "android.intent.extra.alarm.MESSAGE": r[2] if len(r) > 2 else "GOAT",
        "android.intent.extra.alarm.SKIP_UI": True}),
    "timer": lambda r: _i("android.intent.action.SET_TIMER", extras={
        "android.intent.extra.alarm.LENGTH": int(r[0]),
        "android.intent.extra.alarm.MESSAGE": r[1] if len(r) > 1 else "GOAT",
        "android.intent.extra.alarm.SKIP_UI": True}),
    "dial": lambda r: _i("android.intent.action.DIAL", "tel:" + r[0]),
    "sms": lambda r: _i("android.intent.action.SENDTO", "smsto:" + r[0], extras={"sms_body": r[1] if len(r) > 1 else ""}),
    "url": lambda r: _i("android.intent.action.VIEW", r[0]),
    "maps": lambda r: _i("android.intent.action.VIEW", "geo:0,0?q=" + _q(r[0])),
    "search": lambda r: _i("android.intent.action.VIEW", "https://www.google.com/search?q=" + _q(r[0])),
    "settings": lambda r: _i("android.settings." + (SETS[r[0]] + "_SETTINGS" if r and r[0] in SETS else "SETTINGS")),
    "intent": lambda r: dict(json.loads(r[0]), op="intent"),
}


def main(a):
    if not a:
        print(__doc__); return
    op, r = a[0], a[1:]
    c = {"op": op}
    if op in ("tap", "hold"): c.update(x=float(r[0]), y=float(r[1]))
    elif op == "swipe": c.update(x1=float(r[0]), y1=float(r[1]), x2=float(r[2]), y2=float(r[3]))
    elif op == "scroll": c["up"] = bool(r and r[0] == "up")
    elif op == "tap_text": c["text"] = r[0]
    elif op == "type": c.update(text=r[0], submit=len(r) > 1 and r[1] == "submit")
    elif op == "open": c["app"] = r[0]
    elif op == "volume": c.update(action=r[0] if r else "up", percent=int(r[1]) if len(r) > 1 else 50)
    elif op == "media": c["action"] = r[0] if r else "play_pause"
    elif op == "torch":
        if r: c["on"] = r[0] == "on"
    elif op in SHORT:
        c = SHORT[op](r)
    res = call(c)
    if op == "shot" and res.get("jpeg"):
        p = ROOT / "workspace" / "phone-uploads" / "phone-shot.jpg"
        p.write_bytes(base64.b64decode(res.pop("jpeg")))
        res["saved"] = str(p)
    if op == "screen" and res.get("ok"):
        print(f"app={res['app']} {res['w']}x{res['h']}")
        for i in res["items"]:
            print(f"{i['x']},{i['y']}\t{'[edit] ' if i.get('edit') else ''}{'*' if i.get('c') else ' '} {i.get('t', '')}")
        return
    print(json.dumps(res, ensure_ascii=False)[:3000])


main(sys.argv[1:])
