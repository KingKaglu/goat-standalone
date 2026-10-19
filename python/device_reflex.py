"""Device commands as COMMANDS: his phone and Windows settings, no model,
no screenshots, no tapping.

His order, 2026-10-19: "when I tell it to change something or open something
on my PC or my phone it needs to be fast and without visuals — done by
commands". The transcript said why it wasn't: every phone order ("open
Messenger on my phone", "set the alarm at 11 on my phone") and every polite
one ("can you close all the tabs for me, please?") missed the reflex lane and
went to the brain — 4-7 s of first tokens, then a shell round trip per step,
and on the phone, taps over whatever he was doing (his 2026-10-18 rule:
phone = intents and commands, NEVER tapping).

Three pieces, all called from reflex.match():

- normalize(): peels the politeness and the device off a sentence —
  "would you mind opening Messenger on my phone?" -> ("open Messenger",
  "phone"). The reflex patterns are anchored, so one "for me, please" used
  to sink the whole match.
- phone(): the phone's own command channel (phone_server /phone/do -> the
  bubble app's accessibility service): open app by name, Android intents
  for alarm/timer/dial/settings/search/maps, global actions (home, back,
  lock, notifications), and volume/torch/media.
- pc(): Windows settings flipped by registry / WinRT / ms-settings: URIs —
  dark mode, Wi-Fi, Bluetooth, settings pages — plus "restart GOAT".

Like reflex.py: a pattern that isn't sure returns None and the brain takes
the turn. Each run returns a short result; "ERROR: …" is spoken as a failure.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---- normalize ------------------------------------------------------------
# The verbs he wraps in "would you mind …ing" — back to the bare order.
_GERUND = {"opening": "open", "closing": "close", "setting": "set",
           "turning": "turn", "putting": "put", "playing": "play",
           "pausing": "pause", "launching": "launch", "starting": "start",
           "locking": "lock", "muting": "mute", "switching": "switch",
           "restarting": "restart", "making": "make", "showing": "show",
           "bringing": "bring", "pulling": "pull", "taking": "take",
           "calling": "call", "dialing": "dial", "dialling": "dial",
           "skipping": "skip", "going": "go", "enabling": "enable",
           "disabling": "disable", "waking": "wake"}
_LEAD_RE = re.compile(
    r"^(?:(?:hey|hi|hello|ok(?:ay)?|alright|all\s+right|so|well|goat|sir|"
    r"please|kindly|thanks|thank\s+you|"
    r"be\s+a\s+(?:lad|dear|pal|mate)\s+and|can\s+you|could\s+you|will\s+you|"
    r"would\s+you(?:\s+(?:mind|please|kindly))?|do\s+me\s+a\s+favou?r(?:\s+and)?|"
    r"i\s+(?:want|need)\s+you\s+to|i'?d\s+like\s+you\s+to|go\s+ahead\s+and|"
    r"just|now|quickly|quick|გთხოვ|ახლა|აბა|კაი|ოკეი|ოქეი)\b[,!.\s]*)+",
    re.IGNORECASE)
_TAIL_RE = re.compile(
    r"(?:[,\s]+(?:for\s+me|please|thanks|thank\s+you|now|right\s+now|"
    r"real\s+quick|quickly|ok(?:ay)?|goat|sir|ჩემთვის|გთხოვ|ახლა))+\s*[.!?]*$",
    re.IGNORECASE)
_PC_RE = re.compile(
    r"\s*\b(?:on|in|from|at)\s+(?:my|the|this)\s+(?:laptop|pc|computer|"
    r"desktop\s+pc|windows)\b|\s*(?:ლეპტოპ|კომპიუტერ|კომპ)\w*",
    re.IGNORECASE)
_PHONE_RE = re.compile(
    r"\s*\b(?:on|in|from|at|with|using)\s+(?:my|the)\s+(?:phone|mobile|cell\s*phone|"
    r"android|samsung)\b|"
    r"\s*\b(?:my|the)\s+(?:phone|mobile)(?:'s)?\b|"
    r"^\s*phone(?:'s)?\s+(?=\w)|"
    r"\s*\bphone(?:'s)?\s+(?=(?:volume|alarm|timer|torch|flash\s*light|"
    r"wi-?fi|bluetooth|settings|notifications|screen\s+off)\b)|"
    r"\s*\b(?:ჩემს\s+|ჩემი\s+)?(?:ტელეფონ|მობილურ)\w*",
    re.IGNORECASE)


def normalize(text: str) -> tuple:
    """(bare order, device) — device is "phone", "pc" or "" (not said)."""
    t = " ".join((text or "").split())
    # Spoken hesitation the ear writes down: "would you mind, uh, putting…"
    t = " ".join(re.sub(r"(?:^|(?<=[\s,]))(?:uh+|um+|erm+|hmm+|ah+)\b[,.]?",
                        " ", t, flags=re.IGNORECASE).split())
    device = ""
    if _PHONE_RE.search(t):
        device = "phone"
        t = _PHONE_RE.sub(" ", t)
    elif _PC_RE.search(t):
        device = "pc"
        t = _PC_RE.sub(" ", t)
    t = " ".join(t.split())
    for _ in range(3):
        before = t
        t = _LEAD_RE.sub("", t).strip()
        t = _TAIL_RE.sub("", t).strip()
        w = t.split(" ", 1)
        if w and w[0].lower() in _GERUND:
            t = _GERUND[w[0].lower()] + (" " + w[1] if len(w) > 1 else "")
        if t == before:
            break
    t = t.strip(" ,")
    return t, device


# ---- the phone ------------------------------------------------------------
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def phone_call(cmd: dict, timeout: float = 12.0) -> str:
    """One command to the phone; the result string the reflex log speaks."""
    try:
        with open(os.path.join(ROOT, ".phone", "token.txt"), encoding="utf-8") as f:
            tok = f.read().strip()
        req = urllib.request.Request(
            "https://127.0.0.1:8797/phone/do", json.dumps(cmd).encode(),
            {"Cookie": "goat_k=" + tok, "Content-Type": "application/json"})
        res = json.load(urllib.request.urlopen(req, context=_CTX, timeout=timeout))
    except Exception as e:  # noqa: BLE001 — server down / no token
        return f"ERROR: phone link: {e}"
    if res.get("ok"):
        extra = res.get("pkg") or res.get("via") or ""
        return f"phone {cmd.get('op')} ok" + (f" ({extra})" if extra else "")
    err = str(res.get("error") or res)
    if err.startswith("unknown op"):
        # Volume/media/torch arrived with the 2026-10-19 build.
        return "ERROR: phone app too old for this (install the new GOAT app)"
    return f"ERROR: phone: {err}"


def _intent(action: str, data: str = "", **kw) -> dict:
    d = {"op": "intent", "action": action}
    if data:
        d["data"] = data
    d.update(kw)
    return d


def _run(cmd: dict):
    return lambda c=cmd: phone_call(c)


def _open_on_phone(name: str):
    """By app label first; a site he names that has no app opens in the
    phone's browser instead of failing."""
    def run():
        r = phone_call({"op": "open", "app": name})
        if r.startswith("ERROR") and "no app" in r:
            import reflex
            site = reflex.SITES.get(name.lower()) or reflex.SITES.get(
                re.sub(r"\s+", "", name.lower()))
            if site:
                return phone_call(_intent("android.intent.action.VIEW", site))
        return r
    return run


