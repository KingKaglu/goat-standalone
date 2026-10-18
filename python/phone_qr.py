"""Opens a QR code that downloads the GOAT phone app (/goat.apk?k=token). Scan with the phone camera."""
import os, sys, webbrowser
import segno
root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".phone")
urls = [l.strip() for l in open(os.path.join(root, "url.txt"), encoding="utf-8") if l.strip().startswith("https://")]
tok = open(os.path.join(root, "token.txt"), encoding="utf-8").read().strip()
pick = next((u for u in urls if "://100." in u), urls[0])
link = f"{pick}/goat.apk?k={tok}"
out = os.path.join(root, "apk-qr.png")
segno.make(link, error="m").save(out, scale=12, border=3)
print(link)
if "--open" in sys.argv: os.startfile(out)
