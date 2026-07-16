"""GOAT's face: a native, frameless, fullscreen Qt window. No browser.

Run:  cd C:/Users/user/goat-standalone/python && python ui_qt.py

Design (v5 — "the instrument, not the spaceship"):
Every AI-generated assistant UI is the same cyan-on-black cockpit: orbs,
hex grids, fake telemetry. This is deliberately the opposite — the design
language of a beautiful instrument sitting in a dark room:

- Warm near-black. One accent (amber). Paper-white type. No boxes, no
  borders, no panels, no glow-for-glow's-sake.
- ONE living element: a thin horizontal string of light stretched across
  the screen. It is GOAT's presence. Flat and breathing when idle; ripples
  with Giorgi's voice while listening; rolls in slow smooth waves while
  GOAT speaks (driven by the real speaker envelope); shivers finely while
  thinking.
- Below the string, the conversation is pure typography: his words small
  and quiet in amber-gray, GOAT's answer in large light editorial type.
  Older exchanges dim and stack upward like a page you can scroll.
- Tool use is a single quiet gray line ("· read — STATE.md"), not chips.
- Voice-first, typing first-class: a bare underline field sits quietly at
  the bottom, always there. Ctrl+K focuses it; Esc clears it (then Esc
  leaves fullscreen).
- Themes: four rooms for the same instrument (ember / paper / phosphor /
  graphite), each one accent, chosen from the top bar or Ctrl+T, persisted
  in ui-config.json.
- Settings drawer (⚙ or Ctrl+,): theme, text size, voice on/off + level,
  wake word, mic mute, new chat / restart — every switch typographic, saved
  instantly to ui-config.json, engine flags applied live via bind_engine().
- Footer is one small line of plain words: state · model · uptime.

Threading: Qt owns the main thread; GoatApp's asyncio loop runs on a
daemon thread. Events cross via a Signal; typed input crosses back via
run_coroutine_threadsafe inside submit_text.
"""
import ctypes
import ctypes.wintypes
import json
import math
import os
import random
import sys
import threading
import time

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
import subprocess

from goat_paths import GOAT_ROOT

ICON = os.path.join(GOAT_ROOT, "goat.ico")
INBOX = os.path.join(GOAT_ROOT, "inbox")  # pasted images land here
UI_CONFIG = os.path.join(GOAT_ROOT, "ui-config.json")

# ---- themes ----
# Same instrument, different rooms. Each theme keeps the design law:
# one accent, quiet type, no boxes. Values are the full palette a theme
# needs — nothing is derived at runtime so each can be hand-tuned.
# Brightened 2026-07-11 (Giorgi: "UI colours look little dark") — dark
# themes lifted a full step: backgrounds up from near-black, dim/faint
# raised so secondary text is READABLE, not archaeological.
THEMES = {
    "ember": {          # the original: warm dark, amber string
        "bg_top": "#1a1713", "bg_bot": "#14110e",
        "paper": "#f4ede0", "dim": "#948d7e", "faint": "#5c5649",
        "accent": "#ffb35e", "string_base": "#948d7e",
        "you_old": "#c09263", "reply_old": "#aca395", "sel": "#5a4429",
    },
    "paper": {          # daylight: warm paper, ink type, vermilion accent
        "bg_top": "#f6f1e7", "bg_bot": "#efe9db",
        "paper": "#1e1b16", "dim": "#7a7365", "faint": "#a29a86",
        "accent": "#c74a24", "string_base": "#7a7365",
        "you_old": "#a05a3c", "reply_old": "#5f5a50", "sel": "#f0c9b8",
    },
    "phosphor": {       # night instrument: green-black, phosphor trace
        "bg_top": "#101812", "bg_bot": "#0c120d",
        "paper": "#e2f0e4", "dim": "#82997f", "faint": "#4a5f50",
        "accent": "#66f096", "string_base": "#82997f",
        "you_old": "#6aae80", "reply_old": "#9db3a1", "sel": "#265a38",
    },
    "graphite": {       # mono: near-black, white-hot string
        "bg_top": "#18181b", "bg_bot": "#121214",
        "paper": "#f0f0f2", "dim": "#94949b", "faint": "#5a5a63",
        "accent": "#ffffff", "string_base": "#94949b",
        "you_old": "#b6b6bc", "reply_old": "#a8a8af", "sel": "#44444f",
    },
}
THEME_ORDER = ["ember", "paper", "phosphor", "graphite"]


# Reply type sizes: the current answer's pt size (older lines stay put).
TEXT_SIZES = {"small": 24, "normal": 32, "large": 40}
# Manual brain roster (his order 2026-07-17): three independent roles he sets
# by hand from the drawer — no auto-routing, no escalation. Values are the
# display names the engine (goat_app) understands directly.
#   talking brain  — the middle lane, out loud (Gemini Flash = free + always
#                    up even when Claude is spent).
#   working brain  — the left lane, tools, for normal work.
#   hard brain     — the left lane, for heavy work.
TALK_OPTS = ["gemini flash", "sonnet 5"]
WORK_OPTS = ["sonnet 5", "fable 5", "opus 4.8"]
# Global interface zoom — one factor scales EVERY font/padding in the app.
# Drawer offers presets; voice can set any value in [MIN,MAX] via set_ui_scale.
UI_SCALES = {"100%": 1.0, "125%": 1.25, "150%": 1.5, "175%": 1.75, "200%": 2.0}
UI_SCALE_MIN, UI_SCALE_MAX = 0.7, 2.5
# GOAT's speaker level (multiplier on the synthesized voice only).
VOICE_LEVELS = {"quiet": 0.6, "normal": 1.0, "loud": 1.4}

LANGS = {"english": "en", "ქართული": "ka"}

# Friendly UI-part name → palette key(s) the color tool can override.
COLOR_PARTS = {"text": ["paper"], "accent": ["accent"],
               "background": ["bg_top", "bg_bot"]}

DEFAULT_CFG = {"theme": "ember", "text": "normal", "voice": True,
               "level": "normal", "wake": True, "ontop": False,
               "lang": "en", "talk_brain": "gemini flash",
               "work_model": "fable 5", "hard_model": "fable 5",
               "scale": 1.0, "colors": {},
               "geom": None}  # [x, y, w, h] — remembered window box


def load_ui_config() -> dict:
    cfg = dict(DEFAULT_CFG)
    try:
        with open(UI_CONFIG, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in cfg})
    except (OSError, json.JSONDecodeError):
        pass
    if cfg["theme"] not in THEMES:
        cfg["theme"] = "ember"
    if cfg["text"] not in TEXT_SIZES:
        cfg["text"] = "normal"
    if cfg["level"] not in VOICE_LEVELS:
        cfg["level"] = "normal"
    if cfg["lang"] not in LANGS.values():
        cfg["lang"] = "en"
    if cfg["talk_brain"] not in TALK_OPTS:
        cfg["talk_brain"] = "gemini flash"
    if cfg["work_model"] not in WORK_OPTS:
        cfg["work_model"] = "fable 5"
    if cfg["hard_model"] not in WORK_OPTS:
        cfg["hard_model"] = "fable 5"
    try:
        cfg["scale"] = min(UI_SCALE_MAX, max(UI_SCALE_MIN, float(cfg["scale"])))
    except (TypeError, ValueError):
        cfg["scale"] = 1.0
    if not isinstance(cfg.get("colors"), dict):
        cfg["colors"] = {}
    return cfg