_NUMW = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
         "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
_NUMW_ALT = "|".join(sorted(map(re.escape, _NUMW), key=len, reverse=True))


def _num(w: str) -> int:
    w = w.lower()
    return int(w) if w.isdigit() else _NUMW[w]


# "set an alarm at 7", "alarm for 6:30 am", "wake me up at 11 in the morning",
# "set alarm ar 14 today" (his typo, 2026-10-18), "მაღვიძარა 7-ზე".
_ALARM_RE = re.compile(
    r"^(?:(?:set|put|make|create|add)\s+(?:me\s+)?(?:an?\s+|the\s+|my\s+)?alarm|"
    r"wake\s+me(?:\s+up)?|alarm)\s+(?:for|at|ar|to|on)?\s*"
    r"(?P<h>\d{1,2})(?:\s*[:.]\s*(?P<m>\d{2})|\s+(?P<m2>\d{2})(?!\d))?\s*"
    r"(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?|in\s+the\s+morning|in\s+the\s+afternoon|"
    r"in\s+the\s+evening|at\s+night|tonight|o'?clock)?"
    r"(?:\s+(?:today|tomorrow|tonight|in\s+the\s+morning))?"
    r"(?:\s+(?:called|named|labell?ed|for)\s+(?P<label>.{1,40}?))?\s*[.!?]*$|"
    r"^(?:მაღვიძარა|ზარი)\s+(?:დამიყენე\s+|დააყენე\s+)?(?P<kh>\d{1,2})"
    r"(?:\s*[:.]\s*(?P<km>\d{2}))?\s*-?(?:ზე|საათზე)?(?:\s+(?:დამიყენე|დააყენე))?"
    r"\s*[.!?]*$",
    re.IGNORECASE)
