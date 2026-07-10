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
THEMES = {
    "ember": {          # the original: warm dark, amber string
        "bg_top": "#0f0e0c", "bg_bot": "#0b0a09",
        "paper": "#ece5d8", "dim": "#6f6a60", "faint": "#3d3a34",
        "accent": "#ffa94d", "string_base": "#6f6a60",
        "you_old": "#a37c4f", "reply_old": "#948d7f", "sel": "#4a3722",
    },
    "paper": {          # daylight: warm paper, ink type, vermilion accent
        "bg_top": "#f2ede3", "bg_bot": "#eae4d6",
        "paper": "#1e1b16", "dim": "#8a8375", "faint": "#b0a794",
        "accent": "#c74a24", "string_base": "#8a8375",
        "you_old": "#a05a3c", "reply_old": "#6f6a60", "sel": "#f0c9b8",
    },
    "phosphor": {       # night instrument: green-black, phosphor trace
        "bg_top": "#0a0f0b", "bg_bot": "#070b08",
        "paper": "#d8e8da", "dim": "#5f7263", "faint": "#2c3a30",
        "accent": "#5ce88a", "string_base": "#5f7263",
        "you_old": "#4f8a62", "reply_old": "#7f9484", "sel": "#1e4a2e",
    },
    "graphite": {       # mono: near-black, white-hot string
        "bg_top": "#0e0e10", "bg_bot": "#0a0a0b",
        "paper": "#e8e8ea", "dim": "#6c6c72", "faint": "#38383e",
        "accent": "#ffffff", "string_base": "#6c6c72",
        "you_old": "#9a9aa0", "reply_old": "#8c8c92", "sel": "#3a3a44",
    },
}
THEME_ORDER = ["ember", "paper", "phosphor", "graphite"]


# Reply type sizes: the current answer's pt size (older lines stay put).
TEXT_SIZES = {"small": 22, "normal": 30, "large": 38}
# GOAT's speaker level (multiplier on the synthesized voice only).
VOICE_LEVELS = {"quiet": 0.6, "normal": 1.0, "loud": 1.4}

DEFAULT_CFG = {"theme": "ember", "text": "normal", "voice": True,
               "level": "normal", "wake": True, "ontop": False}


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
    return cfg


