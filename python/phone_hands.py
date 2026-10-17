"""GOAT's hands on Giorgi's phone. Usage:
  python phone_hands.py screen | shot | apps | back | home | recents | notifications
  python phone_hands.py tap X Y | hold X Y | swipe X1 Y1 X2 Y2 | scroll [up]
  python phone_hands.py tap_text "Send" | type "hello" [submit] | open "WhatsApp"
`screen` lists on-screen text with tap coordinates; `shot` saves a screenshot to
workspace/phone-uploads/phone-shot.jpg. Needs the GOAT app's accessibility
service switched on (Settings > Accessibility > GOAT)."""
import base64, json, ssl, sys, urllib.request
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