_TIMER_RE = re.compile(
    r"^(?:(?:set|start|put|make)\s+(?:me\s+)?(?:an?\s+|the\s+)?)?timer\s+"
    r"(?:for\s+|of\s+)?(?P<n>\d{1,3}|" + _NUMW_ALT + r")\s*"
    r"(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?)\s*[.!?]*$|"
    r"^(?:(?:set|start|put|make)\s+(?:me\s+)?)?(?:an?\s+)?(?P<n2>\d{1,3}|"
    + _NUMW_ALT + r")[\s-]*(?P<u2>seconds?|secs?|minutes?|mins?|hours?|hrs?)"
    r"\s+timer\s*[.!?]*$|"
    r"^(?:(?:set|start)\s+(?:a\s+)?)?timer\s+for\s+(?P<half>half\s+an\s+hour)\s*[.!?]*$",
    re.IGNORECASE)


def alarm_time(m) -> tuple | None:
    """(hour, minute) or None when it is honestly ambiguous ("12 in the
    morning" — he meant noon once, a brain asks)."""
    if m.group("kh"):
        h, mi = int(m.group("kh")), int(m.group("km") or 0)
        return (h, mi) if h < 24 and mi < 60 else None
    h = int(m.group("h"))
    mi = int(m.group("m") or m.group("m2") or 0)
    ap = (m.group("ap") or "").lower().replace(".", "").replace(" ", "")
    if ap.startswith("p") or ap.startswith(("intheafternoon", "intheevening",
                                            "atnight", "tonight")):
        if h < 12:
            h += 12
    elif ap.startswith("a"):
        if h == 12:
            h = 0
    elif ap == "inthemorning" and h == 12:
        return None
    if h > 23 or mi > 59:
        return None
    return h, mi


def _timer_secs(m) -> int | None:
    if m.group("half"):
        return 1800
    n = m.group("n") or m.group("n2")
    u = (m.group("u") or m.group("u2") or "").lower()
    k = _num(n)
    s = k * (3600 if u.startswith("h") else 60 if u.startswith("m") else 1)
    return s if 0 < s <= 24 * 3600 else None


def alarm_or_timer(t: str) -> "object | None":
    """Alarms, timers and the torch belong on the PHONE even unasked —
    the laptop sleeps, the phone is what wakes him (his correction
    2026-10-18: "no, I meant on my phone, not on laptop")."""
    import reflex
    m = _ALARM_RE.match(t)
    if m:
        hm = alarm_time(m)
        if hm is None:
            return None
        h, mi = hm
        label = (m.group("label") or "GOAT").strip(" .,!?")
        return reflex.Reflex(
            "phone", f"alarm {h:02d}:{mi:02d}",
            _run(_intent("android.intent.action.SET_ALARM", extras={
                "android.intent.extra.alarm.HOUR": h,
                "android.intent.extra.alarm.MINUTES": mi,
                "android.intent.extra.alarm.MESSAGE": label,
                "android.intent.extra.alarm.SKIP_UI": True})),
            speak=f"Sir, setting an alarm for {h:02d}:{mi:02d} on your phone.")
    m = _TIMER_RE.match(t)
    if m:
        s = _timer_secs(m)
        if s is None:
            return None
        nice = (f"{s // 3600}-hour" if s % 3600 == 0
                else f"{s // 60}-minute" if s % 60 == 0
                else f"{s}-second")
        return reflex.Reflex(
            "phone", f"timer {s}s",
            _run(_intent("android.intent.action.SET_TIMER", extras={
                "android.intent.extra.alarm.LENGTH": s,
                "android.intent.extra.alarm.MESSAGE": "GOAT",
                "android.intent.extra.alarm.SKIP_UI": True})),
            speak=f"Sir, starting a {nice} timer on your phone.")
    m = _P_TORCH_RE.match(t)            # the laptop has no torch
    if m:
        s = (m.group("s") or m.group("s2") or m.group("s3") or "").lower()
        ka = m.group("ka")
        on = (s == "on") if s else (ka == "ჩართე") if ka else None
        cmd = {"op": "torch"} if on is None else {"op": "torch", "on": on}
        return reflex.Reflex("phone", "torch " + ("toggle" if on is None
                                                  else "on" if on else "off"),
                             _run(cmd))
    return None