def save_ui_config(cfg: dict):
    try:
        with open(UI_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass  # preferences — never worth an error


def build_style(t: dict, reply_px: int = 30) -> str:
    return f"""
QWidget {{ color: {t['paper']}; font-family: 'Segoe UI'; }}
QLabel#wordmark {{
  color: {t['dim']}; font-size: 13px; letter-spacing: 5px; font-weight: 600;
}}
QLabel#stateword {{ color: {t['accent']}; font-size: 13px; letter-spacing: 2px; }}
QPushButton#winbtn {{
  background: transparent; color: {t['faint']}; border: none;
  font-size: 13px; padding: 2px 10px;
}}
QPushButton#winbtn:hover {{ color: {t['paper']}; }}
QPushButton#themebtn {{
  background: transparent; color: {t['dim']}; border: none;
  font-size: 12px; letter-spacing: 2px; padding: 2px 12px;
}}
QPushButton#themebtn:hover {{ color: {t['accent']}; }}
QLabel#youNow {{
  color: {t['accent']}; font-size: 15px; letter-spacing: 1px; margin-top: 18px;
}}
QLabel#replyNow {{
  color: {t['paper']}; font-size: {reply_px}px; font-weight: 300;
}}
QLabel#youOld {{ color: {t['you_old']}; font-size: 13px; margin-top: 18px; }}
QLabel#replyOld {{ color: {t['reply_old']}; font-size: 17px; font-weight: 300; }}
QLabel#toolLine {{ color: {t['faint']}; font-size: 13px; font-style: italic; }}
QLabel#footer {{ color: {t['faint']}; font-size: 12px; letter-spacing: 1px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 4px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['faint']}; border-radius: 2px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLineEdit#cmd {{
  background: transparent; border: none; border-bottom: 1px solid {t['faint']};
  padding: 8px 2px; font-size: 16px; color: {t['paper']};
  selection-background-color: {t['sel']};
}}
QLineEdit#cmd:focus {{ border-bottom: 1px solid {t['accent']}; }}
QLabel#epigraph {{
  color: {t['faint']}; font-size: 21px; font-weight: 300; letter-spacing: 4px;
}}
QLabel#clock {{ color: {t['dim']}; font-size: 13px; letter-spacing: 2px; }}
QLabel#paneltitle {{
  color: {t['dim']}; font-size: 12px; letter-spacing: 4px; font-weight: 600;
}}
QLabel#optlabel {{ color: {t['dim']}; font-size: 12px; letter-spacing: 1px; }}
QPushButton#optbtn {{
  background: transparent; color: {t['dim']}; border: none;
  font-size: 13px; padding: 3px 8px; text-align: left;
}}
QPushButton#optbtn:hover {{ color: {t['paper']}; }}
QPushButton#optbtn[on="true"] {{ color: {t['accent']}; }}
QPushButton#actbtn {{
  background: transparent; color: {t['paper']}; border: none;
  border-bottom: 1px solid {t['faint']}; font-size: 13px; padding: 3px 10px;
}}
QPushButton#actbtn:hover {{ color: {t['accent']};
  border-bottom: 1px solid {t['accent']}; }}
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
        if self.state == "thinking":
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

        lay = QVBoxLayout(self)
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

        row("theme", "theme", THEME_ORDER, self.win.set_theme_opt)
        row("text size", "text", list(TEXT_SIZES), self.win.set_text_opt)
        row("voice", "voice", ["on", "off"], self.win.set_voice_opt)
        row("voice level", "level", list(VOICE_LEVELS), self.win.set_level_opt)
        row("wake word", "wake", ["on", "off"], self.win.set_wake_opt)
        row("microphone", "mic", ["live", "muted"], self.win.set_mic_opt)
        row("window", "window", ["normal", "on top"], self.win.set_ontop_opt)

        lay.addSpacing(10)
        lay.addWidget(self._mklabel("actions"))
        h = QHBoxLayout()
        h.setSpacing(14)
        for text, cb in (("copy reply", self.win.copy_last_reply),
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
        self.resize(1100, 800)
        self.setAcceptDrops(True)
        self.on_submit = None
        self.on_files = None
        self._drag: QPoint | None = None
        self._reply_label: QLabel | None = None
        self._you_label: QLabel | None = None
        self._t0 = time.time()
        self._model = "sonnet 5"
        self._statew = "booting"
        self._status_hold = 0.0  # until this time, hud_tick may not stomp
        self._usage = ""

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
        bar.addWidget(self.theme_btn)
        bar.addWidget(self.gear_btn)
        bar.addSpacing(10)
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
        # Empty-state epigraph — one quiet line until the first exchange.
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
        page = QHBoxLayout()
        page.setContentsMargins(0, 10, 0, 0)
        page.addStretch(4)
        page.addWidget(self.scroll, stretch=7)
        page.addStretch(4)
        lay.addLayout(page, stretch=1)

        # ---- command field (always there — speak or type, both first-class) ----
        self.input = QLineEdit()
        self.input.setObjectName("cmd")
        self.input.setPlaceholderText("speak — or type here, enter to send")
        self.input.returnPressed.connect(self._submit)
        cmd_row = QHBoxLayout()
        cmd_row.setContentsMargins(0, 0, 0, 6)
        cmd_row.addStretch(4)
        cmd_row.addWidget(self.input, stretch=7)
        cmd_row.addStretch(4)
        lay.addLayout(cmd_row)

        # ---- footer: one quiet line ----
        self.footer = QLabel("")
        self.footer.setObjectName("footer")
        foot_row = QHBoxLayout()
        foot_row.setContentsMargins(34, 4, 34, 18)
        foot_row.addWidget(self.footer)
        foot_row.addStretch(1)
        hint = QLabel("ctrl+k type · ctrl+, settings · ctrl+t theme · ctrl+o file · f11 screen")
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
        elif self.isFullScreen():
            self.showNormal()

    # ---- theme / appearance ----
    def apply_theme(self, name: str):
        t = THEMES.get(name) or THEMES["ember"]
        self._theme_name = name
        self.cfg["theme"] = name
        self.setStyleSheet(build_style(t, TEXT_SIZES[self.cfg["text"]]))
        self.canvas.set_theme(t)
        self.string.set_theme(t)
        self.fade.set_theme(t)
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
        if (ev.button() == Qt.LeftButton and not self.isFullScreen()
                and ev.position().y() < self._titlebar_h):
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _ev):
        self._drag = None

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
        usage = f" · {self._usage}" if self._usage else ""
        mic = "mic muted" if (self.goat and self.goat.mic_muted) else "mic live"
        self.footer.setText(f"{self._model} · {mic} · {up // 60:02d}:{up % 60:02d}{usage}")
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
        elif kind == "model":
            self._model = data  # the engine sends the display name
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
        elif kind == "turn_done":
            # Do NOT drop _reply_label here: the model finishes generating
            # seconds before the voice finishes speaking (often before it
            # even starts), and the word reveal keeps landing in this label
            # until the next "you" resets it. Nulling it here is what made
            # the screen stay permanently blank.
            self.stateword.setText("listening")


def main():
    # Import here so a Qt-less headless run of goat_app.py never pays for it.
    from goat_app import GoatApp

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

    goat = GoatApp(emit=win.post_event)
    win.on_submit = goat.submit_text
    win.on_files = goat.submit_files
    win.bind_engine(goat)

    def engine():
        import asyncio
        import traceback
        try:
            asyncio.run(goat.run())
        except Exception:  # noqa: BLE001 — surface it, don't die silently
            traceback.print_exc()
            win.post_event("status", "engine crashed — check python\\goat-app.log")
        else:
            win.post_event("status", "engine stopped")

    t = threading.Thread(target=engine, daemon=True)
    t.start()

    timer = QTimer()
    def tick():
        if goat.audio.is_tts_playing:
            state = "speaking"
            level = goat.audio.out_level * 6
        elif goat.busy:
            state = "thinking"
            level = 0.25
        else:
            level = (goat.audio._raw_rms_ema or 0.0) * 12
            state = "listening" if level > 0.35 else "idle"
        win.hud_tick(level, state, state in ("idle", "listening"))
        win.update_spoken(goat.tts.spoken_text())
    timer.timeout.connect(tick)
    timer.start(33)

    win.showFullScreen()
    win.string.ignite()  # boot ritual: the light travels down the string
    code = app.exec()
    goat.shutdown_audio()
    os._exit(code)  # asyncio daemon thread has no clean cross-thread stop


if __name__ == "__main__":
    main()
