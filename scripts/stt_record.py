"""Record your own benchmark clips for scripts/stt_bench.py.

Shows one sentence at a time; you read it aloud, it saves NN.wav (16 kHz mono)
and NN.txt (the sentence) side by side. Nothing leaves the laptop.

    py -3.13 scripts\\stt_record.py                 # laptop mic, the built-in sentences
    py -3.13 scripts\\stt_record.py --prompts my.txt  # your own sentences, one per line
    py -3.13 scripts\\stt_record.py --tag phone       # second pass, files named phone-NN

Keys: Enter = start, Enter again = stop. r = redo the last one, s = skip, q = quit.
If you said something different from the text, edit the .txt afterwards: the
reference must be what was actually said.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("GOAT_ROOT") or os.path.dirname(HERE)
SR = 16000

# Everyday requests in the way they are spoken to an assistant: plain Georgian,
# Georgian with English product names, and a few English ones.
PROMPTS = [
    "გამარჯობა, როგორ ხარ დღეს?",
    "რომელი საათია ახლა?",
    "დღეს რა ამინდია თბილისში?",
    "ხვალ დილის რვა საათზე გამაღვიძე.",
    "გახსენი Chrome და მოძებნე უახლესი ამბები.",
    "YouTube-ზე ჩამირთე მშვიდი მუსიკა.",
    "ხმას ცოტა დაუწიე, ძალიან ხმამაღალია.",
    "დამიწერე მოკლე წერილი და Gmail-ით გააგზავნე.",
    "რა შეხვედრები მაქვს ხვალ კალენდარში?",
    "შემახსენე, რომ საღამოს დედას დავურეკო.",
    "ეს ფაილი Downloads საქაღალდეში გადაიტანე.",
    "გააკეთე screenshot და desktop-ზე შეინახე.",
    "რამდენია ასი დოლარი ლარებში?",
    "მითხარი რამე საინტერესო საქართველოს ისტორიაზე.",
    "გაჩერდი, ეს არ მინდოდა.",
    "კიდევ ერთხელ გაიმეორე, ვერ გავიგე.",
    "გახსენი VS Code და ბოლო პროექტი ჩატვირთე.",
    "ეს კოდი გაუშვი და მითხარი, რა error-ს აგდებს.",
    "git-ში ცვლილებები დააკომიტე და push გააკეთე.",
    "ლეპტოპი ათ წუთში გამორთე.",
    "WhatsApp-ზე მომწერა ვინმემ?",
    "ბატარეა რამდენ პროცენტზეა?",
    "თარგმნე ინგლისურად: დღეს ძალიან დაკავებული ვარ.",
    "Open the browser and search for flights to Berlin.",
    "What is on my calendar for tomorrow?",
    "გმადლობ, დღეისთვის საკმარისია.",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "workspace", "stt-bench"))
    ap.add_argument("--prompts", help="text file, one sentence per line")
    ap.add_argument("--tag", default="", help="file name prefix, e.g. phone")
    ap.add_argument("--device", default=None, help="input device index or name")
    ap.add_argument("--list-devices", action="store_true")
    a = ap.parse_args()

    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    if a.list_devices:
        print(sd.query_devices())
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    prompts = PROMPTS
    if a.prompts:
        with open(a.prompts, encoding="utf-8") as f:
            prompts = [ln.strip() for ln in f if ln.strip()]
    os.makedirs(a.out, exist_ok=True)
    dev = a.device
    if dev is not None and str(dev).isdigit():
        dev = int(dev)
    pre = (a.tag + "-") if a.tag else ""

    def record():
        chunks = []

        def cb(indata, frames, t, status):
            chunks.append(indata.copy())

        with sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                            device=dev, callback=cb):
            input("   recording... Enter to stop ")
        return np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, "float32")

    i = 0
    while i < len(prompts):
        name = "%s%02d" % (pre, i + 1)
        wav = os.path.join(a.out, name + ".wav")
        have = " (already recorded)" if os.path.exists(wav) else ""
        print("\n[%d/%d]%s\n   %s" % (i + 1, len(prompts), have, prompts[i]))
        k = input("   Enter = record, s = skip, r = redo previous, q = quit: ").strip().lower()
        if k == "q":
            break
        if k == "s":
            i += 1
            continue
        if k == "r":
            i = max(0, i - 1)
            continue
        audio = record()
        secs = len(audio) / SR
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        if secs < 0.5 or peak < 0.01:
            print("   too short or silent (%.1f s, peak %.3f): not saved, try again" % (secs, peak))
            continue
        sf.write(wav, audio, SR, subtype="PCM_16")
        with open(os.path.join(a.out, name + ".txt"), "w", encoding="utf-8") as f:
            f.write(prompts[i] + "\n")
        print("   saved %s.wav  %.1f s  peak %.2f" % (name, secs, peak))
        i += 1
    n = len([f for f in os.listdir(a.out) if f.lower().endswith(".wav")])
    print("\n%d clips in %s" % (n, a.out))


if __name__ == "__main__":
    main()