_SETTINGS_PAGES = {
    "wifi": "android.settings.WIFI_SETTINGS",
    "wi-fi": "android.settings.WIFI_SETTINGS",
    "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "display": "android.settings.DISPLAY_SETTINGS",
    "sound": "android.settings.SOUND_SETTINGS",
    "battery": "android.intent.action.POWER_USAGE_SUMMARY",
    "location": "android.settings.LOCATION_SOURCE_SETTINGS",
    "apps": "android.settings.APPLICATION_SETTINGS",
    "notifications": "android.settings.ALL_APPS_NOTIFICATION_SETTINGS",
    "airplane": "android.settings.AIRPLANE_MODE_SETTINGS",
    "airplane mode": "android.settings.AIRPLANE_MODE_SETTINGS",
    "accessibility": "android.settings.ACCESSIBILITY_SETTINGS",
    "storage": "android.settings.INTERNAL_STORAGE_SETTINGS",
    "data": "android.settings.DATA_USAGE_SETTINGS",
    "mobile data": "android.settings.DATA_USAGE_SETTINGS",
    "hotspot": "android.settings.TETHER_SETTINGS",
    "date": "android.settings.DATE_SETTINGS",
    "language": "android.settings.LOCALE_SETTINGS",
}
# What he calls an app -> its launcher label (the phone matches labels, and
# a Georgian name or a nickname matches none).
_PHONE_APPS = {
    "fb": "Facebook", "ფეისბუქი": "Facebook", "ფეისბუქ": "Facebook",
    "insta": "Instagram", "ინსტაგრამი": "Instagram", "ინსტა": "Instagram",
    "yt": "YouTube", "იუთუბი": "YouTube", "იუტუბი": "YouTube",
    "მესენჯერი": "Messenger", "მესენჯერ": "Messenger",
    "ვაცაპი": "WhatsApp", "ვოცაპი": "WhatsApp", "whats app": "WhatsApp",
    "ტელეგრამი": "Telegram", "ვაიბერი": "Viber", "გუგლი": "Google",
    "კამერა": "Camera", "გალერეა": "Gallery", "photos": "Gallery",
    "pictures": "Gallery", "სპოტიფაი": "Spotify", "ქრომი": "Chrome",
    "browser": "Chrome", "რუკა": "Maps", "რუკები": "Maps", "google maps": "Maps",
    "კალკულატორი": "Calculator", "calculator app": "Calculator",
    "კალენდარი": "Calendar", "play store": "Play Store", "store": "Play Store",
    "ტიკტოკი": "TikTok", "tik tok": "TikTok", "gmail": "Gmail",
    "ჯიმეილი": "Gmail", "mail": "Gmail", "email": "Gmail",
    "messages": "Messages", "sms": "Messages", "contacts": "Contacts",
    "camo": "Camo", "chat gpt": "ChatGPT", "chatgpt": "ChatGPT",
    "claude": "Claude", "goat": "GOAT",
    "პარამეტრები": "settings", "სეთინგები": "settings",
}
_P_NOT_APP_RE = re.compile(
    r"^(?:me|us|him|her|what|which|how|where|why|it|this|that|these|those|"
    r"something|anything|screen|the\s+screen|my\s+screen)\b", re.IGNORECASE)
_P_OPEN_RE = re.compile(
    r"^(?:open|launch|start|show|bring\s+up|pull\s+up|fire\s+up|go\s+to|"
    r"take\s+me\s+to|switch\s+to|გა?მ?ი?ხსენ\w*|ჩართ\w*|გაუშვ\w*|შედ\w*)\s+"
    r"(?:up\s+)?(?:the\s+|my\s+|a\s+)?(?P<t>.+?)(?:\s+(?:app|application|აპ\w*))?"
    r"\s*[.!?]*$|"
    r"^(?P<t2>.+?)\s+(?:გა?მ?ი?ხსენ\w*|გაუშვ\w*)\s*[.!?]*$",
    re.IGNORECASE)
_P_SETTINGS_RE = re.compile(
    r"^(?P<w>.+?)\s+settings$|^settings(?:\s+for\s+(?P<w2>.+))?$", re.IGNORECASE)
_P_RADIO_RE = re.compile(
    r"^(?:turn|switch)\s+(?P<s>on|off)\s+(?:the\s+)?(?P<r>wi-?fi|bluetooth|"
    r"airplane\s+mode|mobile\s+data|data|hotspot)$|"
    r"^(?:turn|switch)\s+(?:the\s+)?(?P<r2>wi-?fi|bluetooth|airplane\s+mode|"
    r"mobile\s+data|data|hotspot)\s+(?P<s2>on|off)$|"
    r"^(?P<e>enable|disable)\s+(?:the\s+)?(?P<r3>wi-?fi|bluetooth|"
    r"airplane\s+mode|mobile\s+data|data|hotspot)$", re.IGNORECASE)