def save_ui_config(cfg: dict):
    try:
        with open(UI_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass  # preferences — never worth an error


# Sizes bumped 2026-07-11, then made globally scalable 2026-07-12 (Giorgi:
# "increase the icon/UI sizes by 50%" — GOAT resizes its OWN interface).
# Every px passes through _s(scale): one control zooms the whole app.
def build_style(t: dict, reply_px: int = 30, scale: float = 1.0) -> str:
    def s(px: int) -> int:
        return max(1, round(px * scale))
    reply = max(1, round(reply_px * scale))
    return f"""
QWidget {{ color: {t['paper']}; font-family: 'Segoe UI'; }}
QLabel#wordmark {{
  color: {t['dim']}; font-size: {s(15)}px; letter-spacing: 5px; font-weight: 600;
}}
QLabel#stateword {{ color: {t['accent']}; font-size: {s(15)}px; letter-spacing: 2px; }}
QPushButton#winbtn {{
  background: transparent; color: {t['faint']}; border: none;
  font-size: {s(15)}px; padding: {s(3)}px {s(12)}px;
}}
QPushButton#winbtn:hover {{ color: {t['paper']}; }}
QPushButton#themebtn {{
  background: transparent; color: {t['dim']}; border: none;
  font-size: {s(14)}px; letter-spacing: 2px; padding: {s(3)}px {s(12)}px;
}}
QPushButton#themebtn:hover {{ color: {t['accent']}; }}
QPushButton#micbtn {{
  background: transparent; color: {t['dim']}; border: none;
  font-size: {s(14)}px; letter-spacing: 2px; padding: {s(3)}px {s(12)}px;
}}
QPushButton#micbtn:hover {{ color: {t['paper']}; }}
QPushButton#micbtn[muted="true"] {{ color: {t['accent']}; }}
QPushButton#sendbtn {{
  background: transparent; color: {t['faint']}; border: none;
  border-bottom: 1px solid {t['faint']};
  font-size: {s(16)}px; padding: {s(6)}px {s(14)}px;
}}
QPushButton#sendbtn:hover {{ color: {t['accent']};
  border-bottom: 1px solid {t['accent']}; }}
QLabel#youNow {{
  color: {t['accent']}; font-size: {s(18)}px; letter-spacing: 1px; margin-top: {s(18)}px;
}}
QLabel#replyNow {{
  color: {t['paper']}; font-size: {reply}px; font-weight: 300;
}}
QLabel#youOld {{ color: {t['you_old']}; font-size: {s(16)}px; margin-top: {s(18)}px; }}
QLabel#replyOld {{ color: {t['reply_old']}; font-size: {s(20)}px; font-weight: 300; }}
QLabel#toolLine {{ color: {t['faint']}; font-size: {s(15)}px; font-style: italic; }}
QLabel#footer {{ color: {t['faint']}; font-size: {s(14)}px; letter-spacing: 1px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: {s(6)}px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['faint']}; border-radius: 3px; min-height: {s(30)}px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLineEdit#cmd {{
  background: transparent; border: none; border-bottom: 1px solid {t['faint']};
  padding: {s(10)}px {s(2)}px; font-size: {s(19)}px; color: {t['paper']};
  selection-background-color: {t['sel']};
}}
QLineEdit#cmd:focus {{ border-bottom: 1px solid {t['accent']}; }}
QLabel#epigraph {{
  color: {t['faint']}; font-size: {s(24)}px; font-weight: 300; letter-spacing: 4px;
}}
QLabel#clock {{ color: {t['dim']}; font-size: {s(15)}px; letter-spacing: 2px; }}
QLabel#paneltitle {{
  color: {t['dim']}; font-size: {s(14)}px; letter-spacing: 4px; font-weight: 600;
}}
QLabel#optlabel {{ color: {t['dim']}; font-size: {s(14)}px; letter-spacing: 1px; }}
QPushButton#optbtn {{
  background: transparent; color: {t['dim']}; border: none;
  font-size: {s(16)}px; padding: {s(5)}px {s(12)}px; text-align: left;
}}
QPushButton#optbtn:hover {{ color: {t['paper']}; }}
QPushButton#optbtn[on="true"] {{ color: {t['accent']}; }}
QPushButton#actbtn {{
  background: transparent; color: {t['paper']}; border: none;
  border-bottom: 1px solid {t['faint']}; font-size: {s(16)}px; padding: {s(5)}px {s(14)}px;
}}
QPushButton#actbtn:hover {{ color: {t['accent']};
  border-bottom: 1px solid {t['accent']}; }}
QPushButton#workbtn {{
  background: transparent; color: {t['dim']}; border: none;
  border-bottom: 1px solid {t['faint']};
  font-size: {s(15)}px; padding: {s(6)}px {s(12)}px;
}}
QPushButton#workbtn:hover {{ color: {t['accent']};
  border-bottom: 1px solid {t['accent']}; }}
QLabel#workmodel {{ color: {t['accent']}; font-size: {s(13)}px; letter-spacing: 2px; }}
QLabel#workidle {{ color: {t['faint']}; font-size: {s(15)}px; }}
QLabel#worktask {{ color: {t['paper']}; font-size: {s(16)}px; font-weight: 400; margin-top: {s(6)}px; }}
QLabel#workstep {{ color: {t['accent']}; font-size: {s(14)}px; }}
QLabel#workdone {{ color: {t['dim']}; font-size: {s(14)}px; }}
QLabel#worktext {{ color: {t['faint']}; font-size: {s(13)}px; font-style: italic; }}
QLabel#workfail {{ color: {t['accent']}; font-size: {s(15)}px; font-weight: 500; }}
"""


class Backdrop(QWidget):
    """Warm dark gradient. Nothing else — the silence behind the string."""

    def __init__(self):
        super().__init__()
        self.top = QColor("#0f0e0c")
        self.bot = QColor("#0b0a09")

    def set_theme(self, t: dict):
        self.top = QColor(t["bg_top"])
        self.bot = QColor(t["bg_bot"])
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, self.top)
        g.setColorAt(1.0, self.bot)
        p.fillRect(self.rect(), g)


class StringLine(QWidget):
    """GOAT's presence: a single stretched string of light.

    idle      — flat, breathing almost imperceptibly
    listening — ripples with Giorgi's live mic level
    thinking  — fine, fast shimmer, low amplitude
    speaking  — slow smooth traveling waves, amplitude = real speaker level
    """

    N = 180  # points across

    def __init__(self):
        super().__init__()
        self.level = 0.0
        self.state = "idle"
        self._t = 0.0
        self._seed = [random.uniform(0, math.tau) for _ in range(6)]
        self._base = QColor("#6f6a60")
        self._accent = QColor("#ffa94d")
        self._ignite_t0 = 0.0  # boot ritual: light travels down the string
        self.setMinimumHeight(140)

    def ignite(self, duration: float = 1.6):
        self._ignite_dur = duration
        self._ignite_t0 = time.time()

    def set_theme(self, t: dict):
        self._base = QColor(t["string_base"])
        self._accent = QColor(t["accent"])
        self.update()

    def tick(self, level: float, state: str):
        self.level = self.level * 0.7 + max(0.0, min(1.0, level)) * 0.3
        self.state = state
        self._t += 0.05
        self.update()

    def _amplitude_at(self, u: float) -> float:
        """u in [0,1] across the string; returns y offset in px."""
        t = self._t
        s = self._seed
        # ends pinned like a real string
        pin = math.sin(math.pi * u) ** 1.5
        if self.state == "idle":
            return pin * 2.2 * math.sin(6.0 * u * math.tau * 0.5 + t * 0.6)
        if self.state == "listening":
            a = 4 + self.level * 46
            w = (math.sin(u * 11 + t * 4 + s[0]) * 0.5
                 + math.sin(u * 23 - t * 6 + s[1]) * 0.3
                 + math.sin(u * 41 + t * 9 + s[2]) * 0.2)
            return pin * a * w
        if self.state in ("thinking", "working"):
            w = (math.sin(u * 60 + t * 14 + s[3]) * 0.6
                 + math.sin(u * 90 - t * 17 + s[4]) * 0.4)
            return pin * 5.5 * w
        # speaking — two slow, fat traveling waves
        a = 6 + self.level * 40
        w = (math.sin(u * 6 - t * 2.6 + s[5]) * 0.65
             + math.sin(u * 11 - t * 3.4) * 0.35)
        return pin * a * w

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mid = h / 2
        margin = max(30, int(w * 0.06))
        span = w - margin * 2

        path = QPainterPath()
        for i in range(self.N + 1):
            u = i / self.N
            x = margin + span * u
            y = mid + self._amplitude_at(u)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        active = self.state != "idle"
        heat = min(1.0, self.level * 2 + (0.35 if active else 0.0))

        # color: quiet base at rest, the theme accent where alive
        base, acc = self._base, self._accent
        col = QColor(
            int(base.red() + (acc.red() - base.red()) * heat),
            int(base.green() + (acc.green() - base.green()) * heat),
            int(base.blue() + (acc.blue() - base.blue()) * heat),
        )

        grad = QLinearGradient(margin, 0, margin + span, 0)
        edge = QColor(col)
        edge.setAlpha(0)
        core = QColor(col)
        core.setAlpha(230)
        grad.setColorAt(0.0, edge)
        grad.setColorAt(0.18, core)
        grad.setColorAt(0.82, core)
        grad.setColorAt(1.0, edge)

        # boot ritual: the light travels left to right, then life as usual
        if self._ignite_t0:
            f = (time.time() - self._ignite_t0) / getattr(self, "_ignite_dur", 1.6)
            if f >= 1.0:
                self._ignite_t0 = 0.0
            else:
                eased = 1 - (1 - f) ** 3
                p.setClipRect(0, 0, int(margin + span * eased + 26), h)
                p.setOpacity(0.25 + 0.75 * eased)

        # halo pass then the string itself
        halo = QColor(col)
        halo.setAlpha(int(28 + 60 * heat))
        p.setPen(QPen(halo, 7.0, Qt.SolidLine, Qt.RoundCap))
        p.drawPath(path)
        p.setPen(QPen(grad, 1.4, Qt.SolidLine, Qt.RoundCap))
        p.drawPath(path)


