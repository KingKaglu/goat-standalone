"""Phone pairing helper. Every QR carries a ONE-TIME code (10 minutes, one
use), never the master token, so photos of it and browser history stay
harmless (2026-11-06 audit S3).

  py -3.13 phone_qr.py [--open]          QR: download the bubble app (/goat.apk)
  py -3.13 phone_qr.py login [--open]    QR: log the phone's browser in
  py -3.13 phone_qr.py sessions          list browser logins
  py -3.13 phone_qr.py logout-all        end every browser login now
                                         (the bubble app keeps working: it uses
                                         the master token in .phone/token.txt)
  py -3.13 phone_qr.py lan               which networks may reach GOAT
  py -3.13 phone_qr.py trust-lan [NAME]  trust a Wi-Fi by name (default: the one
                                         this laptop is on now). Only for HOME
                                         networks; marking it Private in Windows
                                         does the same without a list.
  py -3.13 phone_qr.py untrust-lan NAME
Tailscale always works. A live GOAT picks up a change within a minute.
"""
import datetime
import os
import sys

import phone_server as ps


def _base() -> str:
    with open(os.path.join(ps.DIR, "url.txt"), encoding="utf-8") as f:
        urls = [ln.strip() for ln in f if ln.strip().startswith("https://")]
    return next((u for u in urls if ".ts.net" in u),
                next((u for u in urls if "://100." in u), urls[0]))


def _qr(link: str, name: str, open_it: bool) -> None:
    import segno
    out = os.path.join(ps.DIR, name)
    segno.make(link, error="m").save(out, scale=12, border=3)
    print(f"{out}\n(one use, valid {ps.PAIR_TTL_S // 60} minutes)")
    if open_it:
        os.startfile(out)


def main(argv: list) -> int:
    cmd = next((a for a in argv[1:] if not a.startswith("--")), "apk")
    open_it = "--open" in argv
    if cmd == "apk":
        _qr(f"{_base()}/goat.apk?k={ps.make_pair_code()}", "apk-qr2.png", open_it)
    elif cmd == "login":
        _qr(f"{_base()}/?k={ps.make_pair_code()}", "login-qr.png", open_it)
    elif cmd == "sessions":
        rows = ps.Sessions(os.path.join(ps.DIR, "sessions.json")).rows()
        for e in rows:
            seen = datetime.datetime.fromtimestamp(e.get("seen", 0))
            print(f"last used {seen:%Y-%m-%d %H:%M}  {e.get('via', '')}")
        print(f"{len(rows)} browser login(s)")
    elif cmd == "logout-all":
        n = ps.Sessions(os.path.join(ps.DIR, "sessions.json")).clear()
        print(f"ended {n} browser login(s); phones must scan a new login QR")
    elif cmd in ("lan", "trust-lan", "untrust-lan"):
        gate = ps.LanGate(os.path.join(ps.DIR, "lan.json"))
        gate.refresh()
        args = [a for a in argv[2:] if not a.startswith("--")]
        if cmd == "trust-lan":
            names = args or sorted({name for net, alias, name, cat in gate._nets
                                    if name and alias.lower() != "tailscale"
                                    and net.version == 4 and not net.is_loopback})
            print("trusted:", ", ".join(sorted(ps.trust_lan(names))) or "(none)")
        elif cmd == "untrust-lan":
            left = gate.trusted() - set(args)
            with open(gate.path, "w", encoding="utf-8") as f:
                import json
                json.dump({"trusted": sorted(left)}, f, ensure_ascii=False)
            print("trusted:", ", ".join(sorted(left)) or "(none)")
        for row in gate.describe():
            print(row)
        print(f"mode: {gate.mode} (GOAT_PHONE_LAN)")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