# Android 10+ lets no app flip these — the panel is the fastest honest route.
_P_PANEL = {"wifi": "android.settings.panel.action.WIFI",
            "wi-fi": "android.settings.panel.action.WIFI",
            "mobile data": "android.settings.panel.action.INTERNET_CONNECTIVITY",
            "data": "android.settings.panel.action.INTERNET_CONNECTIVITY",
            "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
            "airplane mode": "android.settings.AIRPLANE_MODE_SETTINGS",
            "hotspot": "android.settings.TETHER_SETTINGS"}
_P_TORCH_RE = re.compile(
    r"^(?:turn|switch)\s+(?P<s>on|off)\s+(?:the\s+)?(?:flash\s*light|torch|flash)$|"
    r"^(?:turn|switch)\s+(?:the\s+)?(?:flash\s*light|torch|flash)\s+(?P<s2>on|off)$|"
    r"^(?:flash\s*light|torch)(?:\s+(?P<s3>on|off))?$|"
    r"^(?P<ka>ჩართე|გამორთე|ჩაქრე)\s+(?:ფანარ|ლამპ)\w*$", re.IGNORECASE)
_P_DIAL_RE = re.compile(
    r"^(?:call|dial|ring)\s+(?P<n>\+?[\d\s\-]{3,20})$", re.IGNORECASE)
_P_SEARCH_RE = re.compile(
    r"^(?:search(?:\s+for)?|google|look\s+up)\s+(?P<q>.{2,80})$", re.IGNORECASE)
_P_MAPS_RE = re.compile(
    r"^(?:navigate|directions|take\s+me|drive|route)\s+(?:me\s+)?(?:to\s+)?"
    r"(?P<q>.{2,80})$", re.IGNORECASE)
_P_GLOBAL = [
    (re.compile(r"^(?:go\s+)?home(?:\s+screen)?$|^(?:go\s+to\s+)?(?:the\s+)?"
                r"home\s+screen$", re.IGNORECASE), "home", "home screen"),
    (re.compile(r"^(?:go\s+)?back$", re.IGNORECASE), "back", "back"),
    (re.compile(r"^(?:show\s+|open\s+|pull\s+down\s+)?(?:the\s+|my\s+)?"
                r"notifications?(?:\s+(?:panel|shade))?$", re.IGNORECASE),
     "notifications", "notifications"),
    (re.compile(r"^(?:show\s+|open\s+)?(?:the\s+|my\s+)?(?:recent\s+apps|"
                r"recents|app\s+switcher)$", re.IGNORECASE), "recents", "recent apps"),
    (re.compile(r"^(?:show\s+|open\s+|pull\s+down\s+)?(?:the\s+)?quick\s+"
                r"settings$", re.IGNORECASE), "quick_settings", "quick settings"),
    (re.compile(r"^(?:lock(?:\s+(?:it|the\s+screen|screen))?|"
                r"(?:დაბლოკ|ჩაკეტ)\w*)$", re.IGNORECASE), "lock", "lock"),
]