def _sweep_inbox(folder: str = INBOX, days: float = 7.0):
    """Delete app-generated clipboard clips (clip-*.png) older than `days`.
    Only clips: anything else in the inbox wasn't created by us and is not
    ours to clean up."""
    cutoff = time.time() - days * 86400
    try:
        for name in os.listdir(folder):
            if name.startswith("clip-") and name.endswith(".png"):
                path = os.path.join(folder, name)
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
    except OSError:
        pass  # inbox missing or a file in use — never a boot blocker


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}m"
    if n >= 1_000:
        return f"{n / 1e3:.0f}k"
    return str(n)


class PageLabel(QLabel):
    """QLabel whose minimum height is its real wrapped-text height.

    Word-wrapped QLabels in a QVBoxLayout report a near-zero minimum
    (heightForWidth is ignored in the layout's minimum-size pass), so once
    the page outgrows the viewport the scroll area COMPRESSES old lines to
    slivers instead of scrolling — history looked deleted."""

    def minimumSizeHint(self):
        base = super().minimumSizeHint()
        if not self.wordWrap():
            return base
        w = self.width()
        if w <= 1:
            return base
        return base.expandedTo(
            base.__class__(0, self.heightForWidth(w)))


class ClickableThumb(QLabel):
    """Image thumbnail that opens in the system viewer on click."""
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, _ev):
        try:
            subprocess.Popen(["explorer", self.path])
        except Exception:
            pass


class TopFade(QWidget):
    """Old lines dissolve as they scroll up under the string — a soft
    gradient lip over the top of the conversation. Mouse passes through.
    The lip must match the backdrop AT ITS OWN SCREEN POSITION (the backdrop
    is a gradient) or it reads as a grey band instead of a dissolve."""

    def __init__(self, parent):
        super().__init__(parent)
        self._top = QColor("#0f0e0c")
        self._bot = QColor("#0b0a09")
        self._frac = 0.25  # vertical position of the lip within the window
        self._col = QColor(self._top)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_theme(self, t: dict):
        self._top = QColor(t["bg_top"])
        self._bot = QColor(t["bg_bot"])
        self._mix()

    def set_frac(self, f: float):
        self._frac = max(0.0, min(1.0, f))
        self._mix()

    def _mix(self):
        f = self._frac
        self._col = QColor(
            int(self._top.red() + (self._bot.red() - self._top.red()) * f),
            int(self._top.green() + (self._bot.green() - self._top.green()) * f),
            int(self._top.blue() + (self._bot.blue() - self._top.blue()) * f),
        )
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        top = QColor(self._col)
        top.setAlpha(255)
        mid = QColor(self._col)
        mid.setAlpha(120)
        bot = QColor(self._col)
        bot.setAlpha(0)
        g.setColorAt(0.0, top)
        g.setColorAt(0.55, mid)
        g.setColorAt(1.0, bot)
        p.fillRect(self.rect(), g)


class SettingsPanel(QWidget):
    """Quiet right-hand drawer: every switch GOAT and the UI expose.
    Same design law as the rest — typography, one accent, no chrome."""

    def __init__(self, win):
        super().__init__(win.canvas)
        self.win = win
        self._bg = QColor("#0b0a09")
        self._line = QColor("#3d3a34")
        self._groups: dict[str, list] = {}
        self.hide()

        # The drawer is height-locked to the visible window, but its content
        # (10 option rows + actions + footer) is taller than that on a short
        # window — a plain layout then COMPRESSES every row below its natural
        # height and clips the button text (measured 2026-07-12). Put the
        # content in a scroll area so rows keep full height and overflow just
        # scrolls. Panel keeps its own paintEvent (bg + left border).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll)
        content = QWidget()
        content.setAttribute(Qt.WA_TranslucentBackground, True)
        scroll.setWidget(content)

        lay = QVBoxLayout(content)
        lay.setContentsMargins(26, 24, 26, 24)
        lay.setSpacing(14)
        title = QLabel("S E T T I N G S")
        title.setObjectName("paneltitle")
        lay.addWidget(title)
        lay.addSpacing(6)

        def row(label, key, options, handler):
            lay.addWidget(self._mklabel(label))
            h = QHBoxLayout()
            h.setSpacing(2)
            btns = []
            for opt in options:
                b = QPushButton(opt)
                b.setObjectName("optbtn")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, o=opt: handler(o))
                h.addWidget(b)
                btns.append((opt, b))
            h.addStretch(1)
            lay.addLayout(h)
            self._groups[key] = btns

        row("talking brain", "talk_brain", TALK_OPTS, self.win.set_talk_opt)
        row("working brain", "work_model", WORK_OPTS, self.win.set_work_opt)
        row("hard brain", "hard_model", WORK_OPTS, self.win.set_hard_opt)
        row("theme", "theme", THEME_ORDER, self.win.set_theme_opt)
        row("interface size", "scale", list(UI_SCALES), self.win.set_scale_opt)
        row("text size", "text", list(TEXT_SIZES), self.win.set_text_opt)
        row("voice", "voice", ["on", "off"], self.win.set_voice_opt)
        row("voice level", "level", list(VOICE_LEVELS), self.win.set_level_opt)
        row("language", "lang", list(LANGS), self.win.set_lang_opt)
        row("wake word", "wake", ["on", "off"], self.win.set_wake_opt)
        row("microphone", "mic", ["live", "muted"], self.win.set_mic_opt)
        row("window", "window", ["normal", "on top"], self.win.set_ontop_opt)

        lay.addSpacing(10)
        lay.addWidget(self._mklabel("actions"))
        h = QHBoxLayout()
        h.setSpacing(14)
        for text, cb in (("copy reply", self.win.copy_last_reply),
                         ("reset colors", self.win.reset_ui_colors),
                         ("new chat", self.win.new_chat),
                         ("restart", self.win.restart_goat)):
            b = QPushButton(text)
            b.setObjectName("actbtn")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            h.addWidget(b)
        h.addStretch(1)
        lay.addLayout(h)
        lay.addStretch(1)
        hint = QLabel("esc closes · changes save instantly")
        hint.setObjectName("footer")
        lay.addWidget(hint)

    @staticmethod
    def _mklabel(text):
        lbl = QLabel(text)
        lbl.setObjectName("optlabel")
        return lbl

    def set_theme(self, t: dict):
        self._bg = QColor(t["bg_bot"])
        self._bg.setAlpha(252)
        self._line = QColor(t["faint"])
        self.update()

    def refresh(self):
        """Light the active option in every group."""
        state = dict(self.win.cfg)
        state["voice"] = "on" if state.get("voice", True) else "off"
        state["wake"] = "on" if state.get("wake", True) else "off"
        state["window"] = "on top" if state.get("ontop") else "normal"
        state["lang"] = next((label for label, code in LANGS.items()
                              if code == state.get("lang", "en")), "english")
        # scale is stored as a float; light the preset that matches (or none
        # if he set an off-preset value by voice).
        sc = float(state.get("scale", 1.0))
        state["scale"] = next((lbl for lbl, v in UI_SCALES.items()
                               if abs(v - sc) < 0.001), "")
        goat = self.win.goat
        state["mic"] = "muted" if (goat and goat.mic_muted) else "live"
        for key, btns in self._groups.items():
            active = str(state.get(key, ""))
            for opt, b in btns:
                b.setProperty("on", "true" if opt == active else "false")
                b.style().unpolish(b)
                b.style().polish(b)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)
        p.setPen(QPen(self._line, 1))
        p.drawLine(0, 0, 0, self.height())


class WorkPanel(QWidget):
    """Left lane: what the WORKING brain is doing right now — the task, each
    live step (marked ✓ the moment the next begins or the turn ends), and the
    brain's own narration. Silent by design: this is the build log Giorgi
    watches on the left while he talks to Gemini in the middle."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._bg = QColor("#0b0a09")
        self._bg.setAlpha(70)
        self._line = QColor("#3d3a34")
        self._cur_step = None    # the in-progress step label ("▸ …")
        self._text_label = None  # rolling narration label for this turn

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 10, 16, 12)
        outer.setSpacing(6)
        self.header = QLabel("W O R K I N G   B R A I N")
        self.header.setObjectName("paneltitle")
        outer.addWidget(self.header)
        self.sub = QLabel("idle")
        self.sub.setObjectName("workmodel")
        outer.addWidget(self.sub)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        host = QWidget()
        host.setAutoFillBackground(False)
        self.col = QVBoxLayout(host)
        self.col.setContentsMargins(0, 8, 0, 0)
        self.col.setSpacing(5)
        self.col.addStretch(1)
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, stretch=1)

        self._idle = QLabel("no work running.\nsend an order to the\nworking brain —\nctrl+enter, or the\nwork button below.")
        self._idle.setObjectName("workidle")
        self._idle.setWordWrap(True)
        self.col.insertWidget(0, self._idle)

    def set_theme(self, t: dict):
        self._bg = QColor(t["bg_bot"])
        self._bg.setAlpha(80)
        self._line = QColor(t["faint"])
        self.update()

    def set_model(self, name: str):
        if self._cur_step is None and self.win and not self.win_busy():
            self.sub.setText(f"{name} · idle")

    def win_busy(self) -> bool:
        return bool(self.win and self.win.goat and self.win.goat.busy)

    def _add(self, text: str, name: str) -> QLabel:
        lbl = PageLabel(text)
        lbl.setObjectName(name)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.col.insertWidget(self.col.count() - 1, lbl)
        QTimer.singleShot(20, self._down)
        return lbl

    def _down(self):
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _mark_cur_done(self):
        if self._cur_step is not None:
            txt = self._cur_step.text()
            if txt.startswith("▸ "):
                self._cur_step.setText("✓ " + txt[2:])
            self._cur_step.setObjectName("workdone")
            self._cur_step.style().unpolish(self._cur_step)
            self._cur_step.style().polish(self._cur_step)
            self._cur_step = None

    def start(self, model: str, task: str):
        if self._idle is not None:
            self._idle.deleteLater()
            self._idle = None
        self._mark_cur_done()
        self.sub.setText(f"{model} · working")
        self._add("— " + " ".join(task.split())[:200], "worktask")
        self._text_label = None

    def step(self, desc: str):
        self._mark_cur_done()
        self._cur_step = self._add("▸ " + desc, "workstep")
        self._text_label = None

    def text(self, piece: str):
        if self._text_label is None:
            self._text_label = self._add("", "worktext")
        self._text_label.setText((self._text_label.text() + piece)[-1200:])
        self._down()

    def add(self, note: str):
        self._add("+ " + note, "workstep")
        self._text_label = None

    def done(self):
        self._mark_cur_done()
        self.sub.setText("idle")
        self._add("✓ done", "workdone")
        self._text_label = None

    def fail(self, reason: str):
        self._mark_cur_done()
        self.sub.setText("idle")
        self._add("⚠ " + reason, "workfail")
        self._text_label = None

    def files(self, paths: list):
        for p in paths:
            if p.strip():
                self._add("file — " + os.path.basename(p.strip()), "workstep")

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)
        p.setPen(QPen(self._line, 1))
        p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())


class GoatWindow(QWidget):
    event_sig = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.cfg = load_ui_config()
        self.setWindowTitle("GOAT")
        flags = Qt.FramelessWindowHint
        if self.cfg.get("ontop"):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        # Frameless still needs a floor — otherwise a resize can crush it to
        # nothing (usability pass 2026-07-12).
        self.setMinimumSize(720, 520)
        self._restore_geometry()   # remembered box, or a sane default
        self.setAcceptDrops(True)
        self.on_submit = None    # talk lane (middle)
        self.on_work = None      # work lane (left)
        self.on_files = None
        self._drag: QPoint | None = None
        self._reply_label: QLabel | None = None
        self._you_label: QLabel | None = None
        self._t0 = time.time()
        # Footer model = the talking brain (Gemini Flash), the always-on voice.
        # Placeholder until the engine reports it at boot.
        self._model = "…"
        self._work_model = ""    # working brain (shown on the left panel)
        self._statew = "booting"
        self._status_hold = 0.0  # until this time, hud_tick may not stomp
        self._usage = ""
        self._claude_out = False   # Claude quota spent? (footer meter)
        self._claude_reset = ""    # reset clock when out

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.canvas = Backdrop()
        outer.addWidget(self.canvas)
        self.goat = None  # engine handle, set by bind_engine()
        self._theme_name = self.cfg["theme"]

        lay = QVBoxLayout(self.canvas)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ---- top line: wordmark · state | window controls ----
        bar = QHBoxLayout()
        bar.setContentsMargins(34, 22, 22, 0)
        wordmark = QLabel("G O A T")
        wordmark.setObjectName("wordmark")
        self.stateword = QLabel("booting")
        self.stateword.setObjectName("stateword")
        # Mic toggle lives in the titlebar — the single most-used switch of a
        # voice assistant was buried in the drawer (usability pass 2026-07-12).
        self.mic_btn = QPushButton("mic")
        self.mic_btn.setObjectName("micbtn")
        self.mic_btn.setCursor(Qt.PointingHandCursor)
        self.mic_btn.setToolTip("microphone — click or ctrl+m to mute/unmute")
        self.mic_btn.clicked.connect(self.toggle_mic)
        self.theme_btn = QPushButton(self._theme_name)
        self.theme_btn.setObjectName("themebtn")
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.setToolTip("theme — click or ctrl+t to cycle")
        self.theme_btn.clicked.connect(self.cycle_theme)
        self.gear_btn = QPushButton("≡")  # NOT "⚙": Segoe maps it to a color emoji
        self.gear_btn.setObjectName("winbtn")
        self.gear_btn.setCursor(Qt.PointingHandCursor)
        self.gear_btn.setToolTip("settings — ctrl+,")
        self.gear_btn.clicked.connect(self.toggle_settings)
        b_min = QPushButton("–")
        b_min.setObjectName("winbtn")
        b_min.clicked.connect(self.showMinimized)
        b_full = QPushButton("⛶")
        b_full.setObjectName("winbtn")
        b_full.clicked.connect(self.toggle_fullscreen)
        b_close = QPushButton("✕")
        b_close.setObjectName("winbtn")
        b_close.clicked.connect(QApplication.quit)
        bar.addWidget(wordmark)
        bar.addSpacing(18)
        bar.addWidget(self.stateword)
        bar.addStretch(1)
        self.clock = QLabel("")
        self.clock.setObjectName("clock")
        bar.addWidget(self.clock)
        bar.addSpacing(16)
        bar.addWidget(self.mic_btn)
        bar.addWidget(self.theme_btn)
        bar.addWidget(self.gear_btn)
        bar.addSpacing(10)
        b_min.setToolTip("minimize")
        b_full.setToolTip("fullscreen — f11")
        b_close.setToolTip("quit GOAT")
        bar.addWidget(b_min)
        bar.addWidget(b_full)
        bar.addWidget(b_close)
        lay.addLayout(bar)
        self._titlebar_h = 60

        # ---- the string ----
        self.string = StringLine()
        lay.addWidget(self.string)

        # ---- the page: conversation as typography ----
        self.col = QVBoxLayout()
        self.col.setSpacing(6)
        self.col.addStretch(1)
        host = QWidget()
        host.setLayout(self.col)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(host)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Both fills off, or the viewport paints its own near-black over the
        # backdrop gradient and the page reads as a faint band.
        self.scroll.viewport().setAutoFillBackground(False)
        host.setAutoFillBackground(False)
        # Yesterday's tail: repaint recent exchanges dimmed, so a restart
        # doesn't LOOK like amnesia (the engine resumes the session anyway).
        restored = self._load_transcript_tail()
        # Empty-state epigraph — one quiet line until the first exchange.
        self.epigraph = None
        if not restored:
            self.epigraph = QLabel("Say the word.")
            self.epigraph.setObjectName("epigraph")
            self.epigraph.setAlignment(Qt.AlignHCenter)
            self.epigraph.setContentsMargins(0, 90, 0, 0)
            self.col.insertWidget(0, self.epigraph)
        # Fade lip over the top of the page (created after scroll exists).
        self.fade = TopFade(self.scroll)
        # Follow mode: auto-scroll only while he's already at the bottom.
        # Scrolling up to reread history parks the page; scrolling back down
        # (or speaking again) re-engages following. Without this the 33ms
        # word reveal yanks the page to the bottom while he's reading.
        self._follow = True
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        # Left lane: the working brain's live build log. Middle: the talking
        # brain (Gemini) conversation. He watches Fable build on the left while
        # he keeps talking to Gemini in the middle (his order 2026-07-17).
        self.work_panel = WorkPanel(self)
        page = QHBoxLayout()
        page.setContentsMargins(0, 10, 0, 0)
        page.addWidget(self.work_panel, stretch=5)
        page.addSpacing(10)
        page.addWidget(self.scroll, stretch=8)
        page.addStretch(1)
        lay.addLayout(page, stretch=1)

        # ---- command field (always there — speak or type, both first-class) ----
        # Enter → talking brain (Gemini, middle). Ctrl+Enter → working brain
        # (left). Ctrl+Shift+Enter → hard brain (left). His manual dispatch.
        self.input = QLineEdit()
        self.input.setObjectName("cmd")
        self.input.setPlaceholderText(
            "talk to the talking brain — enter  ·  work: ctrl+enter  ·  hard: ctrl+shift+enter")
        self.input.returnPressed.connect(self._submit)
        send_btn = QPushButton("talk ↵")
        send_btn.setObjectName("sendbtn")
        send_btn.setCursor(Qt.PointingHandCursor)
        send_btn.setToolTip("send to the talking brain — or press enter")
        send_btn.clicked.connect(self._submit)
        work_btn = QPushButton("work ⌃↵")
        work_btn.setObjectName("workbtn")
        work_btn.setCursor(Qt.PointingHandCursor)
        work_btn.setToolTip("send to the working brain — or ctrl+enter")
        work_btn.clicked.connect(lambda: self._submit_work(False))
        hard_btn = QPushButton("hard ⌃⇧↵")
        hard_btn.setObjectName("workbtn")
        hard_btn.setCursor(Qt.PointingHandCursor)
        hard_btn.setToolTip("send to the hard-task working brain — or ctrl+shift+enter")
        hard_btn.clicked.connect(lambda: self._submit_work(True))
        cmd_row = QHBoxLayout()
        cmd_row.setContentsMargins(0, 0, 0, 6)
        cmd_row.setSpacing(0)
        cmd_row.addStretch(5)
        cmd_row.addWidget(self.input, stretch=8)
        cmd_row.addWidget(send_btn)
        cmd_row.addWidget(work_btn)
        cmd_row.addWidget(hard_btn)
        cmd_row.addStretch(1)
        lay.addLayout(cmd_row)

        # ---- footer: one quiet line ----
        self.footer = QLabel("")
        self.footer.setObjectName("footer")
        foot_row = QHBoxLayout()
        foot_row.setContentsMargins(34, 4, 34, 18)
        foot_row.addWidget(self.footer)
        foot_row.addStretch(1)
        hint = QLabel("esc quiets voice · ctrl+m mic · ctrl+k type · ctrl+n new chat · ctrl+, settings")
        hint.setObjectName("footer")
        foot_row.addWidget(hint)
        lay.addLayout(foot_row)

        self.event_sig.connect(self._on_event)

        QShortcut(QKeySequence(Qt.Key_F11), self, self.toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._escape)
        QShortcut(QKeySequence("Ctrl+K"), self, self._show_cmd)
        QShortcut(QKeySequence("Ctrl+T"), self, self.cycle_theme)
        QShortcut(QKeySequence("Ctrl+,"), self, self.toggle_settings)
        QShortcut(QKeySequence("Ctrl+O"), self, self._pick_files)
        QShortcut(QKeySequence("Ctrl+M"), self, self.toggle_mic)
        QShortcut(QKeySequence("Ctrl+N"), self, self.new_chat)
        # Manual work dispatch: Ctrl+Enter → working brain, +Shift → hard.
        QShortcut(QKeySequence("Ctrl+Return"), self, lambda: self._submit_work(False))
        QShortcut(QKeySequence("Ctrl+Enter"), self, lambda: self._submit_work(False))
        QShortcut(QKeySequence("Ctrl+Shift+Return"), self, lambda: self._submit_work(True))
        QShortcut(QKeySequence("Ctrl+Shift+Enter"), self, lambda: self._submit_work(True))

        self.panel = SettingsPanel(self)
        self.apply_theme(self._theme_name)

    # ---- window controls ----
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _show_cmd(self):
        self.input.setFocus()
        self.input.selectAll()

    def _escape(self):
        if self.panel.isVisible():
            self.panel.hide()
        elif self.input.hasFocus():
            self.input.clear()
            self.input.clearFocus()
        elif self.goat and self.goat.tts.speaking():
            # Keyboard barge-in: esc shuts GOAT up mid-sentence (voice
            # barge-in already did this; mouse/keyboard users had no way).
            self.goat.tts.cancel()
            self._on_event("status", "quieted")
        elif self.isFullScreen():
            self.showNormal()

    def toggle_mic(self):
        """Titlebar mic button / ctrl+m — same switch the drawer exposes."""
        if not self.goat:
            return
        self.set_mic_opt("live" if self.goat.mic_muted else "muted")

    def _refresh_mic_btn(self):
        muted = bool(self.goat and self.goat.mic_muted)
        self.mic_btn.setText("muted" if muted else "mic")
        self.mic_btn.setProperty("muted", "true" if muted else "false")
        self.mic_btn.style().unpolish(self.mic_btn)
        self.mic_btn.style().polish(self.mic_btn)

    # ---- theme / appearance ----
    def apply_theme(self, name: str):
        base = THEMES.get(name) or THEMES["ember"]
        # Live per-part color overrides GOAT set (e.g. text→blue) ride on top
        # of whatever theme is active.
        t = {**base, **(self.cfg.get("colors") or {})}
        self._theme_name = name
        self.cfg["theme"] = name
        self.setStyleSheet(build_style(t, TEXT_SIZES[self.cfg["text"]],
                                       float(self.cfg.get("scale", 1.0))))
        self.canvas.set_theme(t)
        self.string.set_theme(t)
        self.fade.set_theme(t)
        if hasattr(self, "work_panel"):
            self.work_panel.set_theme(t)
        self.theme_btn.setText(name)
        self.panel.set_theme(t)
        self.panel.refresh()

    def cycle_theme(self):
        i = THEME_ORDER.index(self._theme_name) if self._theme_name in THEME_ORDER else 0
        name = THEME_ORDER[(i + 1) % len(THEME_ORDER)]
        self.apply_theme(name)
        save_ui_config(self.cfg)

    # ---- settings panel ----
    def toggle_settings(self):
        if self.panel.isVisible():
            self.panel.hide()
            return
        self._place_panel()
        self.panel.refresh()
        # Slide in from the right edge — 170ms, settles quickly.
        end = self.panel.geometry()
        start = QRect(self.canvas.width(), end.y(), end.width(), end.height())
        self.panel.setGeometry(start)
        self.panel.show()
        self.panel.raise_()
        anim = QPropertyAnimation(self.panel, b"geometry", self)
        anim.setDuration(170)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _place_panel(self):
        # Below the title bar — window controls and the ≡ stay reachable.
        w = max(320, int(self.canvas.width() * 0.24))
        top = self._titlebar_h
        self.panel.setGeometry(self.canvas.width() - w, top,
                               w, self.canvas.height() - top)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.panel.isVisible():
            self._place_panel()
        self._debounce_geom_save()

    def _save(self):
        save_ui_config(self.cfg)
        self.panel.refresh()

    def set_theme_opt(self, name: str):
        self.apply_theme(name)
        save_ui_config(self.cfg)

    def set_text_opt(self, size: str):
        self.cfg["text"] = size
        self.apply_theme(self._theme_name)  # rebuilds the stylesheet
        save_ui_config(self.cfg)

    def set_scale_opt(self, label: str):
        """Drawer preset click ('150%')."""
        self.set_ui_scale(UI_SCALES.get(label, 1.0))

    def set_ui_color(self, part: str, color: str) -> bool:
        """Live recolor of one UI part (GOAT changing its own look). Returns
        False for an unrecognized color/part so the tool can tell him."""
        keys = COLOR_PARTS.get(part)
        if not keys:
            return False
        c = QColor(color)
        if not c.isValid():
            return False
        hexv = c.name()
        cols = dict(self.cfg.get("colors") or {})
        for k in keys:
            cols[k] = hexv
        self.cfg["colors"] = cols
        self.apply_theme(self._theme_name)
        save_ui_config(self.cfg)
        self._on_event("status", f"{part} color → {color}")
        return True

    def reset_ui_colors(self):
        self.cfg["colors"] = {}
        self.apply_theme(self._theme_name)
        save_ui_config(self.cfg)
        self._on_event("status", "colors reset to theme")

    def set_ui_scale(self, factor: float, relative: bool = False):
        """Live global UI zoom — from the drawer OR from GOAT itself (voice:
        'make your interface 50% bigger'). relative=True multiplies the
        current scale (a 50%-bigger request), else sets it absolutely.
        Clamped, applied instantly, saved."""
        cur = float(self.cfg.get("scale", 1.0))
        target = cur * factor if relative else factor
        target = min(UI_SCALE_MAX, max(UI_SCALE_MIN, round(target, 3)))
        self.cfg["scale"] = target
        self.apply_theme(self._theme_name)  # rebuilds the stylesheet at scale
        save_ui_config(self.cfg)
        self._on_event("status", f"interface size {round(target * 100)}%")

    def set_voice_opt(self, opt: str):
        self.cfg["voice"] = opt == "on"
        if self.goat:
            self.goat.tts.enabled = self.cfg["voice"]
            if not self.cfg["voice"]:
                self.goat.tts.cancel()  # silence the current sentence too
        self._save()

    def set_level_opt(self, level: str):
        self.cfg["level"] = level
        if self.goat:
            self.goat.tts.gain = VOICE_LEVELS[level]
        self._save()

    def set_wake_opt(self, opt: str):
        self.cfg["wake"] = opt == "on"
        if self.goat:
            self.goat.wake_enabled = self.cfg["wake"]
        self._save()

    def set_mic_opt(self, opt: str):
        if self.goat:
            self.goat.mic_muted = opt == "muted"
            self._on_event("status", "mic muted" if self.goat.mic_muted
                           else "listening")
        self._refresh_mic_btn()
        self._save()

    def set_talk_opt(self, name: str):
        self.cfg["talk_brain"] = name if name in TALK_OPTS else "gemini flash"
        if self.goat:
            self.goat.set_talk_brain(self.cfg["talk_brain"])
        self._save()

    def set_work_opt(self, name: str):
        self.cfg["work_model"] = name if name in WORK_OPTS else "fable 5"
        if self.goat:
            self.goat.set_work_model(self.cfg["work_model"])
        self._save()

    def set_hard_opt(self, name: str):
        self.cfg["hard_model"] = name if name in WORK_OPTS else "fable 5"
        if self.goat:
            self.goat.set_hard_model(self.cfg["hard_model"])
        self._save()

    def set_lang_opt(self, label: str):
        code = LANGS.get(label, "en")
        if code == self.cfg.get("lang"):
            return
        self.cfg["lang"] = code
        if self.goat:
            self.goat.set_language(code)
        self._save()

    def set_ontop_opt(self, opt: str):
        v = opt == "on top"
        self.cfg["ontop"] = v
        fs = self.isFullScreen()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, v)
        # Changing a window flag hides the window — bring it straight back.
        if fs:
            self.showFullScreen()
        else:
            self.show()
        self._save()

    def copy_last_reply(self):
        """Latest non-empty reply (current or previous) to the clipboard."""
        for i in range(self.col.count() - 2, -1, -1):
            wdg = self.col.itemAt(i).widget()
            if (wdg is not None and wdg.objectName() in ("replyNow", "replyOld")
                    and wdg.text().strip()):
                QApplication.clipboard().setText(wdg.text())
                self._on_event("status", "reply copied")
                return
        self._on_event("status", "nothing to copy yet")

    def bind_engine(self, goat):
        """Hand the window its engine and push the saved preferences in."""
        self.goat = goat
        goat.tts.enabled = self.cfg["voice"]
        goat.tts.gain = VOICE_LEVELS[self.cfg["level"]]
        goat.wake_enabled = self.cfg["wake"]
        # Before the engine thread starts: run() applies voice + hearing
        # model + persona note itself from this attribute.
        goat.language = self.cfg["lang"]
        goat.talk_brain = self.cfg.get("talk_brain", "gemini flash")
        goat.work_model = self.cfg.get("work_model", "fable 5")
        goat.hard_model = self.cfg.get("hard_model", "fable 5")
        self.panel.refresh()

    # ---- session actions ----
    def new_chat(self):
        """Fresh brain: drop the session file, restart through the gate."""
        try:
            os.remove(os.path.join(GOAT_ROOT, ".goat-session-py"))
        except OSError:
            pass
        self.restart_goat()

    def restart_goat(self):
        self._on_event("status", "restarting…")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", os.path.join(GOAT_ROOT, "python", "restart-goat.ps1")],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def mousePressEvent(self, ev):
        # Fallback drag only — on Windows the native hit-test below turns the
        # titlebar into a real caption (OS handles move, Aero Snap, double-
        # click maximize), so this rarely fires. Kept for safety / non-Windows.
        if (ev.button() == Qt.LeftButton and not self.isFullScreen()
                and ev.position().y() < self._titlebar_h):
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _ev):
        self._drag = None

    # ---- native window behavior (resize borders + Aero Snap) ----
    # Frameless windows lose everything the OS normally gives a title bar:
    # edge/corner resize, snap-to-half, snap layouts, double-click maximize,
    # shake-to-minimize. WM_NCHITTEST hands those back — we just tell Windows
    # which part of the window each pixel belongs to (2026-07-12: "window
    # behaves weirdly"). Any failure falls through to Qt's default + the
    # manual drag above, so this can never brick the window.
    _BORDER = 7  # px grab band on each edge

    def _hit_test(self, p: QPoint):
        w, h, b = self.width(), self.height(), self._BORDER
        x, y = p.x(), p.y()
        left, right = x < b, x >= w - b
        top, bottom = y < b, y >= h - b
        if not self.isMaximized() and not self.isFullScreen():
            if top and left:
                return 13     # HTTOPLEFT
            if top and right:
                return 14     # HTTOPRIGHT
            if bottom and left:
                return 16     # HTBOTTOMLEFT
            if bottom and right:
                return 17     # HTBOTTOMRIGHT
            if left:
                return 10     # HTLEFT
            if right:
                return 11     # HTRIGHT
            if top:
                return 12     # HTTOP
            if bottom:
                return 15     # HTBOTTOM
        # Titlebar band, but let real buttons keep their clicks.
        if y < self._titlebar_h:
            child = self.childAt(p)
            if not isinstance(child, (QPushButton, QLineEdit)):
                return 2      # HTCAPTION — drag/snap/double-click-maximize
        return None           # HTCLIENT (default)

    def nativeEvent(self, eventType, message):
        if eventType == "windows_generic_MSG" and not self.isFullScreen():
            try:
                addr = int(message)
                if not addr:
                    return super().nativeEvent(eventType, message)
                msg = ctypes.wintypes.MSG.from_address(addr)
                if msg.message == 0x0084:  # WM_NCHITTEST
                    gx = ctypes.c_short(msg.lParam & 0xFFFF).value
                    gy = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                    # WM_NCHITTEST coords are PHYSICAL screen pixels; Qt
                    # widgets speak LOGICAL (DPI-scaled) ones. At 125% display
                    # scale mapFromGlobal() here was off by 25%, so interior
                    # clicks hit-tested as caption/resize and windowed mode
                    # felt completely click-dead (fullscreen skips this
                    # handler, which is why it still worked). Convert against
                    # the native window rect — physical like the message.
                    rect = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(
                        int(self.winId()), ctypes.byref(rect))
                    dpr = self.devicePixelRatioF() or 1.0
                    p = QPoint(int((gx - rect.left) / dpr),
                               int((gy - rect.top) / dpr))
                    code = self._hit_test(p)
                    if code is not None:
                        return True, code
            except Exception:  # noqa: BLE001 — never let hit-testing crash the UI
                pass
        return super().nativeEvent(eventType, message)

    # ---- remembered window box ----
    def _restore_geometry(self):
        geom = self.cfg.get("geom")
        if (isinstance(geom, (list, tuple)) and len(geom) == 4
                and all(isinstance(n, (int, float)) for n in geom)):
            x, y, w, h = (int(n) for n in geom)
            w, h = max(720, w), max(520, h)
            # Clamp onto a currently-connected screen so a remembered box from
            # an unplugged monitor can't strand GOAT off-screen.
            area = self.screen().availableGeometry() if self.screen() else None
            if area:
                x = min(max(x, area.left()), area.right() - 120)
                y = min(max(y, area.top()), area.bottom() - 80)
                w = min(w, area.width())
                h = min(h, area.height())
            self.setGeometry(x, y, w, h)
        else:
            self.resize(1100, 800)

    def _save_geometry(self):
        if self.isMaximized() or self.isFullScreen() or self.isMinimized():
            return  # only remember the normal floating box
        g = self.geometry()
        self.cfg["geom"] = [g.x(), g.y(), g.width(), g.height()]
        save_ui_config(self.cfg)

    def moveEvent(self, ev):
        super().moveEvent(ev)
        self._debounce_geom_save()

    def _debounce_geom_save(self):
        # Coalesce the flood of move/resize events into one save shortly after
        # motion stops — no disk write per pixel.
        if not hasattr(self, "_geom_timer"):
            self._geom_timer = QTimer(self)
            self._geom_timer.setSingleShot(True)
            self._geom_timer.timeout.connect(self._save_geometry)
        self._geom_timer.start(600)

    # ---- attachments: drop / pick / paste ----
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self._send_files(paths)
            ev.acceptProposedAction()

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Send to GOAT")
        if paths:
            self._send_files(paths)

    def keyPressEvent(self, ev):
        # Ctrl+V with an image on the clipboard (screenshot etc.) sends it.
        # When the text field has focus it eats Ctrl+V first — Esc, then paste.
        if ev.matches(QKeySequence.Paste):
            img = QApplication.clipboard().image()
            if not img.isNull():
                os.makedirs(INBOX, exist_ok=True)
                path = os.path.join(INBOX, time.strftime("clip-%Y%m%d-%H%M%S.png"))
                img.save(path, "PNG")
                self._send_files([path])
                return
        super().keyPressEvent(ev)

    def _send_files(self, paths: list):
        if not self.on_files:
            return
        # anything typed (but not yet sent) rides along as the note
        note = self.input.text().strip()
        self.input.clear()
        self.on_files(paths, note)

    # ---- per-frame ----
    def update_spoken(self, text: str):
        """Word-by-word reveal, driven by the playback clock (not the LLM
        stream) — the text on screen is exactly what the voice has said."""
        if self._reply_label is not None and text and self._reply_label.text() != text:
            self._reply_label.setText(text)
            self._scroll_down()

    def hud_tick(self, mic_level: float, state: str, _listening: bool):
        self.string.tick(mic_level, state)
        # Status lines ("calibrating…", "reconnected") hold the word for a
        # few seconds — without the hold, the 33ms audio-state ticker stomps
        # every status within one frame and none of them are ever seen.
        if (state != self._statew and self._statew != "booting"
                and time.time() >= self._status_hold):
            self._statew = state
            self.stateword.setText(state)
        up = int(time.time() - self._t0)
        mic = "mic muted" if (self.goat and self.goat.mic_muted) else "mic live"
        # Claude usage meter: OUT (+reset) when spent, else session tokens.
        if self._claude_out:
            claude = " · claude OUT" + (
                f" · resets {self._claude_reset}" if self._claude_reset else "")
        elif self._usage:
            claude = f" · claude {self._usage}"
        else:
            claude = ""
        self.footer.setText(
            f"talk {self._model} · {mic} · {up // 60:02d}:{up % 60:02d}{claude}")
        self.clock.setText(time.strftime("%H:%M"))
        # Keep the fade lip glued across resizes (33ms — geometry set is cheap).
        if self.fade.width() != self.scroll.width():
            self.fade.setGeometry(0, 0, self.scroll.width(), 46)
            self.fade.raise_()
            y = self.scroll.mapTo(self.canvas, QPoint(0, 0)).y()
            self.fade.set_frac(y / max(1, self.canvas.height()))

    # ---- input ----
    def _submit(self):
        text = self.input.text().strip()
        if text and self.on_submit:
            self.on_submit(text)
            self.input.clear()

    def _submit_work(self, hard: bool = False):
        """Dispatch the typed order to the working brain (left lane), or the
        hard-task brain when hard=True."""
        text = self.input.text().strip()
        if text and self.on_work:
            self.on_work(text, hard)
            self.input.clear()

    # ---- the page ----
    def _dim_previous(self):
        """The current exchange is bright; everything before it recedes."""
        for i in range(self.col.count() - 1):
            item = self.col.itemAt(i)
            wdg = item.widget()
            if wdg is None:
                continue
            name = wdg.objectName()
            if name == "youNow":
                wdg.setObjectName("youOld")
            elif name == "replyNow":
                wdg.setObjectName("replyOld")
            wdg.style().unpolish(wdg)
            wdg.style().polish(wdg)

    def _load_transcript_tail(self, keep: int = 6) -> bool:
        """Old exchanges from workspace/transcript.jsonl, painted dimmed.
        Returns True when anything was restored."""
        path = os.path.join(GOAT_ROOT, "workspace", "transcript.jsonl")
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()[-keep:]
        except OSError:
            return False
        restored = False
        for line in lines:
            try:
                ex = json.loads(line)
            except json.JSONDecodeError:
                continue
            user = (ex.get("user") or "").strip()
            reply = (ex.get("reply") or "").strip()
            if not user:
                continue
            self._add_line(user.lower(), "youOld")
            if reply:
                self._add_line(reply, "replyOld")
            restored = True
        return restored

    def _add_line(self, text: str, name: str) -> QLabel:
        lbl = PageLabel(text)
        lbl.setObjectName(name)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.col.insertWidget(self.col.count() - 1, lbl)
        QTimer.singleShot(30, self._scroll_down)
        return lbl

    def _add_thumbnail(self, path: str):
        """Inline preview of an attached image. Non-images (QPixmap can't
        load them) are silently skipped — their name is already on the page."""
        if not path:
            return
        pm = QPixmap(path)
        if pm.isNull():
            return
        lbl = ClickableThumb(path)
        lbl.setObjectName("attachThumb")
        lbl.setPixmap(pm.scaled(
            460, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lbl.setToolTip(path)
        self.col.insertWidget(self.col.count() - 1, lbl)
        QTimer.singleShot(30, self._scroll_down)

    def _on_scrolled(self, value: int):
        sb = self.scroll.verticalScrollBar()
        self._follow = value >= sb.maximum() - 80

    def _scroll_down(self):
        if not self._follow:
            return  # he scrolled up to read — don't snatch the page
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---- events from GoatApp (any thread) ----
    def post_event(self, kind: str, data: str):
        self.event_sig.emit(kind, str(data))

    def _on_event(self, kind: str, data: str):
        if kind == "status":
            self._statew = data.lower()
            self.stateword.setText(self._statew)
            self._status_hold = time.time() + 4.0
        elif kind == "talkmodel":
            self._model = data  # talking brain = the footer's live model
        elif kind == "model":
            self._work_model = data  # working brain (left panel)
            self.work_panel.set_model(data)
        elif kind == "claude":
            # Claude usage meter: "ok" or "out|HH:MM".
            if data == "ok":
                self._claude_out = False
            elif data.startswith("out"):
                self._claude_out = True
                _, _, reset = data.partition("|")
                self._claude_reset = reset.strip()
        elif kind == "ui_scale":
            # GOAT resizing its own interface (voice/typed request routed
            # through the engine). Payload: "<factor>" absolute, or "*<factor>"
            # relative (e.g. "*1.5" = 50% bigger).
            try:
                if data.startswith("*"):
                    self.set_ui_scale(float(data[1:]), relative=True)
                else:
                    self.set_ui_scale(float(data))
            except (ValueError, TypeError):
                pass
        elif kind == "ui_color":
            # "part|color" — GOAT recoloring its own interface.
            part, _, color = data.partition("|")
            self.set_ui_color(part.strip(), color.strip())
        elif kind == "usage":
            try:
                tin, tout = (int(x) for x in data.split("|"))
                self._usage = f"{_fmt_tok(tin)} in / {_fmt_tok(tout)} out"
            except ValueError:
                pass
        elif kind == "limit":
            # out of quota — say it loud on the page, in amber, and hold the
            # state word long enough to actually register
            self._add_line(data.lower(), "youNow")
            self._statew = "out of usage"
            self.stateword.setText("out of usage")
            self._status_hold = time.time() + 15.0
        elif kind == "you":
            if self.epigraph is not None:
                self.epigraph.deleteLater()
                self.epigraph = None
            self._dim_previous()
            self._reply_label = None
            self._follow = True  # he spoke — bring him to the reply
            self._you_label = self._add_line(data.lower(), "youNow")
            spacer = self._add_line("", "replyNow")
            spacer.setFixedHeight(2)
        elif kind == "files":
            # thumbnails of what he just sent, right under his line
            for path in data.split("\n"):
                self._add_thumbnail(path.strip())
        elif kind == "delta":
            # Text is NOT shown from the model's stream — it would race far
            # ahead of the voice. The label is created here; its words are
            # revealed by update_spoken(), synced to actual playback.
            if self._reply_label is None:
                self._reply_label = self._add_line("", "replyNow")
        elif kind == "tool":
            # Keep _reply_label — the whole turn reveals into ONE label
            # (spoken_text() is cumulative; a second label would duplicate).
            self._add_line(f"·  {data.lower()}", "toolLine")
        elif kind == "work_start":
            # Left lane: a work turn began — "<model>|<task>".
            model, _, task = data.partition("|")
            self.work_panel.start(model.strip(), task)
        elif kind == "work_tool" or kind == "work_step":
            self.work_panel.step(data)
        elif kind == "work_text":
            self.work_panel.text(data)
        elif kind == "work_add":
            self.work_panel.add(data)
        elif kind == "work_files":
            self.work_panel.files(data.split("\n"))
        elif kind == "work_done":
            self.work_panel.done()
        elif kind == "work_fail":
            self.work_panel.fail(data)
        elif kind == "turn_done":
            # Do NOT drop _reply_label here: the model finishes generating
            # seconds before the voice finishes speaking (often before it
            # even starts), and the word reveal keeps landing in this label
            # until the next "you" resets it. Nulling it here is what made
            # the screen stay permanently blank.
            self.stateword.setText("listening")


def main():
    # Own taskbar identity (otherwise Windows groups us under "Python").
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KingKaglu.GOAT")
    except Exception:  # noqa: BLE001
        pass

    _sweep_inbox()
    app = QApplication(sys.argv)
    app.setApplicationName("GOAT")
    if os.path.exists(ICON):
        app.setWindowIcon(QIcon(ICON))
    win = GoatWindow()

    # Boot latency (2026-07-15, "it needs so much time to turn on"): the
    # goat_app import drags torch in via silero-vad — seconds even warm,
    # much longer on a cold disk cache — and used to run BEFORE the window
    # existed, so launching looked like nothing was happening. Paint the
    # window immediately, import the engine on a side thread, bind it on
    # the main thread the moment the import lands.
    win.showFullScreen()
    win.string.ignite()  # boot ritual: the light travels down the string

    holder: dict = {}

    def _import_engine():
        try:
            from goat_app import GoatApp
            holder["cls"] = GoatApp
        except Exception:  # noqa: BLE001 — surface it, don't die silently
            import traceback
            traceback.print_exc()
    imp = threading.Thread(target=_import_engine, daemon=True)
    imp.start()

    def _bind_when_ready():
        if imp.is_alive():
            QTimer.singleShot(50, _bind_when_ready)
            return
        if "cls" not in holder:
            win.post_event(
                "status", "engine import failed — check python\\goat-app.log")
            return
        goat = holder["cls"](emit=win.post_event)
        holder["goat"] = goat
        win.on_submit = goat.submit_text
        win.on_work = goat.submit_work
        win.on_files = goat.submit_files
        win.bind_engine(goat)

        def engine():
            import asyncio
            import traceback
            try:
                asyncio.run(goat.run())
            except Exception:  # noqa: BLE001 — surface it, don't die silently
                traceback.print_exc()
                win.post_event(
                    "status", "engine crashed — check python\\goat-app.log")
            else:
                win.post_event("status", "engine stopped")

        threading.Thread(target=engine, daemon=True).start()
    QTimer.singleShot(50, _bind_when_ready)

    timer = QTimer()
    def tick():
        goat = holder.get("goat")
        if goat is None:
            win.hud_tick(0.2, "booting", False)
            return
        if goat.audio.is_tts_playing:
            state = "speaking"
            level = goat.audio.out_level * 6
        elif getattr(goat, "talk_busy", False):
            state = "thinking"       # talking brain composing (middle)
            level = 0.25
        elif goat.busy:
            state = "working"        # working brain building (left), silent
            level = 0.2
        else:
            level = (goat.audio._raw_rms_ema or 0.0) * 12
            state = "listening" if level > 0.35 else "idle"
        win.hud_tick(level, state, state in ("idle", "listening"))
        win.update_spoken(goat.tts.spoken_text())
    timer.timeout.connect(tick)
    timer.start(33)

    code = app.exec()
    if holder.get("goat"):
        holder["goat"].shutdown_audio()
    os._exit(code)  # asyncio daemon thread has no clean cross-thread stop


if __name__ == "__main__":
    main()