def phone(t: str) -> "object | None":
    """Match a bare order (already normalized, phone marker removed)."""
    import reflex
    t = re.sub(r"\s*,\s*", " ", t).strip(" .!?")
    if not t:
        return None
    got = alarm_or_timer(t)
    if got is not None:
        return got
    for rx, op, what in _P_GLOBAL:
        if rx.match(t):
            return reflex.Reflex("phone", what, _run({"op": op}))

    # Volume / media / torch: the bubble app's own ops (AudioManager,
    # CameraManager) — never the on-screen slider.
    if reflex._MUTE_RE.match(t):
        act = "unmute" if re.search(r"unmute", t, re.I) else "mute"
        return reflex.Reflex("phone", act, _run({"op": "volume", "action": act}))
    if reflex._VOL_UP_RE.match(t) or re.match(r"^volume\s+up$|^(?:turn\s+)?up\s+"
                                              r"the\s+volume$", t, re.I):
        return reflex.Reflex("phone", "volume up", _run({"op": "volume", "action": "up"}))
    if reflex._VOL_DOWN_RE.match(t) or re.match(r"^(?:turn\s+)?down\s+the\s+volume$",
                                                t, re.I):
        return reflex.Reflex("phone", "volume down",
                             _run({"op": "volume", "action": "down"}))
    m = reflex._VOL_SET_RE.match(t)
    if m:
        pct = max(0, min(100, int(m.group("p"))))
        return reflex.Reflex("phone", f"volume {pct}%",
                             _run({"op": "volume", "action": "set", "percent": pct}))
    if reflex._NEXT_RE.match(t):
        return reflex.Reflex("phone", "next", _run({"op": "media", "action": "next"}))
    if reflex._PREV_RE.match(t):
        return reflex.Reflex("phone", "previous",
                             _run({"op": "media", "action": "previous"}))
    if reflex._PLAY_RE.match(t) or re.match(r"^(?:pause|play|resume|stop)\s+(?:the\s+)?"
                                            r"(?:music|song|video|it)$", t, re.I):
        return reflex.Reflex("phone", "play/pause",
                             _run({"op": "media", "action": "play_pause"}))
    m = _P_RADIO_RE.match(t)
    if m:
        r = (m.group("r") or m.group("r2") or m.group("r3")).lower()
        r = re.sub(r"\s+", " ", r)
        return reflex.Reflex(
            "phone", f"{r} panel", _run(_intent(_P_PANEL.get(r, "android.settings.SETTINGS"))),
            speak=f"Sir, opening the {r} switch on your phone. Android won't let me flip it.")
    m = _P_DIAL_RE.match(t)
    if m:
        num = re.sub(r"[\s\-]", "", m.group("n"))
        return reflex.Reflex("phone", f"dial {num}",
                             _run(_intent("android.intent.action.DIAL", "tel:" + num)))
    m = _P_MAPS_RE.match(t)
    if m:
        q = m.group("q").strip(" .!?")
        return reflex.Reflex("phone", f"navigate {q}", _run(_intent(
            "android.intent.action.VIEW",
            "google.navigation:q=" + urllib.parse.quote(q))))
    m = _P_SEARCH_RE.match(t)
    if m:
        q = m.group("q").strip(" .!?")
        return reflex.Reflex("phone", f"search {q}", _run(_intent(
            "android.intent.action.VIEW",
            "https://www.google.com/search?q=" + urllib.parse.quote(q))))

    m = _P_OPEN_RE.match(t)
    if m:
        name = (m.group("t") or m.group("t2") or "").strip(" .,!?\"'")
        name = re.sub(r"(\w)-[ა-ჿ]+$", r"\1", name)      # "Facebook-ი"
        low = name.lower()
        if not name or len(name) > 40 or reflex._WORKISH_RE.match(name):
            return None
        # "show me my phone screen" is a request to LOOK, not an app called
        # "me screen" — anything pointing or describing goes to a brain.
        if _P_NOT_APP_RE.match(low):
            return None
        name = _PHONE_APPS.get(low, name)
        low = name.lower()
        s = _P_SETTINGS_RE.match(low)
        if s:
            page = (s.group("w") or s.group("w2") or "").strip()
            act = _SETTINGS_PAGES.get(page, "android.settings.SETTINGS" if not page
                                      else None)
            if act is None:
                return None
            return reflex.Reflex("phone", f"settings {page}".strip(),
                                 _run(_intent(act)))
        if low in ("alarms", "my alarms", "the alarms", "alarm", "clock"):
            return reflex.Reflex("phone", "alarms",
                                 _run(_intent("android.intent.action.SHOW_ALARMS")))
        if low in ("dialer", "phone app", "dial pad", "keypad"):
            return reflex.Reflex("phone", "dialer",
                                 _run(_intent("android.intent.action.DIAL")))
        if low.startswith(("http://", "https://")) or reflex._URLISH_RE.match(low):
            url = low if low.startswith("http") else "https://" + low
            return reflex.Reflex("phone", url, _run(_intent(
                "android.intent.action.VIEW", url)))
        return reflex.Reflex("phone", f"open {name}", _open_on_phone(name))
    return None


# ---- Windows settings ----------------------------------------------------
def _ps(script: str, timeout: float = 15.0) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", script], capture_output=True, text=True,
                       timeout=timeout, creationflags=_NO_WINDOW)
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return "ERROR: " + ((r.stderr or out).strip().splitlines() or ["powershell failed"])[-1]
    return out


def _theme(dark: bool) -> str:
    import winreg
    v = 0 if dark else 1
    key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "AppsUseLightTheme", 0, winreg.REG_DWORD, v)
        winreg.SetValueEx(k, "SystemUsesLightTheme", 0, winreg.REG_DWORD, v)
    # Tell running apps and the taskbar to repaint now, not at next logon.
    # Off-thread: one hung window held the broadcast 2.3 s (measured
    # 2026-10-19), and the setting itself is already written.
    import ctypes
    import threading

    def poke():
        res = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "ImmersiveColorSet",
                                                 0x0002, 2000, ctypes.byref(res))
    threading.Thread(target=poke, daemon=True).start()
    return "dark mode" if dark else "light mode"


_RADIO_PS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$m = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) { $x = $m.MakeGenericMethod($t).Invoke($null, @($op)); $x.Wait(-1) | Out-Null; $x.Result }
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$r = (Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])) | ? { $_.Kind -eq '__KIND__' } | Select-Object -First 1
if (-not $r) { Write-Error 'no __KIND__ radio'; exit 1 }
$s = Await ($r.SetStateAsync('__STATE__')) ([Windows.Devices.Radios.RadioAccessStatus])
"$($r.Kind) $s"
"""


def _radio(kind: str, on: bool) -> str:
    """Wi-Fi / Bluetooth through Windows.Devices.Radios — the same switch the
    action center flips, no admin, ~0.4 s."""
    out = _ps(_RADIO_PS.replace("__KIND__", kind).replace("__STATE__",
                                                          "On" if on else "Off"))
    if out.startswith("ERROR") or "Allowed" not in out:
        return out if out.startswith("ERROR") else f"ERROR: {out or 'radio refused'}"
    return f"{kind} {'on' if on else 'off'}"


def _restart_goat() -> str:
    """Same detached launch the brain's RESTART rule uses: through WMI, so the
    helper is not in GOAT's kill-on-close job and survives the app dying.
    restart-goat.ps1 runs the preflight itself and leaves GOAT alone on FAIL."""
    script = os.path.join(HERE, "restart-goat.ps1").replace("\\", "/")
    return _ps("Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
               "-Arguments @{ CommandLine = 'powershell -NoProfile -ExecutionPolicy "
               f"Bypass -File {script}' }} | Out-Null; 'restart helper launched'")


_MS_SETTINGS = {
    "wifi": "network-wifi", "wi-fi": "network-wifi", "network": "network",
    "internet": "network", "bluetooth": "bluetooth", "devices": "bluetooth",
    "display": "display", "screen": "display", "sound": "sound",
    "audio": "sound", "battery": "batterysaver", "power": "powersleep",
    "night light": "nightlight", "apps": "appsfeatures", "startup": "startupapps",
    "startup apps": "startupapps", "storage": "storagesense",
    "windows update": "windowsupdate", "update": "windowsupdate",
    "updates": "windowsupdate", "privacy": "privacy",
    "notifications": "notifications", "personalization": "personalization",
    "background": "personalization-background", "wallpaper": "personalization-background",
    "colors": "personalization-colors", "themes": "themes",
    "mouse": "mousetouchpad", "touchpad": "devices-touchpad",
    "keyboard": "typing", "language": "regionlanguage", "time": "dateandtime",
    "date": "dateandtime", "vpn": "network-vpn", "hotspot": "network-mobilehotspot",
    "airplane mode": "network-airplanemode", "focus": "quiethours",
    "do not disturb": "quiethours", "default apps": "defaultapps",
    "accounts": "yourinfo", "system": "about", "about": "about",
    "printers": "printers", "camera": "privacy-webcam", "microphone": "privacy-microphone",
    "graphics": "display-advancedgraphics", "multitasking": "multitasking",
}
_W_SETTINGS_RE = re.compile(
    r"^(?:open|show|go\s+to|bring\s+up|pull\s+up|take\s+me\s+to)\s+(?:the\s+|my\s+)?"
    r"(?:(?P<a>[\w\s-]{2,25}?)\s+settings|settings\s+(?:for|of)\s+(?P<b>[\w\s-]{2,25}))"
    r"\s*[.!?]*$", re.IGNORECASE)
_W_THEME_RE = re.compile(
    r"^(?:(?:turn|switch)\s+(?P<s>on|off)\s+(?:the\s+)?dark\s+(?:mode|theme)|"
    r"(?:turn|switch)\s+(?:the\s+)?dark\s+(?:mode|theme)\s+(?P<s2>on|off)|"
    r"(?P<e>enable|disable)\s+(?:the\s+)?dark\s+(?:mode|theme)|"
    r"(?:switch|change|go|set\s+it|put\s+it|set\s+(?:the\s+)?theme)\s+(?:back\s+)?"
    r"to\s+(?P<to>dark|light)(?:\s+(?:mode|theme))?|"
    r"(?:use\s+|make\s+it\s+)?(?P<plain>dark|light)\s+(?:mode|theme)(?:\s+on)?|"
    r"(?P<ka>მუქი|ღია|ბნელი|თეთრი)\s+(?:რეჟიმ|თემ)\w*(?:\s+(?:ჩართე|დააყენე|გადართე))?)"
    r"\s*[.!?]*$", re.IGNORECASE)
_W_RADIO_RE = re.compile(
    r"^(?:(?:turn|switch)\s+(?P<s>on|off)\s+(?:the\s+)?(?P<r>wi-?fi|bluetooth|bt)|"
    r"(?:turn|switch)\s+(?:the\s+)?(?P<r2>wi-?fi|bluetooth|bt)\s+(?P<s2>on|off)|"
    r"(?P<e>enable|disable|disconnect|reconnect|connect)\s+(?:the\s+|from\s+)?"
    r"(?P<r3>wi-?fi|bluetooth|bt)|"
    r"(?P<ka>ჩართე|გამორთე|გათიშე)\s+(?P<kr>ვაიფაი\w*|ვაი-ფაი\w*|ინტერნეტ\w*|"
    r"ბლუთუზ\w*|ბლუთუს\w*)|"
    r"(?P<kr2>ვაიფაი\w*|ვაი-ფაი\w*|ბლუთუზ\w*|ბლუთუს\w*)\s+(?P<ka2>ჩართე|გამორთე|გათიშე))"
    r"\s*[.!?]*$", re.IGNORECASE)
_W_RESTART_RE = re.compile(
    # Bare "reload" is the page (reflex _REFRESH_RE) and bare "reboot" is the
    # machine — only a bare "restart" means me.
    r"^(?:(?:re-?start|relaunch)(?:\s+(?:goat|yourself|the\s+goat|goat\s+app))?|"
    r"(?:reboot|reload)\s+(?:goat|yourself)|goat\s+restart(?:\s+yourself)?|"
    r"გადაიტვირთე|გადატვირთე\s+(?:გოატ\w*|თავი)|დარესტარტდი)\s*[.!?]*$",
    re.IGNORECASE)


def pc(t: str, raw: str = "") -> "object | None":
    """Windows-side settings and GOAT's own restart. `t` is normalized."""
    import reflex
    t = re.sub(r"\s*,\s*", " ", t).strip()
    if _W_RESTART_RE.match(t):
        # A bare "restart" is GOAT — every one in the transcript was. The
        # machine itself ("restart the computer") never matches: a brain
        # confirms that one.
        return reflex.Reflex("restart", "GOAT", _restart_goat,
                             speak="Sir, restarting. Back in a moment.")
    m = _W_THEME_RE.match(t)
    if m:
        s = (m.group("s") or m.group("s2") or "").lower()
        e = (m.group("e") or "").lower()
        to = (m.group("to") or m.group("plain") or "").lower()
        ka = m.group("ka") or ""
        if s:
            dark = s == "on"
        elif e:
            dark = e == "enable"
        elif to:
            dark = to == "dark"
        else:
            dark = ka in ("მუქი", "ბნელი")
        return reflex.Reflex("setting", "dark mode" if dark else "light mode",
                             lambda d=dark: _theme(d))
    m = _W_RADIO_RE.match(t)
    if m:
        r = (m.group("r") or m.group("r2") or m.group("r3") or m.group("kr")
             or m.group("kr2") or "").lower()
        kind = "Bluetooth" if r.startswith(("b", "ბ")) else "WiFi"
        s = (m.group("s") or m.group("s2") or "").lower()
        e = (m.group("e") or "").lower()
        ka = m.group("ka") or m.group("ka2") or ""
        on = (s == "on") if s else (e in ("enable", "connect", "reconnect")) if e \
            else ka == "ჩართე"
        if kind == "WiFi" and not on:
            # Wi-Fi off cuts my cloud ear and his phone link — he may not be
            # able to SAY "turn it back on". Ask once; his yes runs it.
            import close_guard

            def hold():
                close_guard.ask("Wi-Fi off", run=lambda: _radio("WiFi", False),
                                no_line="Sir, Wi-Fi stays on.")
                return "asked first"
            return reflex.Reflex(
                "confirm", "wifi off?", hold,
                speak="Sir, that cuts my hearing and your phone link. Turn Wi-Fi off anyway?")
        return reflex.Reflex("setting", f"{'wifi' if kind == 'WiFi' else 'bluetooth'} "
                             f"{'on' if on else 'off'}", lambda k=kind, o=on: _radio(k, o))
    m = _W_SETTINGS_RE.match(t)
    if m:
        page = re.sub(r"\s+", " ", (m.group("a") or m.group("b") or "").lower()).strip()
        page = re.sub(r"^(?:the|my|windows)\s+", "", page)
        uri = _MS_SETTINGS.get(page)
        if uri:
            return reflex.Reflex("open_app", f"{page} settings",
                                 lambda u=uri: _start("ms-settings:" + u))
    return None


def _start(uri: str) -> str:
    try:
        os.startfile(uri)  # noqa: S606
        return f"opened {uri}"
    except OSError as e:
        return f"ERROR: {e}"
