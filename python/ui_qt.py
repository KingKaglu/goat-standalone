"""GOAT's face: a native, frameless, fullscreen Qt window. No browser.

Run:  cd C:/Users/user/goat-standalone/python && python ui_qt.py

Design (v7 — "Goat, your personal assistant", 2026-09-30, from his concept):
- Three columns on a midnight room: a left rail (goat mark, pages, a live
  presence card), the centre (greeting over a painted mountain range, quick
  actions, recent exchanges, the composer), and a right rail (search, the
  brain in use, tools / live activity, system switches, today).
- Built from what the older faces taught, not from the mockup's fiction:
  every card is a real GOAT capability (sight, hands, reflexes, web, skills,
  memory.md) — no invented models or features. The spoken reply still
  reveals word-for-word with the voice; the work log (steps, thinking,
  context meter) lives in the right rail's Activity card; collapse still
  goes to the dot + message card; Snap, the remembered box, the global zoom,
  themes and every shortcut carry over.
- The mountain scenery is painted (seeded ridges, snow, stars) and cached —
  no image files, no per-frame cost.

Older design notes (v6 — "the instrument, not the spaceship", now with margins):
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
- Footer is one small line of plain words: state · model · uptime. The
  shortcut hints give up their space to it rather than overprint (v6).
- v6, after reading the rendered window at 100% and 175%: the transcript is a
  real reading column (reading margins, measure capped near 78 characters),
  the page's BONES scale with the global zoom instead of only its type, and
  the scroll rails stopped reading as hard rules down the edges.

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
import re
import sys
import threading
import time

# Before numpy loads (2026-10-01, RAM): OpenBLAS reserves a work buffer per
# CPU thread at import — ~360 MB committed in this process for matrix math
# GOAT never does (audio blocks are tiny; the VAD runs in onnxruntime).
# One thread: same speed for GOAT's work, measured.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np  # noqa: E402
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QSize,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QPixmap,
    QRadialGradient,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
import subprocess

# tts_edge is imported where it's used: at the top it pulled scipy.signal
# and edge_tts (~2s) in FRONT of the window. The engine thread loads it
# anyway, behind the first paint.
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
    # v7 (his concept, 2026-09-30): midnight navy, one electric-blue accent,
    # a dusk glow behind the mountains. The default room of the new face.
    "midnight": {
        "bg_top": "#0b1324", "bg_bot": "#060a14",
        "paper": "#e9eefa", "dim": "#8e9ab4", "faint": "#56627d",
        # v8 (2026-10-24): accent softened to #5aa2ff (the voice), Claude's
        # own colour added as "work".
        "accent": "#5aa2ff", "accent2": "#8cc0ff", "string_base": "#3a4a6b",
        "you_old": "#b9c6e0", "reply_old": "#aab4c8", "sel": "#1d3b6e",
        "rail": "#070c18", "card": "#0f1a2e", "line": "#1c2a44",
        "glow": "#6a4d63", "ok": "#4fd18b", "danger": "#ff6b5e",
        "work": "#f2b36b",
    },
    # The HUD (his order 2026-09-26: "the whole interface, like JARVIS").
    # "hud" switches on the painted layer — arc reactor, grid, corner
    # brackets — so every other theme stays exactly the instrument it was.
    "jarvis": {         # deep-space navy, arc-reactor cyan
        "bg_top": "#081521", "bg_bot": "#030a11",
        "paper": "#e2f7ff", "dim": "#82b5c9", "faint": "#39677e",
        "accent": "#3fd6ff", "string_base": "#2f86a6",
        "you_old": "#66c3e2", "reply_old": "#9fc6d5", "sel": "#0e4a66",
        "work": "#ffb35e", "hud": True,
    },
    "ember": {          # the original: warm dark, amber string
        "bg_top": "#1a1713", "bg_bot": "#14110e",
        "paper": "#f4ede0", "dim": "#948d7e", "faint": "#5c5649",
        "accent": "#ffb35e", "string_base": "#948d7e",
        "you_old": "#c09263", "reply_old": "#aca395", "sel": "#5a4429",
        "work": "#8fb8ff",
    },
    "paper": {          # daylight: warm paper, ink type, vermilion accent
        "bg_top": "#f6f1e7", "bg_bot": "#efe9db",
        "paper": "#1e1b16", "dim": "#7a7365", "faint": "#a29a86",
        "accent": "#c74a24", "string_base": "#7a7365",
        "you_old": "#a05a3c", "reply_old": "#5f5a50", "sel": "#f0c9b8",
        "work": "#2f6fb0", "ok": "#1f9d55", "danger": "#c0392b",
    },
    "phosphor": {       # night instrument: green-black, phosphor trace
        "bg_top": "#101812", "bg_bot": "#0c120d",
        "paper": "#e2f0e4", "dim": "#82997f", "faint": "#4a5f50",
        "accent": "#66f096", "string_base": "#82997f",
        "you_old": "#6aae80", "reply_old": "#9db3a1", "sel": "#265a38",
        "work": "#f0d066",
    },
    "graphite": {       # mono: near-black, white-hot string
        "bg_top": "#18181b", "bg_bot": "#121214",
        "paper": "#f0f0f2", "dim": "#94949b", "faint": "#5a5a63",
        "accent": "#ffffff", "string_base": "#94949b",
        "you_old": "#b6b6bc", "reply_old": "#a8a8af", "sel": "#44444f",
        "work": "#9aa0ff",
    },
}
THEME_ORDER = ["midnight", "jarvis", "ember", "paper", "phosphor", "graphite"]


def _hex_mix(a: str, b: str, f: float) -> str:
    ca, cb = QColor(a), QColor(b)
    return QColor(int(ca.red() + (cb.red() - ca.red()) * f),
                  int(ca.green() + (cb.green() - ca.green()) * f),
                  int(ca.blue() + (cb.blue() - ca.blue()) * f)).name()


def _complete_theme(t: dict) -> dict:
    """The v7 face needs a few more tokens than the older rooms defined
    (rail, card, line, glow…). Derive them so every theme still works; a
    theme that sets them itself (midnight) keeps its hand-tuned values."""
    t = dict(t)
    t.setdefault("rail", _hex_mix(t["bg_bot"], "#000000", 0.25))
    t.setdefault("card", _hex_mix(t["bg_top"], t["paper"], 0.05))
    t.setdefault("line", _hex_mix(t["bg_top"], t["paper"], 0.12))
    t.setdefault("accent2", _hex_mix(t["accent"], t["paper"], 0.35))
    t.setdefault("glow", _hex_mix(t["bg_top"], t["accent"], 0.35))
    t.setdefault("ok", "#4fd18b")
    # v8 tokens (2026-10-24). surface/surface2/hairline are paper laid over
    # the room at 5.5% / 9% / 6%: one neutral lift instead of navy boxes.
    # "work" is Claude's colour and goes ONLY on Claude's marks; "danger"
    # replaces the hard-coded reds.
    t.setdefault("danger", "#ff6b5e")
    t.setdefault("work", "#f2b36b")
    t.setdefault("surface", _hex_mix(t["bg_top"], t["paper"], 0.055))
    t.setdefault("surface2", _hex_mix(t["bg_top"], t["paper"], 0.09))
    t.setdefault("hairline", _hex_mix(t["bg_top"], t["paper"], 0.06))
    return t


for _n in THEMES:
    THEMES[_n] = _complete_theme(THEMES[_n])


# Reply type sizes (px) for the line GOAT is saying now. v8 (2026-10-24):
# the current reply is the hero again (27), older replies drop to 0.78x in
# Light, so the newest line reads first.
TEXT_SIZES = {"small": 24, "normal": 27, "large": 31}
REPLY_OLD_K = 0.78
# Manual brain roster (his order 2026-07-17): three independent roles he sets
# by hand from the drawer — no auto-routing, no escalation. Values are the
# display names the engine (goat_app) understands directly.
#   working brain  — the one brain (2026-09-23: the talking brain is gone);
#                    it hears everything, sees the desktop, and speaks.
#   hard brain     — the same, on the heavier model.
# Roster refreshed 2026-09-14: Opus 5 replaces Opus 4.8, Fable 5.1 replaces
# Fable 5. Opus 5 leads because Fable bills from a credit bucket his account
# doesn't have (measured) — picking Fable still works, the engine just falls
# back to Opus 5 instead of dead-ending the work lane.
# 2026-09-25: Opus 5.5 replaces Opus 5 as the default (his order).
WORK_OPTS = ["opus 5.5", "fable 5.1"]
# Old names, so a saved config from before the refresh lands on the model
# that replaced its pick instead of being silently reset.
WORK_OPTS_RENAMED = {"fable 5": "fable 5.1", "opus 5": "opus 5.5",
                     "opus 4.8": "opus 5.5", "opus 4.6": "opus 5.5",
                     "sonnet 4.6": "opus 5.5"}
# Thinking depth on the work lane (his order 2026-09-14: "highest thinking").
# Same ladder the API exposes; "max" is the top and the default.
EFFORT_OPTS = ["low", "medium", "high", "xhigh", "max"]
# Global interface zoom — one factor scales EVERY font/padding in the app.
# Drawer offers presets; voice can set any value in [MIN,MAX] via set_ui_scale.
UI_SCALES = {"100%": 1.0, "125%": 1.25, "150%": 1.5, "175%": 1.75, "200%": 2.0}
UI_SCALE_MIN, UI_SCALE_MAX = 0.7, 2.5
# GOAT's speaker level (multiplier on the synthesized voice only).
VOICE_LEVELS = {"quiet": 0.6, "normal": 1.0, "loud": 1.4}
# Who GOAT sounds like — the voices themselves live in tts_edge.CHARACTERS.
VOICE_CHARACTERS = ["goat", "ultron"]

# "ორივე" (both) = the bilingual ear: scribe auto-detects each utterance
# and GOAT answers in whatever language that utterance was in.
LANGS = {"english": "en", "ქართული": "ka", "ორივე": "auto"}

# Friendly UI-part name → palette key(s) the color tool can override.
COLOR_PARTS = {"text": ["paper"], "accent": ["accent"],
               "background": ["bg_top", "bg_bot"]}

# Bumped when a new face should re-seat the theme once (v7 → midnight).
DESIGN_VERSION = 7

DEFAULT_CFG = {"theme": "midnight", "text": "normal", "voice": True,
               "level": "normal", "wake": True, "ontop": False,
               "lang": "en", "character": "goat",
               "work_model": "fable 5.1", "hard_model": "fable 5.1",
               "effort": "high", "last_lang": "en",
               "scale": 1.0, "colors": {},
               "geom": None,    # [x, y, w, h] — remembered window box
               "bubble": None,  # [x, y] — remembered collapsed-bubble corner
               "design": 0}     # last DESIGN_VERSION this config has seen


def load_ui_config() -> dict:
    cfg = dict(DEFAULT_CFG)
    try:
        # utf-8-SIG: PowerShell's `Out-File -Encoding utf8` (5.1) writes a
        # BOM, and a BOM made json.load throw — which silently reset every
        # preference he had (theme, brains, effort) to the defaults. Reading
        # BOM-tolerantly costs nothing and makes that class of edit safe.
        with open(UI_CONFIG, encoding="utf-8-sig") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in cfg})
    except (OSError, json.JSONDecodeError):
        pass
    # A new face arrives in its own room once; after that his pick sticks.
    if not isinstance(cfg.get("design"), int) or cfg["design"] < DESIGN_VERSION:
        cfg["theme"] = "midnight"
        cfg["design"] = DESIGN_VERSION
    if cfg["theme"] not in THEMES:
        cfg["theme"] = "midnight"
    if cfg["text"] not in TEXT_SIZES:
        cfg["text"] = "normal"
    if cfg["level"] not in VOICE_LEVELS:
        cfg["level"] = "normal"
    if cfg["lang"] not in LANGS.values():
        cfg["lang"] = "en"
    if cfg["character"] not in VOICE_CHARACTERS:
        cfg["character"] = "goat"
    cfg.pop("talk_brain", None)      # retired 2026-09-23 (one brain)
    for key in ("work_model", "hard_model"):
        if cfg[key] not in WORK_OPTS:
            cfg[key] = WORK_OPTS_RENAMED.get(cfg[key], "opus 5.5")
    if cfg["effort"] not in EFFORT_OPTS:
        cfg["effort"] = "max"
    if cfg["last_lang"] not in ("en", "ka"):
        cfg["last_lang"] = "en"
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
#
# 2026-07-20 "instrument, refined" (Giorgi picked direction A):
#   voice  — Segoe UI Variable Display for GOAT's spoken lines (Georgian
#            glyphs fall through to plain Segoe UI automatically),
#   machine — Cascadia Mono for everything the MACHINE says about itself
#            (clock, state, steps, models, shortcuts, meters),
#   bones  — hairline rules at ~40% of the theme's faint tone.
# Palettes, the string, and the one-accent law are untouched.
VOICE_FONT = "'Segoe UI Variable Display', 'Segoe UI'"
BODY_FONT = "'Segoe UI Variable Text', 'Segoe UI'"
MONO_FONT = "'Cascadia Mono', 'Consolas'"


# ---- the page's bones (v7, 2026-09-30) ----
# Every size below is at 100% and passes through the global zoom in
# _apply_metrics(), so at 200% the furniture grows with the type (the v6
# lesson: scaling only fonts crowded big type into small rooms).
RAIL_W = 256                      # left rail: mark, pages, presence
SIDE_W = 348                      # right rail: search, brain, tools, system
# v8: margins sit on the 4/8/16/24/32 spacing scale.
CENTER_MARGIN = (32, 24, 32, 16)  # centre column
RAIL_MARGIN = (24, 24, 24, 24)
SIDE_MARGIN = (16, 12, 16, 16)
TITLEBAR_H = 52                   # the drag band along the top edge
# The chat transcript is still the one true reading column: reading margins
# and a measure, never a spreadsheet row (v6).
READ_MARGIN = (6, 0, 18, 0)
READ_MEASURE_CH = 72
WORK_MARGIN = (16, 16, 16, 16)    # inside the Activity card

# Segoe Fluent Icons (Win 11) with MDL2 as the Win 10 fallback. Glyphs are
# private-use codepoints; a name table keeps the call sites readable.
ICON_FONT = "'Segoe Fluent Icons', 'Segoe MDL2 Assets'"
IC = {
    "home": "", "chat": "", "skills": "", "files": "",
    "memory": "", "tools": "", "settings": "",
    "search": "", "attach": "", "mic": "", "send": "",
    "mute": "", "globe": "", "eye": "", "code": "",
    "desktop": "", "bolt": "", "doc": "", "photo": "",
    "volume": "", "chev": "", "more": "", "clock": "",
    "min": "", "max": "", "restore": "", "close": "",
    "work": "", "book": "", "money": "", "game": "",
    "video": "", "music": "", "copy": "", "add": "",
    "folder": "", "lang": "", "brain": "", "hand": "",
    "sync": "", "down": "", "open": "", "check": "",
    "phone": "\ue8ea", "mail": "\ue715", "bell": "\uea8f",
}


def hairline(t: dict, alpha: int = 100) -> str:
    """rgba() of the theme's faint tone — rules that whisper, not shout."""
    return rgba(t["faint"], alpha)


def rgba(hexcol: str, alpha: int) -> str:
    c = QColor(hexcol)
    return f"rgba({c.red()},{c.green()},{c.blue()},{max(0, min(255, alpha))})"


def build_style(t: dict, reply_px: int = 20, scale: float = 1.0) -> str:
    def s(px: float) -> int:
        return max(1, round(px * scale))
    reply = max(1, round(reply_px * scale))
    acc, acc2 = t["accent"], t["accent2"]
    # v8 surfaces: paper lifted off the room, not navy boxes. Mostly opaque
    # so the painted scenery behind a card never fights the words on it.
    card = rgba(t["surface"], 215)
    card_hi = rgba(t["surface2"], 235)
    line = rgba(t["paper"], 15)          # hairline: paper at 6%
    ring = rgba(acc, 102)                # focus / hover ring: accent at 40%
    tint = rgba(acc, 34)
    tint_hi = rgba(acc, 60)
    danger, work = t["danger"], t["work"]
    danger_soft = _hex_mix(danger, t["paper"], 0.25)
    # Labels: 11 px mono caps, +1.6 px tracking (caps set by mklabel).
    track = f"{1.6 * scale:.1f}"
    return f"""
QWidget {{ color: {t['paper']}; font-family: {BODY_FONT}; font-size: {s(15)}px; }}
QToolTip {{ color: {t['paper']}; background: {t['card']}; border: 1px solid {line};
  padding: {s(4)}px {s(8)}px; }}

/* ---- icons ---- */
QLabel#ico, QLabel#icoAcc, QLabel#icoTile {{ font-family: {ICON_FONT}; }}
QLabel#ico {{ color: {t['dim']}; font-size: {s(17)}px; }}
QLabel#icoAcc {{ color: {acc2}; font-size: {s(18)}px; }}
QLabel#icoTile {{
  color: {acc2}; font-size: {s(17)}px; background: {rgba(acc, 30)};
  border: 1px solid {rgba(acc, 55)}; border-radius: {s(10)}px;
}}

/* ---- left rail ---- */
QLabel#brand {{ color: {t['paper']}; font-family: {VOICE_FONT};
  font-size: {s(27)}px; font-weight: 600; }}
QLabel#brandSub {{ color: {t['dim']}; font-size: {s(12)}px; }}
QPushButton#nav {{
  background: transparent; color: {t['dim']}; border: 1px solid transparent;
  border-radius: {s(10)}px; text-align: left; font-size: {s(15)}px;
  padding: {s(10)}px {s(16)}px;
}}
QPushButton#nav:hover {{ background: {rgba(t['paper'], 12)}; color: {t['paper']}; }}
QPushButton#nav:pressed {{ background: {rgba(t['paper'], 6)}; }}
QPushButton#nav[on="true"] {{
  color: {t['paper']}; border: 1px solid {rgba(acc, 60)}; background: {rgba(acc, 38)};
}}
QFrame#presence {{ background: {card}; border: 1px solid {line}; border-radius: {s(16)}px; }}
QLabel#statedot {{ color: {t['ok']}; font-size: {s(11)}px; }}
QLabel#statedot[mood="busy"] {{ color: {acc2}; }}
QLabel#statedot[mood="off"] {{ color: {t['faint']}; }}
QLabel#statedot[mood="bad"] {{ color: {danger}; }}
QLabel#stateword {{ color: {t['paper']}; font-family: {MONO_FONT}; font-size: {s(11)}px;
  font-weight: 500; letter-spacing: {track}px; }}
QLabel#footer {{ color: {t['faint']}; font-family: {MONO_FONT}; font-size: {s(11)}px; }}

/* ---- centre ---- */
QLabel#hello {{ color: {t['paper']}; font-family: {VOICE_FONT};
  font-size: {s(44)}px; font-weight: 300; }}
QLabel#helloSub {{ color: {t['dim']}; font-size: {s(15)}px; }}
QLabel#brainsLine {{ font-family: {MONO_FONT}; font-size: {s(11)}px; }}
QLabel#ambient {{ color: {t['faint']}; font-size: {s(13)}px; }}
QFrame#actionCard {{
  background: {card}; border: 1px solid {line}; border-radius: {s(16)}px;
  text-align: left; padding: 0;
}}
QFrame#actionCard:hover {{ background: {card_hi}; border: 1px solid {ring}; }}
QFrame#actionCard[on="true"] {{ border: 1px solid {acc}; background: {tint}; }}
QLabel#cardTitle {{ color: {t['paper']}; font-size: {s(15)}px; font-weight: 500; }}
QLabel#cardSub {{ color: {t['dim']}; font-size: {s(13)}px; }}
QLabel#chev {{ color: {t['faint']}; font-family: {ICON_FONT}; font-size: {s(10)}px; }}
QFrame#card {{ background: {card}; border: 1px solid {line}; border-radius: {s(16)}px; }}
QLabel#sectionTitle, QLabel#paneltitle {{ color: {t['dim']}; font-family: {MONO_FONT};
  font-size: {s(11)}px; font-weight: 500; letter-spacing: {track}px; }}
QLabel#pageTitle {{ color: {t['paper']}; font-family: {VOICE_FONT};
  font-size: {s(32)}px; font-weight: 300; }}
QLabel#pageSub {{ color: {t['dim']}; font-size: {s(15)}px; }}
QPushButton#link {{ background: transparent; border: none; color: {acc2};
  font-size: {s(13)}px; padding: {s(2)}px {s(4)}px; }}
QPushButton#link:hover {{ color: {t['paper']}; }}
QFrame#row {{
  background: transparent; border: none; border-top: 1px solid {rgba(t['line'], 160)};
  text-align: left; padding: 0;
}}
QFrame#row[first="true"] {{ border-top: none; }}
QFrame#row:hover {{ background: {rgba(t['paper'], 10)}; }}
QLabel#rowTitle {{ color: {t['paper']}; font-size: {s(15)}px; font-weight: 500; }}
QLabel#rowSub {{ color: {t['dim']}; font-size: {s(13)}px; }}
QLabel#rowTime {{ color: {t['faint']}; font-family: {MONO_FONT}; font-size: {s(11)}px; }}
QLabel#empty {{ color: {t['faint']}; font-size: {s(15)}px; }}
QPushButton#more {{ background: transparent; border: none; color: {t['faint']};
  font-family: {ICON_FONT}; font-size: {s(13)}px; padding: {s(6)}px; }}
QPushButton#more:hover {{ color: {t['paper']}; }}

/* ---- composer ---- */
QFrame#composer {{
  background: {rgba(t['surface'], 235)}; border: 1px solid {ring};
  border-radius: {s(16)}px;
}}
QFrame#composer[work="true"] {{ border: 1px solid {work}; }}
QLineEdit#cmd {{
  background: transparent; border: none; font-size: {s(16)}px;
  color: {t['paper']}; padding: {s(8)}px {s(4)}px;
  selection-background-color: {t['sel']};
}}
QPushButton#cbtn {{
  background: transparent; border: none; color: {t['dim']};
  font-family: {ICON_FONT}; font-size: {s(17)}px; padding: {s(6)}px;
  border-radius: {s(16)}px;
}}
QPushButton#cbtn:hover {{ color: {t['paper']}; background: {rgba(t['paper'], 14)}; }}
QPushButton#micbtn {{
  background: transparent; border: none; color: {t['dim']};
  font-family: {ICON_FONT}; font-size: {s(17)}px; padding: {s(6)}px;
  border-radius: {s(16)}px;
}}
QPushButton#micbtn:hover {{ color: {t['paper']}; background: {rgba(t['paper'], 14)}; }}
QPushButton#micbtn[muted="true"] {{ color: {danger}; }}
QPushButton#micbtn[hot="true"] {{ color: {acc2}; background: {tint}; }}
QPushButton#sendbtn {{
  background: {acc}; border: none; color: white; font-family: {ICON_FONT};
  font-size: {s(16)}px; border-radius: {s(10)}px;
  min-width: {s(38)}px; max-width: {s(38)}px; min-height: {s(38)}px; max-height: {s(38)}px;
}}
QPushButton#sendbtn:hover {{ background: {acc2}; }}
QPushButton#sendbtn:pressed {{ background: {_hex_mix(acc, '#000000', 0.18)}; }}
QPushButton#chip {{
  background: {card}; border: 1px solid {line}; color: {t['paper']};
  border-radius: {s(10)}px; padding: {s(7)}px {s(14)}px; font-size: {s(13)}px;
}}
QPushButton#chip[slim="true"] {{ padding: {s(7)}px {s(9)}px; min-width: 0px; }}
QPushButton#chip:hover {{ background: {card_hi}; border: 1px solid {ring}; }}
QPushButton#chip:pressed {{ background: {rgba(t['paper'], 6)}; }}
QPushButton#chip[on="true"] {{ border: 1px solid {acc}; background: {tint_hi}; }}

/* ---- right rail ---- */
QFrame#search {{ background: {card}; border: 1px solid {line}; border-radius: {s(12)}px; }}
QLineEdit#searchField {{ background: transparent; border: none; color: {t['paper']};
  font-size: {s(14)}px; padding: {s(8)}px {s(2)}px; selection-background-color: {t['sel']}; }}
QLabel#kbd {{ color: {t['dim']}; font-family: {MONO_FONT}; font-size: {s(11)}px; border: 1px solid {line};
  border-radius: {s(5)}px; padding: {s(1)}px {s(6)}px; }}
QPushButton#winbtn {{
  background: transparent; color: {t['dim']}; border: none; border-radius: {s(6)}px;
  font-family: {ICON_FONT}; font-size: {s(11)}px; padding: {s(7)}px {s(12)}px;
}}
QPushButton#winbtn:hover {{ color: {t['paper']}; background: {rgba(t['paper'], 16)}; }}
QPushButton#winclose:hover {{ background: #c42b1c; color: white; }}
QPushButton#winclose {{
  background: transparent; color: {t['dim']}; border: none; border-radius: {s(6)}px;
  font-family: {ICON_FONT}; font-size: {s(11)}px; padding: {s(7)}px {s(12)}px;
}}
QPushButton#gear {{ background: transparent; border: none; color: {t['dim']};
  font-family: {ICON_FONT}; font-size: {s(16)}px; padding: {s(6)}px; border-radius: {s(16)}px; }}
QPushButton#gear:hover {{ color: {t['paper']}; background: {rgba(t['paper'], 14)}; }}
QProgressBar#planbar {{ background: {rgba(t['paper'], 16)}; border: none; border-radius: {s(4)}px; max-height: {s(8)}px; min-height: {s(8)}px; }}
QProgressBar#planbar::chunk {{ background: {acc}; border-radius: {s(4)}px; }}
QProgressBar#planbar[hot="true"]::chunk {{ background: {danger_soft}; }}
QFrame#sideCard {{
  background: {card}; border: 1px solid {line}; border-radius: {s(16)}px;
  text-align: left; padding: 0;
}}
QFrame#sideCard:hover {{ background: {card_hi}; border: 1px solid {ring}; }}
QLabel#modelName {{ color: {t['paper']}; font-size: {s(19)}px; font-weight: 600; }}
QLabel#pill {{ color: {t['ok']}; background: {rgba(t['ok'], 38)}; border-radius: {s(9)}px;
  font-size: {s(11)}px; font-weight: 600; padding: {s(2)}px {s(9)}px; }}
QLabel#pill[off="true"] {{ color: {t['dim']}; background: {rgba(t['paper'], 18)}; }}
QLabel#pill[warn="true"] {{ color: {danger_soft}; background: {rgba(danger, 40)}; }}
QLabel#pillAcc {{ color: {acc2}; background: {rgba(acc, 40)}; border-radius: {s(9)}px;
  font-size: {s(11)}px; font-weight: 600; padding: {s(2)}px {s(9)}px; }}
QLabel#quote {{ color: {t['paper']}; font-family: {VOICE_FONT}; font-size: {s(15)}px;
  font-style: italic; }}
QLabel#clock {{ color: {t['dim']}; font-family: {MONO_FONT}; font-size: {s(11)}px; }}

/* ---- chat ---- */
QLabel#youNow, QLabel#youOld {{
  background: {rgba(acc, 46)}; border: 1px solid {rgba(acc, 80)};
  border-radius: {s(16)}px; font-size: {s(15)}px;
}}
QLabel#youNow {{ color: {t['paper']}; }}
QLabel#youOld {{ color: {t['you_old']}; background: {rgba(acc, 26)};
  border: 1px solid {rgba(acc, 45)}; }}
QLabel#replyNow {{ color: {t['paper']}; font-family: {VOICE_FONT};
  font-size: {reply}px; font-weight: 400; }}
QLabel#replyOld {{ color: {t['reply_old']}; font-family: {VOICE_FONT};
  font-size: {max(1, round(reply * REPLY_OLD_K))}px; font-weight: 300; }}
QLabel#toolLine {{ color: {t['faint']}; font-family: {MONO_FONT}; font-size: {s(11)}px; }}
QLabel#notice {{ color: {danger_soft}; background: {rgba(danger, 26)};
  border: 1px solid {rgba(danger, 70)}; border-radius: {s(12)}px; font-size: {s(15)}px; }}
QLabel#epigraph {{ color: {t['faint']}; font-family: {VOICE_FONT};
  font-size: {s(22)}px; font-weight: 300; }}
QLabel#orbCaption {{ color: {t['dim']}; font-family: {MONO_FONT}; font-size: {s(11)}px;
  letter-spacing: {track}px; }}
QLabel#speaker {{ color: {acc2}; font-family: {MONO_FONT}; font-size: {s(11)}px;
  font-weight: 500; letter-spacing: {track}px; }}
QLabel#doc {{ color: {t['paper']}; font-size: {s(15)}px; }}

/* ---- settings ---- */
QLabel#optlabel {{ color: {t['dim']}; font-size: {s(13)}px; }}
QPushButton#optbtn {{
  background: {rgba(t['paper'], 8)}; color: {t['dim']}; border: 1px solid {line};
  border-radius: {s(10)}px; font-size: {s(13)}px; padding: {s(5)}px {s(12)}px;
}}
QPushButton#optbtn:hover {{ color: {t['paper']}; border: 1px solid {ring}; }}
QPushButton#optbtn:pressed, QPushButton#actbtn:pressed {{ background: {rgba(t['paper'], 4)}; }}
QPushButton#optbtn[on="true"] {{ color: {t['paper']}; background: {tint_hi};
  border: 1px solid {acc}; }}
QPushButton#actbtn {{
  background: {rgba(t['paper'], 8)}; color: {t['paper']}; border: 1px solid {line};
  border-radius: {s(10)}px; font-size: {s(13)}px; padding: {s(7)}px {s(14)}px;
}}
QPushButton#actbtn:hover {{ border: 1px solid {acc}; background: {tint}; }}

/* ---- activity (the working brain's live log) ---- */
QLabel#workmodel {{ color: {work}; font-family: {MONO_FONT}; font-size: {s(11)}px; }}
QLabel#workidle {{ color: {t['faint']}; font-size: {s(13)}px; }}
QLabel#worktask {{ color: {t['paper']}; font-size: {s(15)}px; font-weight: 500; margin-top: {s(8)}px; }}
QLabel#workstep {{ color: {work}; font-family: {MONO_FONT}; font-size: {s(12)}px; }}
QLabel#workdone {{ color: {t['dim']}; font-family: {MONO_FONT}; font-size: {s(12)}px; }}
QLabel#worktext {{ color: {t['dim']}; font-size: {s(12)}px; font-style: italic; }}
QLabel#workvoice {{ color: {t['paper']}; font-size: {s(13)}px; margin-top: {s(4)}px; }}
QLabel#workthink {{
  color: {t['dim']}; font-family: {VOICE_FONT}; font-size: {s(12)}px;
  font-style: italic; padding-left: {s(8)}px;
  border-left: 1px solid {hairline(t, 90)}; margin: {s(2)}px 0; }}
QLabel#workctx {{ color: {t['faint']}; font-family: {MONO_FONT}; font-size: {s(11)}px;
  padding-top: {s(4)}px; }}
QLabel#workfail {{ color: {danger_soft}; font-family: {MONO_FONT}; font-size: {s(12)}px; font-weight: 500; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: {s(8)}px; margin: 0; }}
QScrollBar::handle:vertical {{
  background: {rgba(t['paper'], 34)}; border-radius: {s(3)}px;
  min-height: {s(40)}px; margin: 0 {s(2)}px;
}}
QScrollBar::handle:vertical:hover {{ background: {rgba(t['paper'], 70)}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QMenu {{ background: {t['surface']}; border: 1px solid {line}; padding: {s(4)}px; }}
QMenu::item {{ padding: {s(8)}px {s(16)}px; border-radius: {s(8)}px; }}
QMenu::item:selected {{ background: {tint_hi}; }}
"""


def _qc(hexcol: str, alpha: int = 255) -> QColor:
    c = QColor(hexcol)
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


def _mixc(a: QColor, b: QColor, f: float, alpha: int = 255) -> QColor:
    f = max(0.0, min(1.0, f))
    return QColor(int(a.red() + (b.red() - a.red()) * f),
                  int(a.green() + (b.green() - a.green()) * f),
                  int(a.blue() + (b.blue() - a.blue()) * f), alpha)


def _ridge(w: int, base: float, amp: float, rng: random.Random,
           peak: tuple | None = None, detail: float = 1.0) -> list:
    """One mountain skyline as [(x, y)], left to right. A few slow waves for
    the range, fast small ones for the rock, and an optional summit — a
    sharp, slightly lopsided cone — so the main peak reads as a mountain,
    not a sine wave."""
    waves = [(rng.uniform(0.6, 1.6), rng.uniform(0, math.tau), 1.0),
             (rng.uniform(2.0, 3.5), rng.uniform(0, math.tau), 0.45),
             (rng.uniform(5.0, 8.0), rng.uniform(0, math.tau), 0.18 * detail),
             (rng.uniform(13.0, 19.0), rng.uniform(0, math.tau), 0.07 * detail),
             (rng.uniform(31.0, 43.0), rng.uniform(0, math.tau), 0.03 * detail)]
    norm = sum(a for _, _, a in waves)
    pts = []
    step = max(2, w // 260)
    for x in range(0, w + step, step):
        u = x / max(1, w)
        v = sum(a * math.sin(f * u * math.tau + ph) for f, ph, a in waves) / norm
        y = base - amp * (0.5 + 0.5 * v)
        if peak is not None:
            px, ph_, pw = peak
            d = (u - px) / (pw * (0.8 if u < px else 1.15))
            if abs(d) < 1.0:
                y -= ph_ * (1.0 - abs(d)) ** 1.35
        pts.append(QPointF(x, y))
    return pts


def paint_scene(p: QPainter, rect: QRect, t: dict, seed: int = 7,
                peak_x: float = 0.64, stars: int = 140, trees: bool = False,
                fade_to: QColor | None = None):
    """Night mountains at dusk, painted — the concept's photograph without a
    photograph. Deterministic per seed, so it never shimmers between paints.
    Colours come from the theme, so the light rooms get misty day ranges."""
    w, h = rect.width(), rect.height()
    if w < 4 or h < 4:
        return
    rng = random.Random(seed)
    p.save()
    p.translate(rect.topLeft())
    p.setClipRect(QRect(0, 0, w, h))
    p.setRenderHint(QPainter.Antialiasing, True)
    bg_top, bg_bot = QColor(t["bg_top"]), QColor(t["bg_bot"])
    paper, glow, acc = QColor(t["paper"]), QColor(t["glow"]), QColor(t["accent"])
    light = paper.lightness() < bg_top.lightness()   # the paper room

    sky = QLinearGradient(0, 0, 0, h)
    sky.setColorAt(0.0, _mixc(bg_top, acc, 0.10))
    sky.setColorAt(0.42, _mixc(bg_top, glow, 0.55))
    sky.setColorAt(0.62, _mixc(bg_top, glow, 0.85))
    sky.setColorAt(1.0, bg_bot)
    p.fillRect(0, 0, w, h, sky)

    if not light:
        for _ in range(stars):
            x, y = rng.uniform(0, w), rng.uniform(0, h * 0.5) ** 1.08
            a = rng.randint(40, 190)
            r = rng.choice((0.6, 0.8, 0.8, 1.1, 1.5))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, a))
            p.drawEllipse(QPointF(x, y), r, r)

    def fill(pts, col_top: QColor, col_bot: QColor, top_y: float):
        path = QPainterPath(QPointF(0, h))
        for pt in pts:
            path.lineTo(pt)
        path.lineTo(w, h)
        path.closeSubpath()
        g = QLinearGradient(0, top_y, 0, h)
        g.setColorAt(0.0, col_top)
        g.setColorAt(1.0, col_bot)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawPath(path)
        return path

    far_c = _mixc(bg_top, glow, 0.45)
    fill(_ridge(w, h * 0.68, h * 0.17, rng, detail=0.7,
                peak=(min(0.95, peak_x + 0.27), h * 0.12, 0.12)),
         _mixc(far_c, paper, 0.12, 235), _mixc(bg_top, bg_bot, 0.4), h * 0.45)

    # The massif: a main summit and a lower shoulder peak beside it.
    main = _ridge(w, h * 0.82, h * 0.10, rng, peak=(peak_x, h * 0.60, 0.27))
    sh = _ridge(w, h * 0.82, 0, random.Random(seed + 1),
                peak=(peak_x - 0.17, h * 0.30, 0.13), detail=0.0)
    main = [QPointF(a.x(), min(a.y(), b.y())) for a, b in zip(main, sh)]
    top_y = min(pt.y() for pt in main)
    body_top = _mixc(bg_top, paper, 0.10)
    mp = fill(main, body_top, bg_bot, top_y)

    def summit_near(u: float, span: float) -> QPointF:
        band = [pt for pt in main if abs(pt.x() / w - u) < span]
        return min(band, key=lambda pt: pt.y()) if band else main[len(main) // 2]

    def spine(top: QPointF, lean: float) -> list:
        """A ragged ridge from a summit down to the valley — the line where
        the lit face turns into shadow."""
        pts, y, x = [], top.y(), top.x()
        steps = 26
        for i in range(steps + 1):
            f = i / steps
            pts.append(QPointF(x, y))
            y = top.y() + (h - top.y()) * (f + 1 / steps)
            x += lean * w / steps + rng.uniform(-1, 1) * w * 0.006
        return pts

    warm = _mixc(glow, paper, 0.45)
    p.save()
    p.setClipPath(mp)
    for u, span, lean, strength in ((peak_x, 0.2, 0.05, 1.0),
                                    (peak_x - 0.17, 0.09, 0.03, 0.7)):
        top = summit_near(u, span)
        sp = spine(top, lean)
        # Lit face: left skyline from the summit down, then back up the spine.
        face = QPainterPath(top)
        left = [pt for pt in main if top.x() - w * 0.30 < pt.x() <= top.x()]
        for pt in reversed(left):
            face.lineTo(pt)
        face.lineTo(QPointF(left[0].x() if left else top.x() - w * 0.3, h))
        for pt in reversed(sp):
            face.lineTo(pt)
        face.closeSubpath()
        g = QLinearGradient(0, top.y(), 0, h * 0.95)
        g.setColorAt(0.0, _mixc(warm, paper, 0.25, int(150 * strength)))
        g.setColorAt(0.35, _mixc(warm, body_top, 0.5, int(90 * strength)))
        g.setColorAt(1.0, _mixc(warm, bg_bot, 1.0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawPath(face)
        # Snow: everything above a ragged snowline near the summit, bright on
        # the lit side and blue-grey in the shade.
        depth = (h * 0.82 - top.y()) * 0.30
        snow = QPainterPath(QPointF(top.x() - w * 0.25, top.y() - 4))
        n = 36
        for i in range(n + 1):
            x = top.x() - w * 0.25 + w * 0.5 * i / n
            dx = abs(x - top.x()) / (w * 0.25)
            y = top.y() + depth * (1 - dx) ** 0.7 * rng.uniform(0.55, 1.15)
            snow.lineTo(QPointF(x, y))
        snow.lineTo(QPointF(top.x() + w * 0.25, top.y() - 4))
        snow.closeSubpath()
        sg = QLinearGradient(top.x() - w * 0.08, 0, top.x() + w * 0.08, 0)
        sg.setColorAt(0.0, _mixc(paper, warm, 0.18, int(215 * strength)))
        sg.setColorAt(0.5, _mixc(paper, warm, 0.25, int(175 * strength)))
        sg.setColorAt(0.52, _mixc(body_top, paper, 0.35, int(120 * strength)))
        sg.setColorAt(1.0, _mixc(body_top, paper, 0.2, int(70 * strength)))
        p.setBrush(sg)
        p.drawPath(snow)
        # Couloirs: a few dark gullies raking down the lit face.
        for _ in range(5 if strength == 1.0 else 2):
            x = top.x() - rng.uniform(0.01, 0.12) * w
            y = top.y() + rng.uniform(0.05, 0.3) * depth
            path = QPainterPath(QPointF(x, y))
            for _s in range(6):
                x += rng.uniform(-0.012, 0.003) * w
                y += depth * rng.uniform(0.22, 0.4)
                path.lineTo(QPointF(x, y))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_mixc(bg_bot, bg_top, 0.3, 70), max(1.0, w / 700)))
            p.drawPath(path)
        p.setPen(Qt.NoPen)
    p.restore()

    near = _ridge(w, h * 0.93, h * 0.10, rng, detail=1.3)
    fill(near, _mixc(bg_bot, bg_top, 0.5, 250), bg_bot, h * 0.8)
    if trees:
        p.setPen(Qt.NoPen)
        p.setBrush(_mixc(bg_bot, QColor(0, 0, 0), 0.35))
        x = -4.0
        while x < w + 8:
            th = rng.uniform(h * 0.05, h * 0.13)
            tw = th * rng.uniform(0.28, 0.4)
            base = h * 0.985 - rng.uniform(0, h * 0.03)
            tri = QPainterPath(QPointF(x - tw / 2, base))
            tri.lineTo(x, base - th)
            tri.lineTo(x + tw / 2, base)
            tri.closeSubpath()
            p.drawPath(tri)
            x += rng.uniform(tw * 0.45, tw * 1.1)
        p.fillRect(QRectF(0, h * 0.97, w, h * 0.03 + 1), bg_bot)

    # Hand off to the room: the lower part dissolves into the page colour so
    # type laid over it sits on quiet ground.
    end = fade_to or bg_bot
    fg = QLinearGradient(0, h * 0.55, 0, h)
    fg.setColorAt(0.0, _mixc(end, end, 0, 0))
    fg.setColorAt(1.0, _mixc(end, end, 0, 255))
    p.fillRect(0, 0, w, h, fg)
    lf = QLinearGradient(0, 0, w * 0.18, 0)      # soft left edge into the rail
    lf.setColorAt(0.0, _mixc(end, end, 0, 150))
    lf.setColorAt(1.0, _mixc(end, end, 0, 0))
    p.fillRect(0, 0, w, h, lf)
    p.restore()


class Backdrop(QWidget):
    """The room: rails, and the mountain scene behind the centre's greeting.
    Drawn once into a pixmap per size/theme — the presence wave repaints
    thirty times a second on top of it and must not re-paint mountains."""

    def __init__(self):
        super().__init__()
        self.t = THEMES["midnight"]
        self.rail_w = RAIL_W
        self.side_w = SIDE_W
        self.dim = 0.0           # veil over the range on the working pages
        self._cache: QPixmap | None = None
        self._cache_key = None

    def set_theme(self, t: dict):
        self.t = dict(t)
        self._cache = None
        self.update()

    def set_rails(self, rail_w: int, side_w: int):
        if (rail_w, side_w) != (self.rail_w, self.side_w):
            self.rail_w, self.side_w = rail_w, side_w
            self._cache = None
            self.update()

    def set_dim(self, dim: float):
        """The mountains are the home page's; behind a conversation or a
        settings page they sink back so the words win."""
        if abs(dim - self.dim) > 0.001:
            self.dim = dim
            self._cache = None
            self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        key = (self.width(), self.height(), self.rail_w, self.side_w, self.dim,
               self.t.get("bg_top"), self.t.get("accent"), self.t.get("glow"))
        if self._cache is None or key != self._cache_key:
            self._cache = self._paint()
            self._cache_key = key
        p.drawPixmap(0, 0, self._cache)

    def _paint(self) -> QPixmap:
        w, h = max(1, self.width()), max(1, self.height())
        pm = QPixmap(w, h)
        p = QPainter(pm)
        t = self.t
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(t["bg_top"]))
        g.setColorAt(1.0, QColor(t["bg_bot"]))
        p.fillRect(0, 0, w, h, g)
        cx0, cx1 = self.rail_w, max(self.rail_w + 1, w - self.side_w)
        # Centre: the range behind the greeting, about the top 58%.
        # Fade into exactly the page colour at the scene's foot, or the
        # light rooms show a seam where the painting stops.
        paint_scene(p, QRect(cx0, 0, cx1 - cx0, int(h * 0.58)), t, seed=11,
                    peak_x=0.74, fade_to=_mixc(QColor(t["bg_top"]),
                                               QColor(t["bg_bot"]), 0.58))
        if self.dim:
            p.fillRect(cx0, 0, cx1 - cx0, h, _qc(t["bg_bot"], int(255 * self.dim)))
        # Left rail: its own darker panel with a small range and pines at
        # the foot, like the concept's sidebar.
        rail = QColor(t["rail"])
        p.fillRect(0, 0, cx0, h, _qc(t["rail"], 238))
        sh = int(h * 0.36)
        p.setOpacity(0.42)
        paint_scene(p, QRect(0, h - sh, cx0, sh), t, seed=5, peak_x=0.3,
                    stars=0, trees=True, fade_to=rail)
        p.setOpacity(1.0)
        top = QLinearGradient(0, h - sh, 0, h - sh + sh * 0.5)
        top.setColorAt(0.0, _qc(t["rail"], 255))
        top.setColorAt(1.0, _qc(t["rail"], 0))
        p.fillRect(0, h - sh, cx0, int(sh * 0.5), top)
        # Right rail: plain and dark — it holds the instruments.
        p.fillRect(cx1, 0, w - cx1, h, _qc(t["rail"], 225))
        p.setPen(QPen(_qc(t["line"], 200), 1))
        p.drawLine(cx0, 0, cx0, h)
        p.drawLine(cx1, 0, cx1, h)
        p.end()
        return pm


def goat_mark_path() -> QPainterPath:
    """The goat's head, front on, in a 100×100 box: sweeping horns, ears out
    to the side, a long shield face and a beard. Filled, one piece — so it
    reads at 20px in the rail and at 90px in the greeting."""
    path = QPainterPath()
    path.setFillRule(Qt.WindingFill)
    for sx in (1, -1):
        def P(x, y):
            return QPointF(50 + sx * (x - 50), y)
        horn = QPainterPath(P(44, 30))
        horn.cubicTo(P(38, 14), P(24, 4), P(6, 8))
        horn.cubicTo(P(16, 10), P(27, 18), P(31, 30))
        horn.cubicTo(P(33, 35), P(37, 38), P(41, 40))
        horn.closeSubpath()
        path.addPath(horn)
        ear = QPainterPath(P(38, 40))
        ear.cubicTo(P(30, 36), P(18, 36), P(9, 42))
        ear.cubicTo(P(18, 48), P(30, 49), P(39, 48))
        ear.closeSubpath()
        path.addPath(ear)
    face = QPainterPath(QPointF(37, 34))
    face.cubicTo(QPointF(44, 30), QPointF(56, 30), QPointF(63, 34))
    face.cubicTo(QPointF(64, 50), QPointF(61, 66), QPointF(55, 78))
    face.lineTo(QPointF(52, 83))
    face.lineTo(QPointF(48, 83))
    face.lineTo(QPointF(45, 78))
    face.cubicTo(QPointF(39, 66), QPointF(36, 50), QPointF(37, 34))
    face.closeSubpath()
    path.addPath(face)
    beard = QPainterPath(QPointF(45, 80))
    beard.lineTo(QPointF(55, 80))
    beard.cubicTo(QPointF(54, 88), QPointF(52, 94), QPointF(50, 99))
    beard.cubicTo(QPointF(48, 94), QPointF(46, 88), QPointF(45, 80))
    beard.closeSubpath()
    path.addPath(beard)
    return path


def goat_mark_cuts() -> QPainterPath:
    """Eyes and the brow line, cut out of the face."""
    cut = QPainterPath()
    for sx in (1, -1):
        eye = QPainterPath(QPointF(50 + sx * -9.5, 50))
        eye.lineTo(QPointF(50 + sx * -4.5, 53.5))
        eye.lineTo(QPointF(50 + sx * -5.5, 55.5))
        eye.lineTo(QPointF(50 + sx * -10.5, 52))
        eye.closeSubpath()
        cut.addPath(eye)
    nose = QPainterPath(QPointF(47.2, 70))
    nose.lineTo(QPointF(52.8, 70))
    nose.lineTo(QPointF(50, 74))
    nose.closeSubpath()
    cut.addPath(nose)
    return cut


def paint_goat_mark(p: QPainter, box: QRectF, t: dict, glow: bool = True):
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    k = min(box.width(), box.height()) / 100.0
    p.translate(box.center().x() - 50 * k, box.center().y() - 50 * k)
    p.scale(k, k)
    shape = goat_mark_path().subtracted(goat_mark_cuts())
    acc, acc2, paper = QColor(t["accent"]), QColor(t["accent2"]), QColor(t["paper"])
    if glow:
        for wdt, a in ((9, 18), (5, 34), (2.4, 60)):
            p.setPen(QPen(_mixc(acc, acc, 0, a), wdt, Qt.SolidLine, Qt.RoundCap,
                          Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPath(shape)
    g = QLinearGradient(0, 0, 0, 100)
    g.setColorAt(0.0, _mixc(acc2, paper, 0.55))
    g.setColorAt(0.55, acc2)
    g.setColorAt(1.0, acc)
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawPath(shape)
    p.restore()


class GoatMark(QWidget):
    def __init__(self, size: int = 64, glow: bool = True):
        super().__init__()
        self._t = THEMES["midnight"]
        self._glow = glow
        self._base = size
        self.set_scale(1.0)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_scale(self, k: float):
        d = max(16, round(self._base * k))
        self.setFixedSize(d, d)

    def set_theme(self, t: dict):
        self._t = dict(t)
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        pad = self.width() * 0.04
        paint_goat_mark(p, QRectF(self.rect()).adjusted(pad, pad, -pad, -pad),
                        self._t, self._glow)


def _ease_k(dt: float, dur: float) -> float:
    """Share of the remaining gap a value closes this frame so it lands
    (~95%) on its target in `dur` seconds: an exponential ease-out."""
    return 1.0 - math.exp(-3.0 * dt / max(0.02, dur))


class Presence(QWidget):
    """GOAT's presence, v8 (design.md, 2026-10-24): the string and the sphere
    become one form, a ring of light threaded on a string. The voice is the
    ring; Claude is a small warm light that orbits it only while a work turn
    runs.

    state      ring                        string
    asleep     dim, faint colour           flat, 25%      no repaint
    idle       70%                         flat           one slow breath / 8 s
    listening  core fills with his level   ripples out from the ring
    thinking   a bright arc runs the ring  fine shiver
    speaking   rings pulse outward         slow waves on the real TTS level
    + work     Claude's light orbits       beads travel right
    error      dashed, red, flinch once    kinks

    Every value eases to its target (150 ms into listening, 300 speaking,
    400 asleep). It repaints only while something moves or a live signal is
    non-zero; at rest the window's 33 ms tick costs a few float compares.
    compact = the rail's status card (34 px tall), same form, smaller.
    """

    STATES = ("asleep", "idle", "listening", "thinking", "speaking", "error")
    # Seconds to blend INTO each state (design.md motion rules).
    BLEND = {"listening": 0.15, "thinking": 0.25, "speaking": 0.3,
             "idle": 0.35, "asleep": 0.4, "error": 0.2}
    BREATH_EVERY, BREATH_LEN = 8.0, 2.4

    def __init__(self, compact: bool = False):
        super().__init__()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.compact = compact
        self.state = "asleep"
        self.work = False
        self.level = 0.0
        self.paints = 0               # paint count (tests, measure.py)
        self._t = 0.0                 # animation clock: runs only while moving
        now = time.monotonic()
        self._last = now
        self._breath_t0 = now
        self._breathing = False
        self._flinch_t0 = 0.0
        self._ignite_t0 = 0.0
        self._ignite_dur = 1.4
        self._frame = 0
        # Blend weight of every state + Claude's light, eased each tick.
        self._v = {s: 0.0 for s in self.STATES}
        self._v["asleep"] = 1.0
        self._v["work"] = 0.0
        self._voice = QColor("#5aa2ff")
        self._workc = QColor("#f2b36b")
        self._danger = QColor("#ff6b5e")
        self._faint = QColor("#56627d")
        self._paper = QColor("#e9eefa")
        self.setMinimumHeight(28 if compact else 72)

    def set_theme(self, t: dict):
        self._voice = QColor(t["accent"])
        self._workc = QColor(t.get("work", "#f2b36b"))
        self._danger = QColor(t.get("danger", "#ff6b5e"))
        self._faint = QColor(t.get("faint", "#56627d"))
        self._paper = QColor(t["paper"])
        self.update()

    def ignite(self, duration: float = 1.4):
        """Boot ritual: the ring draws itself, then the string runs out."""
        self._ignite_dur = duration
        self._ignite_t0 = time.monotonic()
        self.update()

    # ---- the clock ----
    def tick(self, level: float, state: str, work: bool = False) -> bool:
        """Advance one frame. Asks for a paint only if something on it
        moves; returns whether it did (tests read it)."""
        now = time.monotonic()
        dt = max(0.0, min(0.1, now - self._last))
        self._last = now
        state = {"booting": "asleep", "working": "idle"}.get(state, state)
        if state not in self.STATES:
            state = "idle"
        if state != self.state:
            if state == "error":
                self._flinch_t0 = now
            if state == "idle":
                self._breath_t0 = now    # first breath a full cycle later
            self.state = state
        self.work = bool(work)
        lvl = max(0.0, min(1.0, level)) if state in ("listening", "speaking") else 0.0
        # VU needle: fast attack, slower release.
        self.level += (lvl - self.level) * (0.45 if lvl > self.level else 0.12)
        if self.level < 0.004:
            self.level = 0.0
        k_state = _ease_k(dt, self.BLEND.get(state, 0.3))
        k_work = _ease_k(dt, 0.3)
        moving = False
        for name, v in self._v.items():
            if name == "work":
                tg, k = (1.0 if self.work else 0.0), k_work
            else:
                tg, k = (1.0 if name == state else 0.0), k_state
            if v != tg:
                v += (tg - v) * k
                # the last 1.5% snaps (invisible) so the frames stop on time
                self._v[name] = tg if abs(tg - v) < 0.015 else v
                moving = True
        v = self._v
        busy = (moving or self.level > 0.0
                or v["listening"] + v["thinking"] + v["speaking"] + v["work"] > 0.0
                or (self._flinch_t0 and now - self._flinch_t0 < 0.5)
                or (self._ignite_t0 and now - self._ignite_t0 < self._ignite_dur + 0.1))
        want = bool(busy)
        if busy:
            self._t += dt
        elif self.state == "idle":
            # Idle: one slow breath every 8 s, at 15 fps, then still.
            if self._breath(now) > 0.0:
                self._breathing = True
                self._frame += 1
                want = self._frame % 2 == 0
            elif self._breathing:
                self._breathing = False
                want = True              # land the frame exactly at rest
        if want and self.isVisible():
            self.update()
        return want

    def _breath(self, now: float) -> float:
        if self.state != "idle":
            return 0.0
        cyc = (now - self._breath_t0) % self.BREATH_EVERY
        lead = self.BREATH_EVERY - self.BREATH_LEN
        if cyc < lead:
            return 0.0
        return math.sin(math.pi * (cyc - lead) / self.BREATH_LEN) ** 2

    # ---- the picture ----
    def _offsets(self, d, X, side: int, half: float, sa: float):
        """String y-offsets (px) at distances d from the ring (absolute x X)."""
        v, t, lv = self._v, self._t, self.level
        y = np.zeros_like(d)
        if v["listening"] > 0.0:      # ripples running out from the ring
            y += (9 * sa * v["listening"] * (0.3 + 0.7 * lv)
                  * np.exp(-(d / (half * 0.35)) ** 2) * np.sin(d / (9 * sa) - t * 9))
        if v["speaking"] > 0.0:       # slow fat waves on the real voice level
            y += (13 * sa * v["speaking"] * (0.35 + 0.65 * lv)
                  * np.exp(-(d / (half * 0.55)) ** 2)
                  * np.sin(X / (34 * sa) - t * 2.6)
                  * (0.6 + 0.4 * np.sin(X / (97 * sa) + t * 0.7)))
        if v["thinking"] > 0.0:       # a fine shiver
            y += (1.6 * sa * v["thinking"] * np.sin(X / (3.1 * sa) + t * 14)
                  * np.exp(-(d / (half * 0.5)) ** 2))
        if v["error"] > 0.0 and side > 0:   # a kink just off the ring
            y -= 7 * sa * v["error"] * np.exp(-((d - 26 * sa) / (10 * sa)) ** 2)
        return y

    def paintEvent(self, _ev):
        self.paints += 1
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        now = time.monotonic()
        w, h = self.width(), self.height()
        if w < 20 or h < 12:
            return
        v = self._v
        asleep, idle, err = v["asleep"], v["idle"], v["error"]
        lis, thk, spk, wrk = v["listening"], v["thinking"], v["speaking"], v["work"]
        lv = self.level
        cx, cy = w / 2, h / 2
        r = h * (0.25 if self.compact else 0.30)
        sa = 0.45 if self.compact else max(0.45, r / 50)  # wave scale
        half = w / 2 - 2
        b = self._breath(now) * idle
        live = lis + thk + spk + err

        acc = _mixc(_mixc(self._voice, self._faint, asleep), self._danger, err)

        def ca(a: float, c: QColor = acc) -> QColor:
            q = QColor(c)
            q.setAlphaF(max(0.0, min(1.0, a)))
            return q

        if self._flinch_t0:                       # error: flinch once
            ft = now - self._flinch_t0
            if ft < 0.45:
                p.translate(4 * sa * math.sin(ft * 48) * (1 - ft / 0.45) * err, 0)
            else:
                self._flinch_t0 = 0.0
        grow = 1.0
        if self._ignite_t0:                       # boot ritual
            f = (now - self._ignite_t0) / self._ignite_dur
            if f >= 1.0:
                self._ignite_t0 = 0.0
            else:
                grow = 1 - (1 - f) ** 3
                p.setOpacity(0.25 + 0.75 * grow)

        # halo: light pooled behind the ring, wider than tall, zero at edges
        halo = (0.10 * idle + 0.22 * lis + 0.16 * thk + 0.26 * spk + 0.14 * err
                + 0.05 * b + 0.08 * spk * lv)
        halo = max(halo, 0.14 * wrk * (1 - asleep))
        if halo > 0.003:
            ry = cy * 0.98
            rx = min(half, r * (2.6 if self.compact else 3.4))
            g = QRadialGradient(QPointF(0, 0), ry)
            g.setColorAt(0.0, ca(halo))
            g.setColorAt(1.0, ca(0.0))
            p.save()
            p.translate(cx, cy)
            p.scale(max(1.0, rx / ry), 1.0)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(QPointF(0, 0), ry, ry)
            p.restore()

        # the string, both sides of the ring
        x0 = r + (2 if self.compact else 4)
        reach = max(0.0, (half - x0) * grow)
        str_a = (0.25 * asleep + 0.55 * idle + 0.85 * (lis + thk + spk)
                 + 0.6 * err + 0.1 * b)
        step = 2.0 if self.compact else 3.0
        beads = None
        p.setBrush(Qt.NoBrush)
        if reach > 2:
            d = np.arange(0.0, reach + step, step)
            for side in (-1, 1):
                X = cx + side * (x0 + d)
                g = QLinearGradient(cx + side * x0, cy, cx + side * (x0 + reach), cy)
                g.setColorAt(0.0, ca(str_a))
                g.setColorAt(1.0, ca(0.0))
                p.setPen(QPen(g, (1.3 if self.compact else 1.6) + 0.4 * spk))
                if live > 0.0:
                    Y = cy + self._offsets(d, X, side, half, sa)
                    p.drawPolyline(QPolygonF(
                        [QPointF(a, c) for a, c in zip(X.tolist(), Y.tolist())]))
                    if side > 0:
                        beads = Y
                else:
                    p.drawLine(QPointF(float(X[0]), cy), QPointF(float(X[-1]), cy))

        # Claude at work: beads run right along the string (toward his column)
        if wrk > 0.0 and reach > 2:
            p.setPen(Qt.NoPen)
            br = 1.3 if self.compact else 1.9
            for i in range(3):
                u = (self._t * 0.45 + i / 3) % 1.0
                dist = u * reach
                y = cy
                if beads is not None:
                    y = float(beads[min(len(beads) - 1, int(dist / step))])
                p.setBrush(ca(wrk * 0.9 * math.sin(math.pi * u), self._workc))
                p.drawEllipse(QPointF(cx + x0 + dist, y), br, br)

        # glass core
        core = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.35), r * 1.2)
        core.setColorAt(0.0, ca(0.20 - 0.14 * asleep + 0.06 * b))
        core.setColorAt(1.0, ca(0.02))
        p.setPen(Qt.NoPen)
        p.setBrush(core)
        p.drawEllipse(QPointF(cx, cy), r, r)
        # listening: his level fills the core
        if lis > 0.0:
            rf = r * (0.35 + 0.55 * lv)
            g = QRadialGradient(QPointF(cx, cy), max(1.0, rf))
            g.setColorAt(0.0, ca(0.6 * lis, self._voice))
            g.setColorAt(1.0, ca(0.0, self._voice))
            p.setBrush(g)
            p.drawEllipse(QPointF(cx, cy), rf, rf)
        p.setBrush(Qt.NoBrush)
        # speaking: rings pulse outward with the voice
        if spk > 0.0:
            reach_r = (min(cy - 1, r * 1.55) - r) if self.compact else 18 * sa
            for i in range(2):
                u = (self._t * 0.8 + i * 0.5) % 1.0
                rr = r + 3 + reach_r * u
                p.setPen(QPen(ca(spk * (1 - u) * (0.25 + 0.5 * lv)), 1.2))
                p.drawEllipse(QPointF(cx, cy), rr, rr)

        # the ring itself
        ring_a = (0.35 * asleep + 0.7 * idle + 1.0 * (lis + thk + spk + err)
                  + 0.2 * b)
        pen = QPen(ca(ring_a), 1.4 if self.compact else (2.0 if r > 30 else 1.7))
        if err > 0.5:
            pen.setDashPattern([3, 2] if self.compact else [6, 3])
        p.setPen(pen)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        if grow < 1.0:
            p.drawArc(rect, 90 * 16, -int(360 * 16 * min(1.0, grow * 1.6)))
        else:
            p.drawEllipse(rect)
        # thinking: a bright arc runs the ring
        if thk > 0.0:
            a0 = (self._t * 300) % 360
            wpx = 2.0 if self.compact else 2.6
            for i in range(14):
                pc = QPen(ca(0.9 * thk * (1 - i / 14), self._paper), wpx)
                pc.setCapStyle(Qt.RoundCap)
                p.setPen(pc)
                p.drawArc(rect, int((a0 - i * 6) * 16), int(-6 * 16))
        # Claude's light: a warm satellite with a trail, only while it works
        if wrk > 0.0:
            orb = r + max(4.0 if self.compact else 8.0, r * 0.28)
            sat = (-self._t * 80) % 360
            orect = QRectF(cx - orb, cy - orb, 2 * orb, 2 * orb)
            for i in range(18):
                pt = QPen(ca(0.55 * wrk * (1 - i / 18), self._workc), 1.4)
                pt.setCapStyle(Qt.RoundCap)
                p.setPen(pt)
                p.drawArc(orect, int((sat + i * 5) * 16), int(5 * 16))
            ang = math.radians(sat)
            sx, sy = cx + orb * math.cos(ang), cy - orb * math.sin(ang)
            gr = max(4.0, 14 * r / 50)
            hg = QRadialGradient(QPointF(sx, sy), gr)
            hg.setColorAt(0.0, ca(0.55 * wrk, self._workc))
            hg.setColorAt(1.0, ca(0.0, self._workc))
            p.setPen(Qt.NoPen)
            p.setBrush(hg)
            p.drawEllipse(QPointF(sx, sy), gr, gr)
            p.setBrush(ca(wrk, self._workc))
            dr = max(1.8, 3.6 * r / 50)
            p.drawEllipse(QPointF(sx, sy), dr, dr)


class VoiceOrb(QWidget):
    """The collapsed dot's sphere (his order 2026-09-30: "a sphere in the
    middle that moves like in the movies when AI talks"). Since v8 phase b
    the window itself shows Presence; the dot keeps this until phase f.

    A globe of light points on a rotating sphere, projected with a little
    perspective so the near side is bright and large and the far side
    fades. Its surface is displaced by travelling waves, and the waves are
    driven by the REAL audio: GOAT's speaker envelope while it talks, his
    mic level while it listens — so it moves with the voice, not a loop.

    idle      — slow spin, a barely-there breath
    listening — ripples that follow his voice
    thinking  — fast, fine shimmer; spin speeds up
    speaking  — big, smooth swells with GOAT's own voice level
    """

    N = 760                       # points on the sphere
    SPIN = {"idle": 0.18, "booting": 0.18, "listening": 0.35,
            "thinking": 1.1, "working": 0.8, "speaking": 0.55}

    def __init__(self, n: int = 0):
        super().__init__()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        if n:
            self.N = n           # the bubble's small sphere needs fewer points
        self.level = 0.0
        self.state = "idle"
        self._t = 0.0
        self._rot = 0.0
        self._energy = 0.0         # smoothed "how alive" 0..1 for glow
        self._acc = QColor("#3b8bff")
        self._acc2 = QColor("#6fb3ff")
        self._paper = QColor("#e9eefa")
        # Fibonacci sphere: evenly spread points, no poles bunching.
        golden = math.pi * (3 - math.sqrt(5))
        pts = []
        for i in range(self.N):
            y = 1 - 2 * (i + 0.5) / self.N
            r = math.sqrt(1 - y * y)
            a = golden * i
            pts.append((math.cos(a) * r, y, math.sin(a) * r))
        self._pts = pts
        rng = random.Random(3)
        # Wave directions for the surface displacement.
        self._dirs = []
        for _ in range(4):
            v = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1))
            n = math.sqrt(sum(c * c for c in v)) or 1.0
            self._dirs.append(tuple(c / n for c in v))
        # The waves live in the sphere's own frame, so each point's
        # projection on each wave direction never changes: compute once.
        # Per frame that leaves only the sines — the paint must stay cheap,
        # it runs on the UI thread thirty times a second.
        d0, d1, d2, d3 = self._dirs
        self._proj = [(x * d0[0] + y * d0[1] + z * d0[2],
                       1.7 * (x * d1[0] + y * d1[1] + z * d1[2]),
                       2.9 * (x * d2[0] + y * d2[1] + z * d2[2]),
                       0.7 * (x * d3[0] + y * d3[1] + z * d3[2]))
                      for (x, y, z) in pts]
        self._P = np.array(pts, dtype=float)
        self._A = np.array(self._proj, dtype=float)
        self._cols: dict = {}      # (near bucket, lift bucket) -> QColor
        self._glow: dict = {}      # size/energy step -> halo+core pixmap
        self._frame = 0

    def set_theme(self, t: dict):
        self._acc = QColor(t["accent"])
        self._acc2 = QColor(t.get("accent2", t["accent"]))
        self._paper = QColor(t["paper"])
        self._cols = {}
        self.update()

    def _col(self, nb: int, lb: int) -> QColor:
        c = self._cols.get((nb, lb))
        if c is None:
            near, lift = nb / 15, lb / 4
            c = _mixc(self._acc, self._acc2, near * 0.8, 0)
            c = _mixc(c, self._paper, min(1.0, 0.15 + lift * 0.5), 0)
            c.setAlpha(int(25 + 205 * near ** 1.4))
            self._cols[(nb, lb)] = c
        return c

    def tick(self, level: float, state: str):
        lvl = max(0.0, min(1.0, level))
        # Fast attack, slower release — reads like a VU needle, not jitter.
        k = 0.45 if lvl > self.level else 0.12
        self.level += (lvl - self.level) * k
        self.state = state or "idle"
        spin = self.SPIN.get(self.state, 0.3)
        self._rot += 0.033 * spin * (1 + self.level * 1.5)
        self._t += 0.033
        want = {"speaking": 0.55 + 0.45 * self.level,
                "listening": 0.3 + 0.7 * self.level,
                "thinking": 0.55, "working": 0.45}.get(self.state, 0.15)
        self._energy += (want - self._energy) * 0.08
        # Idle is a slow drift: 10 frames a second shows it just as well as
        # 30, at a third of the CPU. Voice and work states keep every frame.
        self._frame += 1
        calm = self.state in ("idle", "booting") and self.level < 0.05
        if self.isVisible() and (not calm or self._frame % 3 == 0):
            self.update()

    def _amp(self) -> tuple:
        """(amplitude, wave frequency, wave speed) for the current state."""
        s, lv = self.state, self.level
        if s == "speaking":
            return 0.05 + 0.20 * lv, 3.0, 2.4
        if s == "listening":
            return 0.02 + 0.16 * lv, 5.0, 3.2
        if s in ("thinking", "working"):
            return 0.035, 9.0, 7.0
        return 0.012 + 0.008 * math.sin(self._t * 0.9), 2.5, 0.8

    def paintEvent(self, _ev):
        p = QPainter(self)
        self.paint_orb(p, QRectF(self.rect()))

    def _paint_glow(self, w: float, h: float, R: float, hr: float, e: float,
                    dpr: float) -> QPixmap:
        """Halo (light spilling from the orb) + core (a soft inner light,
        hotter when it talks), for one energy step."""
        pm = QPixmap(max(1, int(math.ceil(w * dpr))), max(1, int(math.ceil(h * dpr))))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        acc, acc2, paper = self._acc, self._acc2, self._paper
        cx, cy = w / 2, h / 2
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        halo = QRadialGradient(QPointF(cx, cy), hr)
        halo.setColorAt(0.0, _mixc(acc, acc, 0, int(70 + 90 * e)))
        halo.setColorAt(0.35 + 0.15 * e, _mixc(acc, acc, 0, int(22 + 40 * e)))
        halo.setColorAt(1.0, _mixc(acc, acc, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QPointF(cx, cy), hr, hr)
        core = QRadialGradient(QPointF(cx, cy - R * 0.1), R * 1.05)
        core.setColorAt(0.0, _mixc(acc2, paper, 0.5, int(90 + 110 * e)))
        core.setColorAt(0.5, _mixc(acc, acc2, 0.5, int(40 + 60 * e)))
        core.setColorAt(1.0, _mixc(acc, acc, 0, 0))
        p.setBrush(core)
        p.drawEllipse(QPointF(cx, cy), R * 1.05, R * 1.05)
        p.end()
        return pm

    def paint_orb(self, p: QPainter, box: QRectF, dot_k: float = 1.0):
        """Paint the orb into any box (the collapsed bubble)."""
        w, h = box.width(), box.height()
        if w < 10 or h < 10:
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        cx, cy = box.center().x(), box.center().y()
        R = min(w, h) * 0.34
        e = self._energy
        acc, acc2, paper = self._acc, self._acc2, self._paper

        # Halo + core: two big radial gradients that only change with the
        # energy, so they are painted once per energy step into a pixmap
        # and blitted — filling them live was ~2ms of every frame.
        dpr = p.device().devicePixelRatioF() if p.device() else 1.0
        eb = round(e * 24)
        # The halo must reach zero INSIDE the Chat widget, or its edge shows
        # as a box; in the bubble it overshoots and the disc clips it.
        hr = min(cx, cy) * 0.98
        key = (int(w), int(h), int(hr), eb, dpr, acc.rgba(), acc2.rgba(),
               paper.rgba())
        glow = self._glow.get(key)
        if glow is None:
            if len(self._glow) > 48:
                self._glow.clear()
            glow = self._paint_glow(w, h, R, hr, eb / 24, dpr)
            self._glow[key] = glow
        p.drawPixmap(box.topLeft(), glow)

        # Orbit rings: two thin tilted ellipses turning around the globe.
        p.setBrush(Qt.NoBrush)
        for k, (tilt, speed, alpha) in enumerate(((0.28, 0.6, 90), (0.16, -0.9, 60))):
            p.save()
            p.translate(cx, cy)
            p.rotate(math.degrees(self._rot * speed) * 0.25 + k * 70)
            rr = R * (1.32 + 0.12 * k + 0.05 * e)
            pen = QPen(_mixc(acc2, paper, 0.2, int(alpha * (0.6 + 0.6 * e))), 1.2)
            p.setPen(pen)
            p.drawEllipse(QPointF(0, 0), rr, rr * tilt)
            # A bright bead travelling along the ring.
            a = self._t * (1.4 + k) * (1 if speed > 0 else -1)
            p.setPen(Qt.NoPen)
            p.setBrush(_mixc(paper, acc2, 0.3, int(150 + 100 * e)))
            p.drawEllipse(QPointF(math.cos(a) * rr, math.sin(a) * rr * tilt), 2.4, 2.4)
            p.setBrush(Qt.NoBrush)
            p.restore()

        # The globe itself.
        amp, freq, speed = self._amp()
        t = self._t * speed
        ry = self._rot
        tilt = 0.35 + 0.08 * math.sin(self._t * 0.3)
        cyr, syr = math.cos(ry), math.sin(ry)
        cxr, sxr = math.cos(tilt), math.sin(tilt)
        k = 1.3 * R
        # The whole globe in one pass of array math (the per-point Python
        # loop was ~2ms of every frame), then dots batched by (depth, lift)
        # bucket — one drawPoints call per bucket with a round pen instead of
        # one drawEllipse per dot. Before this the Chat page held 36% of a
        # core while GOAT sat idle (measured 2026-10-01). Buckets are drawn
        # far to near, so near dots still land on top.
        P, A = self._P, self._A
        x, y, z = P[:, 0], P[:, 1], P[:, 2]
        disp = (np.sin(freq * A[:, 0] + t) + 0.6 * np.sin(freq * A[:, 1] - t * 1.3)
                + 0.35 * np.sin(freq * A[:, 2] + t * 1.9)
                + 0.25 * np.sin(freq * A[:, 3] - t * 0.6))
        # Rotate about Y, then tilt about X.
        x1 = x * cyr + z * syr
        z1 = -x * syr + z * cyr
        y2 = y * cxr - z1 * sxr
        z2 = y * sxr + z1 * cxr
        s = (1.0 + amp * disp / 2.2) * k / (1.6 - z2 * 0.45)
        px = (cx + x1 * s).tolist()
        py = (cy + y2 * s).tolist()
        nb = np.clip(((z2 + 1) * 7.5).astype(int), 0, 15)   # 0 far .. 15 near
        lb = np.clip((np.maximum(disp, 0) * (e * 4 / 2.2)).astype(int), 0, 4)
        keys = (nb * 5 + lb).tolist()
        groups: dict = {}
        for key, X, Y in zip(keys, px, py):
            g = groups.get(key)
            if g is None:
                groups[key] = g = []
            g.append(QPointF(X, Y))
        col = self._col
        p.setBrush(Qt.NoBrush)
        for key in sorted(groups):
            nb_, lb_ = divmod(key, 5)
            near = (nb_ + 0.5) / 15
            size = (0.6 + 1.5 * near ** 1.5 + 1.1 * (lb_ + 0.4) / 4) * dot_k
            pen = QPen(col(nb_, lb_), size * 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawPoints(QPolygonF(groups[key]))


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


class BottomFollow:
    """Keep a scroll area glued to its bottom only while he is there.

    The old way — setValue(maximum()) on a 20-30ms timer after every streamed
    word — raced the layout: the jump landed before the new line's height was
    known, then again after, and it ignored him entirely when he scrolled up
    to read. On the left lane that read as the bar bouncing up and down
    (his report 2026-09-26). Now the scrollbar's own rangeChanged does the
    snapping, once, after layout; the moment he scrolls up the page parks."""

    SLACK = 6  # px from the bottom that still counts as "at the bottom"

    def __init__(self, scroll: QScrollArea):
        self.sb = scroll.verticalScrollBar()
        self.follow = True
        self.sb.valueChanged.connect(self._on_value)
        self.sb.rangeChanged.connect(self._on_range)

    def _on_value(self, v: int):
        self.follow = v >= self.sb.maximum() - self.SLACK

    def _on_range(self, _lo: int, hi: int):
        if self.follow and not self.sb.isSliderDown():
            self.sb.setValue(hi)

    def snap(self):
        if self.follow and not self.sb.isSliderDown():
            self.sb.setValue(self.sb.maximum())


class PageLabel(QLabel):
    """QLabel whose minimum height is its real wrapped-text height.

    Word-wrapped QLabels in a QVBoxLayout report a near-zero minimum
    (heightForWidth is ignored in the layout's minimum-size pass), so once
    the page outgrows the viewport the scroll area COMPRESSES old lines to
    slivers instead of scrolling — history looked deleted."""

    def resizeEvent(self, ev):
        # The minimum above depends on the width; a label whose text was set
        # before its first layout (restored tail, tool lines) kept the height
        # it would need at Qt's default 100px width — a tall empty gap under
        # the conversation. Ask again whenever the width really changes.
        super().resizeEvent(ev)
        if self.wordWrap() and ev.oldSize().width() != ev.size().width():
            self.updateGeometry()

    def sizeHint(self):
        # Qt's hint for a wrapped label is a guess at some other width; the
        # scroll host sums these, and the difference turned into empty
        # space under the last line. Report the height at the real width.
        base = super().sizeHint()
        if self.wordWrap() and self.width() > 1:
            return QSize(base.width(), self.heightForWidth(self.width()))
        return base

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


class FlowLayout(QLayout):
    """Left-aligned row of buttons that WRAPS instead of clipping.

    The drawer is 24% of the window, and a row like the thinking ladder
    (low…max) already fills it at 100%; at 150% interface scale a plain
    QHBoxLayout just pushed the last options past the edge with no scrollbar
    to reach them. Wrapping keeps every switch reachable at every scale.
    """

    def __init__(self, parent=None, spacing: int = 10, even_rows: bool = False):
        super().__init__(parent)
        self._items: list = []
        self._space = spacing
        # Cards in one row take the row's height, so a grid reads as a grid
        # (Tools page, 2026-10-01: a tall card beside a short one).
        self._even = even_rows
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout plumbing
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._lay(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._lay(rect, test=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _lay(self, rect: QRect, test: bool) -> int:
        # Rows first, then place. A wrapped card's sizeHint height is Qt's
        # guess at some OTHER width; heightForWidth at the card's real width
        # is the height it actually needs.
        rows, row, x = [], [], rect.x()
        for it in self._items:
            if it.isEmpty():
                continue      # a hidden widget takes no room and no gap
            hint = it.sizeHint()
            w = hint.width()
            h = it.heightForWidth(w) if it.hasHeightForWidth() else hint.height()
            h = max(h, it.minimumSize().height())
            # rect.right() is width-1: cards sized to fill the row EXACTLY
            # (CardGrid) always wrapped one early against it.
            if x + w > rect.x() + rect.width() and row:   # doesn't fit — wrap
                rows.append(row)
                row, x = [], rect.x()
            row.append((it, w, h))
            x += w + self._space
        if row:
            rows.append(row)
        y = rect.y()
        for r in rows:
            line_h = max(h for _, _, h in r)
            if not test:
                x = rect.x()
                for it, w, h in r:
                    it.setGeometry(QRect(x, y, w, line_h if self._even else h))
                    x += w + self._space
            y += line_h + self._space
        return (y - self._space - rect.y()) if rows else 0


def glyph_icon(key: str, color: str, px: int = 20) -> QIcon:
    """An icon-font glyph as a QIcon, for buttons that also carry body text
    (a QPushButton has one font; the nav needs two)."""
    ratio = 2
    pm = QPixmap(px * ratio, px * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    f = QFont()
    f.setFamilies(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
    f.setPixelSize(max(8, int(px * 0.86)))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRect(0, 0, px, px), Qt.AlignCenter, IC.get(key, key))
    p.end()
    return QIcon(pm)


def icon_label(key: str, obj: str = "ico") -> QLabel:
    lbl = QLabel(IC.get(key, key))
    lbl.setObjectName(obj)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lbl


def repolish(w: QWidget, *, deep: bool = False):
    w.style().unpolish(w)
    w.style().polish(w)
    if deep:
        for c in w.findChildren(QWidget):
            c.style().unpolish(c)
            c.style().polish(c)


# v8 labels: small mono caps. QSS has no text-transform, but a stylesheet
# font keeps the widget font's capitalization, so it is set here once.
CAPS_LABELS = ("sectionTitle", "paneltitle", "stateword", "speaker")


def capsify(lbl: QLabel) -> QLabel:
    f = lbl.font()
    f.setCapitalization(QFont.AllUppercase)
    lbl.setFont(f)
    return lbl


def mklabel(text: str, obj: str, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(obj)
    if obj in CAPS_LABELS:
        capsify(lbl)
    lbl.setWordWrap(wrap)
    lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lbl


class ElideLabel(QLabel):
    """One line that ends in … instead of pushing the layout wider. Recent
    exchanges are whole spoken paragraphs; the row must not care."""

    def __init__(self, text: str = "", obj: str = ""):
        super().__init__()
        self._full = ""
        if obj:
            self.setObjectName(obj)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_full(text)

    def set_full(self, text: str):
        self._full = " ".join((text or "").split())
        self._elide()

    def full(self) -> str:
        return self._full

    def _elide(self):
        w = max(10, self.width())
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, w))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._elide()

    def minimumSizeHint(self):
        return QSize(10, super().minimumSizeHint().height())


class Clickable(QFrame):
    """A card you can press. QFrame, not QPushButton: a push button sizes to
    its own text and ignores the layout inside it; a frame sizes to its
    content, takes QSS :hover, and still reads as one target."""

    clicked = Signal()

    def __init__(self, obj: str):
        super().__init__()
        self.setObjectName(obj)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self._down = False

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._down = True
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if (ev.button() == Qt.LeftButton and self._down
                and self.rect().contains(ev.position().toPoint())):
            self.clicked.emit()
        self._down = False
        ev.accept()

    def set_on(self, on: bool):
        self.setProperty("on", "true" if on else "false")
        repolish(self)


class ActionCard(Clickable):
    """Quick action on the home page: icon tile, chevron, title, one line."""

    def __init__(self, key: str, title: str, sub: str):
        super().__init__("actionCard")
        lay = QVBoxLayout(self)
        self._lay = lay
        top = QHBoxLayout()
        self.tile = icon_label(key, "icoTile")
        top.addWidget(self.tile)
        top.addStretch(1)
        top.addWidget(icon_label("chev", "chev"), 0, Qt.AlignTop)
        lay.addLayout(top)
        lay.addStretch(1)
        lay.addWidget(mklabel(title, "cardTitle"))
        self.sub = mklabel(sub, "cardSub", wrap=True)
        lay.addWidget(self.sub)
        self.set_scale(1.0)

    def set_scale(self, k: float):
        m = round(14 * k)
        self._lay.setContentsMargins(m, m, m, m)
        self._lay.setSpacing(round(3 * k))
        d = round(38 * k)
        self.tile.setFixedSize(d, d)
        self.setMinimumHeight(round(136 * k))


class DeskRow(Clickable):
    """One line of the Home desk (v8): a title, a quieter line under it, a
    hairline between rows. No icon, no card: the desk is quiet on purpose."""

    def __init__(self, title: str, sub: str):
        super().__init__("row")
        lay = self._lay = QVBoxLayout(self)
        self.title = ElideLabel(title, "rowTitle")
        self.sub = ElideLabel(sub, "rowSub")
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        self.setToolTip(f"{title}\n{sub}" if sub else title)
        self.set_scale(1.0)

    def set_scale(self, k: float):
        self._lay.setContentsMargins(round(8 * k), round(9 * k), round(8 * k),
                                     round(9 * k))
        self._lay.setSpacing(round(2 * k))


class CardGrid(QWidget):
    """Quick-action cards that share a row while they fit and wrap into
    even rows when they don't — at 150% zoom five fixed cards pushed the
    whole home page wider than the window and clipped the greeting."""

    def __init__(self, spacing: int = 14, min_w: int = 150):
        super().__init__()
        self.cards: list = []
        self._base = (spacing, min_w)
        self.min_w = min_w
        self.flow = FlowLayout(self, spacing=spacing, even_rows=True)

    def add(self, card: QWidget):
        self.cards.append(card)
        self.flow.addWidget(card)

    def set_scale(self, k: float):
        self.flow._space = round(self._base[0] * k)
        self.min_w = round(self._base[1] * k)
        self._fit()

    def _fit(self):
        n = len(self.cards)
        if not n:
            return
        gap = self.flow._space
        avail = max(1, self.width())
        per = max(1, min(n, (avail + gap) // (self.min_w + gap)))
        if per < n:          # balance the rows: 5 → 3+2, not 4+1
            rows = -(-n // per)
            per = -(-n // rows)
        w = max(60, (avail - gap * (per - 1)) // per)
        for c in self.cards:
            c.setFixedWidth(w)
        self.flow.invalidate()
        self.updateGeometry()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if ev.oldSize().width() != ev.size().width():
            self._fit()

    def minimumSizeHint(self):
        return QSize(self.min_w, super().minimumSizeHint().height())


class ListRow(Clickable):
    """One line of a list card: tile · title / sub · time · ⋯"""

    def __init__(self, key: str, title: str, sub: str = "", when: str = "",
                 more: bool = False):
        super().__init__("row")
        lay = QHBoxLayout(self)
        self._lay = lay
        self.tile = icon_label(key, "icoTile")
        lay.addWidget(self.tile)
        mid = QVBoxLayout()
        mid.setSpacing(1)
        self.title = ElideLabel(title, "rowTitle")
        mid.addWidget(self.title)
        self.sub = ElideLabel(sub, "rowSub")
        mid.addWidget(self.sub)
        if not sub:
            self.sub.hide()
        lay.addLayout(mid, 1)
        self.when = mklabel(when, "rowTime")
        lay.addWidget(self.when)
        if not when:
            self.when.hide()
        self.more_btn = None
        if more:
            self.more_btn = QPushButton(IC["more"])
            self.more_btn.setObjectName("more")
            self.more_btn.setCursor(Qt.PointingHandCursor)
            lay.addWidget(self.more_btn)
        self.set_scale(1.0)

    def set_scale(self, k: float):
        self._lay.setContentsMargins(round(12 * k), round(8 * k),
                                     round(8 * k), round(8 * k))
        self._lay.setSpacing(round(12 * k))
        d = round(34 * k)
        self.tile.setFixedSize(d, d)


class ChatBubble(QLabel):
    """His side of the conversation: a rounded bubble on the right, as wide
    as its words and never wider than ~3/4 of the column. The width comes
    from the text itself — Qt's own guess for a word-wrapped label is a
    squat, too-narrow box."""

    PAD = (14, 9, 14, 10)

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # A right-aligned item in a column gets its height worked out at the
        # FULL column width (one line) and is then squeezed to its own width
        # — the second line was clipped. The bubble picks its own width, so
        # it reports a plain size and opts out of height-for-width.
        sp = self.sizePolicy()
        sp.setHeightForWidth(False)
        self.setSizePolicy(sp)
        self._k = 1.0
        self.set_scale(1.0)

    def hasHeightForWidth(self) -> bool:
        return False

    def set_scale(self, k: float):
        self._k = k
        self.setContentsMargins(*(round(v * k) for v in self.PAD))
        self.updateGeometry()

    def _maxw(self) -> int:
        pw = self.parentWidget().width() if self.parentWidget() else 700
        return max(160, int(pw * 0.72))

    def sizeHint(self):
        m = self.contentsMargins()
        text_w = self.fontMetrics().horizontalAdvance(self.text() or " ")
        # +8: the QSS border (2px) and a hair of slack, or a one-liner wraps
        # its last word onto a second line.
        w = min(text_w + m.left() + m.right() + 8, self._maxw())
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self):
        h = self.sizeHint()
        return QSize(min(h.width(), 120), h.height())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if ev.oldSize().width() != ev.size().width():
            self.updateGeometry()


class SettingsPage(QWidget):
    """Every switch GOAT and the UI expose, as a page of cards (v7). Was the
    slide-in drawer; the switches, their order and their handlers are the
    same, so the voice-driven setters (set_*_opt) are untouched."""

    SECTIONS = [
        ("Brain", "brain", [
            ("working brain", "work_model", WORK_OPTS, "set_work_opt"),
            ("hard brain", "hard_model", WORK_OPTS, "set_hard_opt"),
            ("thinking", "effort", EFFORT_OPTS, "set_effort_opt")]),
        ("Voice & hearing", "mic", [
            ("voice", "voice", ["on", "off"], "set_voice_opt"),
            ("voice level", "level", list(VOICE_LEVELS), "set_level_opt"),
            ("voice character", "character", VOICE_CHARACTERS, "set_character_opt"),
            ("language", "lang", list(LANGS), "set_lang_opt"),
            ("wake word", "wake", ["on", "off"], "set_wake_opt"),
            ("voice lock", "voice_lock", ["on", "off", "learn"], "set_voice_lock_opt"),
            ("microphone", "mic", ["live", "muted"], "set_mic_opt")]),
        ("Appearance", "photo", [
            ("theme", "theme", THEME_ORDER, "set_theme_opt"),
            ("interface size", "scale", list(UI_SCALES), "set_scale_opt"),
            ("text size", "text", list(TEXT_SIZES), "set_text_opt"),
            ("window", "window", ["normal", "on top"], "set_ontop_opt")]),
    ]
    SHORTCUTS = [("Enter", "send"), ("Ctrl+Enter", "send to the working brain"),
                 ("Ctrl+Shift+Enter", "hard brain"), ("Esc", "stop talking / back"),
                 ("Ctrl+K", "type"), ("Ctrl+F", "search"), ("Ctrl+M", "mic"),
                 ("Ctrl+B", "bubble"), ("Ctrl+N", "new chat"), ("Ctrl+E", "thinking"),
                 ("Ctrl+L", "language"), ("Ctrl+T", "theme"), ("Ctrl+O", "send a file"),
                 ("Ctrl+,", "settings"), ("F11", "fullscreen")]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._groups: dict[str, list] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 8, 12)
        lay.setSpacing(14)
        lay.addWidget(mklabel("Settings", "pageTitle"))
        lay.addWidget(mklabel("Changes save instantly — you can also just say "
                              "them (“make the text bigger”, “speak Georgian”).",
                              "pageSub", wrap=True))
        for title, key, rows in self.SECTIONS:
            card = QFrame()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(18, 14, 18, 16)
            cl.setSpacing(8)
            head = QHBoxLayout()
            head.addWidget(icon_label(key, "icoAcc"))
            head.addWidget(mklabel(title, "sectionTitle"))
            head.addStretch(1)
            cl.addLayout(head)
            for label, gkey, options, handler in rows:
                cl.addWidget(mklabel(label, "optlabel"))
                flow = FlowLayout(spacing=6)
                btns = []
                for opt in options:
                    b = QPushButton(opt)
                    b.setObjectName("optbtn")
                    b.setCursor(Qt.PointingHandCursor)
                    b.clicked.connect(lambda _=False, o=opt, h=handler:
                                      getattr(self.win, h)(o))
                    flow.addWidget(b)
                    btns.append((opt, b))
                cl.addLayout(flow)
                self._groups[gkey] = btns
            lay.addWidget(card)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 16)
        head = QHBoxLayout()
        head.addWidget(icon_label("bolt", "icoAcc"))
        head.addWidget(mklabel("Actions", "sectionTitle"))
        head.addStretch(1)
        cl.addLayout(head)
        flow = FlowLayout(spacing=8)
        for text, cb in (("Copy last reply", self.win.copy_last_reply),
                         ("Reset colors", self.win.reset_ui_colors),
                         ("New chat", self.win.new_chat),
                         ("Restart Goat", self.win.restart_goat)):
            b = QPushButton(text)
            b.setObjectName("actbtn")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            flow.addWidget(b)
        cl.addLayout(flow)
        lay.addWidget(card)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 16)
        cl.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(icon_label("lang", "icoAcc"))
        head.addWidget(mklabel("Shortcuts", "sectionTitle"))
        head.addStretch(1)
        cl.addLayout(head)
        for keys, what in self.SHORTCUTS:
            r = QHBoxLayout()
            k = mklabel(keys, "kbd")
            r.addWidget(k)
            r.addSpacing(8)
            r.addWidget(mklabel(what, "rowSub"))
            r.addStretch(1)
            cl.addLayout(r)
        lay.addWidget(card)
        lay.addStretch(1)

    def set_theme(self, t: dict):
        pass  # all styling rides the window stylesheet

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
        vid = getattr(goat, "vid", None) if goat else None
        state["voice_lock"] = ("on" if vid is not None and vid.locked else "off")
        for key, btns in self._groups.items():
            active = str(state.get(key, ""))
            for opt, b in btns:
                b.setProperty("on", "true" if opt == active else "false")
                repolish(b)


class CtxMeter(QWidget):
    """How full the working brain's session is, against the trim line.

    GOAT has always known this number — it decides when the session gets
    compacted or rotated — but it lived only in the log. On the panel it
    answers the question he actually asks ("is it about to forget?") without
    him having to ask it. One hairline track, one accent fill, no chrome.
    """

    def __init__(self):
        super().__init__()
        self.setFixedHeight(3)
        self._frac = 0.0
        self._track = QColor("#3d3a34")
        self._fill = QColor("#c8622a")

    def set_theme(self, t: dict):
        self._track = QColor(t["faint"])
        self._track.setAlpha(90)
        self._fill = QColor(t["accent"])
        self.update()

    def set_frac(self, frac: float):
        frac = max(0.0, min(1.0, frac))
        if abs(frac - self._frac) > 0.001:
            self._frac = frac
            self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        y = self.height() - 1
        p.setPen(QPen(self._track, 1))
        p.drawLine(0, y, self.width(), y)
        if self._frac > 0:
            p.setPen(QPen(self._fill, 1))
            p.drawLine(0, y, int(self.width() * self._frac), y)


def voice_display(model: str) -> str:
    """A Gemini model id as a person reads it: "gemini-3-flash-preview" ->
    "Gemini 3 Flash", "gemini-3.1-flash-lite" -> "Gemini 3.1 Flash-Lite".
    The raw id is long enough to push the side card's pill off the edge."""
    m = re.match(r"gemini-([\d.]+)-(flash|pro)(-lite)?", model.strip().lower())
    if not m:
        return model.strip()
    return (f"Gemini {m.group(1)} {m.group(2).title()}"
            + ("-Lite" if m.group(3) else ""))


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
        self._line.setAlpha(110)
        self._cur_step = None    # the in-progress step label ("▸ …")
        self._text_label = None  # rolling narration label for this turn
        self._think_label = None  # rolling reasoning label for this block
        # Thinking depth, shown in the sub line from the first paint — the
        # engine confirms it on bind, but the saved setting is true already.
        self._effort = str(getattr(win, "cfg", {}).get("effort", "") or "")
        # Ledger clock: elapsed mm:ss in the header while a turn runs —
        # answers "how long has it been at this?" at a glance.
        self._t0 = 0.0
        self._model_name = ""
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*WORK_MARGIN)
        outer.setSpacing(6)
        # v7: this is the right rail's Activity card. Same ledger, same API.
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(icon_label("sync", "icoAcc"))
        self.header = capsify(QLabel("Activity"))
        self.header.setObjectName("paneltitle")
        head.addWidget(self.header)
        head.addStretch(1)
        self.tools_link = QPushButton("Tools  ›")
        self.tools_link.setObjectName("link")
        self.tools_link.setCursor(Qt.PointingHandCursor)
        head.addWidget(self.tools_link)
        outer.addLayout(head)
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
        # Right gutter so the log clears its own scroll rail — at 175% the
        # rail sat straight on top of the last word of every wrapped line.
        self.col.setContentsMargins(0, 8, 14, 0)
        self.col.setSpacing(5)
        self.col.addStretch(1)
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, stretch=1)
        self._pin = BottomFollow(self.scroll)

        # Hard newlines used to break this at ~20 characters, which was right
        # when the lane was a narrow strip and wrong ever since it became half
        # the window: five ragged stubs down the left of a mostly empty column.
        # Let it wrap to the lane it is actually in.
        self._idle = QLabel(
            "Nothing running. Steps, thinking and files show up here "
            "while Goat works.")
        self._idle.setObjectName("workidle")
        self._idle.setWordWrap(True)
        self._idle.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.col.insertWidget(0, self._idle)

        # Session-fill instrument, pinned to the bottom of the lane. Hidden
        # until the first turn reports a size — an empty meter says nothing.
        self.meter = CtxMeter()
        self.ctx_label = QLabel("")
        self.ctx_label.setObjectName("workctx")
        outer.addWidget(self.meter)
        outer.addWidget(self.ctx_label)
        self.meter.hide()
        self.ctx_label.hide()

    def context(self, used: int, trim: int):
        """Session fill after a turn: 'context 23k · trims at 60k'."""
        self.meter.show()
        self.ctx_label.show()
        self.meter.set_frac(used / trim if trim else 0.0)
        self.ctx_label.setText(
            f"context {_fmt_tok(used)} · trims at {_fmt_tok(trim)}")

    def set_theme(self, t: dict):
        self._bg = QColor(t["card"])
        self._bg.setAlpha(190)
        self._line = QColor(t["line"])
        self._line.setAlpha(230)
        self._radius = 14
        self.meter.set_theme(t)
        self.update()

    def _sub(self, tail: str) -> str:
        """Header sub-line: brain · thinking depth · state. The depth is on
        screen at all times because at max effort a silent minute is normal
        and he should never have to guess whether that is the setting."""
        bits = [self._model_name or "idle"]
        if self._effort:
            bits.append(self._effort)
        bits.append(tail)
        return " · ".join(b for b in bits if b)

    def set_turn_effort(self, level: str):
        """The auto brain's depth for THIS turn (talk runs low, simple tasks
        medium, deep work at his dial) — the sub line shows what is really
        running, not just the dial."""
        if level:
            self._effort = level

    def set_effort(self, level: str):
        self._effort = level
        self.sub.setText(self._sub("working · " + self._elapsed_str()
                                   if self._t0 else "idle"))

    def _on_tick(self):
        if self._t0:
            up = int(time.time() - self._t0)
            self.sub.setText(self._sub(f"working · {up // 60}:{up % 60:02d}"))

    def _elapsed_str(self) -> str:
        if not self._t0:
            return ""
        up = int(time.time() - self._t0)
        return f"{up // 60}:{up % 60:02d}"

    def set_model(self, name: str):
        self._model_name = name
        if self._cur_step is None and self.win and not self.win_busy():
            self.sub.setText(self._sub("idle"))

    def set_brains(self, voice: str, work: str):
        """Two brains (2026-10-24): this card is Claude's workbench, and the
        voice's hand-offs show in it."""
        self._voice_name = voice
        if voice:
            self.header.setText("Claude at work")
            if work:
                self._model_name = work.lower()
            if self._idle is not None:
                self._idle.setText(
                    f"Nothing running. When you give GOAT a job, {voice} hands "
                    "it to Claude — its steps, thinking and notes show "
                    "up here live.")
        else:
            self.header.setText("Activity")

    def voice(self, line: str):
        """What the voice told him about the job."""
        name = getattr(self, "_voice_name", "") or "voice"
        self._add(f"{name} → you: {line}", "workvoice")
        self._text_label = None
        self._think_label = None

    def win_busy(self) -> bool:
        return bool(self.win and self.win.goat and self.win.goat.busy)

    def _add(self, text: str, name: str) -> QLabel:
        lbl = PageLabel(text)
        lbl.setObjectName(name)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.col.insertWidget(self.col.count() - 1, lbl)
        self._trim()
        return lbl

    # The ledger grew one label per step forever; a long night of work made
    # every relayout (and so every scroll) heavier. Keep the recent tail.
    LOG_MAX = 160

    def _trim(self):
        while self.col.count() - 1 > self.LOG_MAX:
            item = self.col.takeAt(0)
            w = item.widget() if item is not None else None
            if w is None:
                break
            for attr in ("_cur_step", "_text_label", "_think_label", "_idle"):
                if getattr(self, attr, None) is w:
                    setattr(self, attr, None)
            w.setParent(None)
            w.deleteLater()

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
            self._idle.hide()
            self._idle.deleteLater()
            self._idle = None
        self._mark_cur_done()
        self._model_name = model
        self._t0 = time.time()
        self._tick.start()
        self.sub.setText(self._sub("working · 0:00"))
        self._last_step = ""
        voice = getattr(self, "_voice_name", "")
        lead = f"{voice} → Claude: " if voice else "— "
        self._add(lead + " ".join(task.split())[:200], "worktask")
        self._text_label = None
        self._think_label = None

    def step(self, desc: str):
        self._mark_cur_done()
        self._last_step = desc
        self._cur_step = self._add("▸ " + desc, "workstep")
        self._text_label = None
        self._think_label = None

    def think(self, piece: str):
        """Streamed thinking summary. One rolling label per reasoning block:
        the next step, note, or narration closes it, so the ledger reads as
        thought → action → thought instead of one endless paragraph."""
        if self._idle is not None:
            self._idle.deleteLater()
            self._idle = None
        if self._think_label is None:
            self._think_label = self._add("… ", "workthink")
        self._think_label.setText((self._think_label.text() + piece)[-700:])

    def text(self, piece: str):
        self._think_label = None
        if self._text_label is None:
            self._text_label = self._add("", "worktext")
        self._text_label.setText((self._text_label.text() + piece)[-1200:])

    def add(self, note: str):
        self._add("+ " + note, "workstep")
        self._text_label = None
        self._think_label = None

    def done(self):
        self._mark_cur_done()
        took = self._elapsed_str()
        self._tick.stop()
        self._t0 = 0.0
        self.sub.setText(self._sub("idle"))
        self._add("✓ done" + (f" · {took}" if took else ""), "workdone")
        self._text_label = None
        self._think_label = None

    def fail(self, reason: str):
        self._mark_cur_done()
        self._tick.stop()
        self._t0 = 0.0
        self.sub.setText(self._sub("idle"))
        self._add("⚠ " + reason, "workfail")
        self._text_label = None
        self._think_label = None

    def files(self, paths: list):
        for p in paths:
            if p.strip():
                self._add("file — " + os.path.basename(p.strip()), "workstep")

    def paintEvent(self, _ev):
        # Drawn as a card like its QSS neighbours (a plain QWidget subclass
        # does not paint a stylesheet background).
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = getattr(self, "_radius", 14)
        p.setPen(QPen(self._line, 1))
        p.setBrush(self._bg)
        p.drawRoundedRect(r, rad, rad)


def pin_topmost(w: QWidget):
    """Put a floating window back at the top of the z-order, without focus.

    WindowStaysOnTopHint is set once, at creation. Windows drops a window out
    of the topmost band when another topmost or full-screen app takes it,
    and Qt never puts it back — the bubble then hid behind whatever he opened
    until he minimised it (his report, 2026-09-26).
    """
    if not w.isVisible() or QGuiApplication.platformName() != "windows":
        return
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x1, 0x2, 0x10, 0x200
    ctypes.windll.user32.SetWindowPos(
        ctypes.wintypes.HWND(int(w.winId())), ctypes.wintypes.HWND(-1),  # HWND_TOPMOST
        0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)


class Bubble(QWidget):
    """GOAT collapsed to a single dot — the messenger-bubble mode.

    Minimizing a voice assistant to the taskbar hides the one thing that
    matters about it: whether it is listening, thinking, or talking. This keeps
    that visible in sixty-odd pixels — a round, always-on-top widget that sits
    in a corner, breathes with the live state, marks a reply he has not seen,
    and opens the full window again on a click (his order, 2026-09-14).

    Frameless and translucent, because a circle inside a grey square frame is
    not a bubble. Qt.Tool as well, so collapsing never leaves a second taskbar
    button standing next to the window it replaced.
    """

    clicked = Signal()
    moved = Signal()
    relocated = Signal()   # every move, drag included — the message card follows

    # How loudly the ring burns per state. Idle is nearly dark on purpose: the
    # bubble should read as "present, not demanding" until something happens.
    GLOW = {"speaking": 1.0, "thinking": 0.8, "working": 0.8,
            "listening": 0.55, "idle": 0.25, "booting": 0.25}
    BUSY = ("speaking", "thinking", "working", "listening")

    def __init__(self, theme: dict, scale: float = 1.0):
        super().__init__(None, Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("GOAT — click to open, drag to move")
        self._t = dict(theme)
        self._state = "idle"
        self._unread = False
        self._phase = 0.0
        self._press: QPoint | None = None
        self._origin: QPoint | None = None
        self._dragged = False
        self._beat_timer = QTimer(self)
        self._beat_timer.timeout.connect(self._beat)
        # v7 (his ask 2026-09-30: "same sphere as the bubble"): the dot is
        # the Chat page's voice orb in miniature, moving with the same live
        # level. Never shown as a widget — it only paints into the disc.
        self._orb = VoiceOrb(n=220)
        self._orb.set_theme(_complete_theme(self._t))
        self._level = 0.0
        self.set_scale(scale)

    # ---- appearance -------------------------------------------------------

    def set_scale(self, scale: float):
        # One dial scales the whole app; the bubble rides it like everything
        # else, with a floor so it can never shrink to an unclickable speck.
        self._d = max(46, int(round(64 * float(scale or 1.0))))
        self.setFixedSize(self._d, self._d)
        self.update()

    def set_theme(self, t: dict):
        self._t = dict(t)
        self._orb.set_theme(_complete_theme(self._t))
        self.update()

    def set_level(self, level: float):
        """Live audio level (his mic / GOAT's voice) — the sphere's swell."""
        self._level = level

    def set_state(self, state: str):
        if state == self._state:
            return
        self._state = state or "idle"
        self._sync_beat()
        self.update()

    def set_unread(self, on: bool):
        if bool(on) == self._unread:
            return
        self._unread = bool(on)
        self._sync_beat()
        self.update()

    def _sync_beat(self):
        """Animate only when there is something to animate.

        A timer ticking behind a dot nobody is looking at is pure battery
        burn, and this machine already has a worn battery.
        """
        want = self.isVisible() and (self._state in self.BUSY or self._unread)
        if want and not self._beat_timer.isActive():
            self._beat_timer.start(50)
        elif not want and self._beat_timer.isActive():
            self._beat_timer.stop()
            self._phase = 0.0
            self.update()

    def _beat(self):
        self._phase = (self._phase + 0.055) % 1.0
        # 50ms beat, orb advances at its 33ms step x1.5 to keep real speed.
        self._orb.tick(self._level, self._state)
        self._orb._t += 0.0165
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        t = self._t
        accent = QColor(t.get("accent", "#ffb35e"))
        pad = max(4, self._d // 10)
        disc = QRect(pad, pad, self._d - 2 * pad, self._d - 2 * pad)

        glow = self.GLOW.get(self._state, 0.25)
        if self._state in self.BUSY:
            # A slow breath, not a blink — the same register as the string.
            glow *= 0.75 + 0.25 * math.sin(self._phase * 2 * math.pi)

        # Halo: a couple of soft strokes standing in for a shadow, so the
        # bubble keeps an edge on a light desktop as well as a dark one.
        for i, step in enumerate((pad, pad // 2 + 1)):
            ring = QColor(accent)
            ring.setAlpha(int(26 * glow) if i == 0 else int(46 * glow))
            p.setPen(QPen(ring, max(1, self._d // 26)))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(disc.adjusted(-step, -step, step, step))

        g = QLinearGradient(0, disc.top(), 0, disc.bottom())
        g.setColorAt(0.0, QColor(t.get("bg_top", "#1a1713")))
        g.setColorAt(1.0, QColor(t.get("bg_bot", "#14110e")))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(disc)

        # The sphere, clipped to the disc — the same orb as the Chat page.
        p.save()
        clip = QPainterPath()
        clip.addEllipse(QRectF(disc))
        p.setClipPath(clip)
        box = QRectF(disc).adjusted(-disc.width() * 0.03, -disc.height() * 0.03,
                                    disc.width() * 0.03, disc.height() * 0.03)
        self._orb.paint_orb(p, box, dot_k=max(0.5, self._d / 130))
        p.restore()

        rim = QColor(accent)
        rim.setAlpha(int(90 + 165 * glow))
        p.setPen(QPen(rim, max(2, self._d // 22)))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(disc)

        if self._unread:
            # One dot, the accent, ringed in the page colour so it reads
            # against the rim it overlaps.
            r = max(6, self._d // 6)
            spot = QRect(disc.right() - r, disc.top(), r, r)
            # A hairline keeper, not a border: on the light theme a 2px ring
            # in the page colour swallowed the accent and the dot read white.
            p.setPen(QPen(QColor(t.get("bg_bot", "#14110e")),
                          max(1, self._d // 40)))
            p.setBrush(accent)
            p.drawEllipse(spot)

    # ---- drag to move, click to open --------------------------------------

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._origin = ev.globalPosition().toPoint()
            self._dragged = False
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._press is None or not (ev.buttons() & Qt.LeftButton):
            return
        here = ev.globalPosition().toPoint()
        # A click is never perfectly still. Anything under a few pixels is
        # still a click, or the bubble would be impossible to press.
        if (here - self._origin).manhattanLength() > 5:
            self._dragged = True
        self.move(here - self._press)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton or self._press is None:
            return
        self._press = None
        if self._dragged:
            self.clamp_to_screen()
            self.moved.emit()
        else:
            self.clicked.emit()
        ev.accept()

    # ---- placement --------------------------------------------------------

    def clamp_to_screen(self):
        """Keep the bubble reachable — never under the taskbar or off an edge."""
        scr = self.screen() or QApplication.primaryScreen()
        if not scr:
            return
        area = scr.availableGeometry()
        x = min(max(self.x(), area.left()), area.right() - self.width() + 1)
        y = min(max(self.y(), area.top()), area.bottom() - self.height() + 1)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def place(self, pos):
        """Put the bubble at a remembered point, or in the default corner.

        The remembered point is clamped, not trusted: a position saved on a
        monitor that is no longer plugged in would otherwise strand it.
        """
        if (isinstance(pos, (list, tuple)) and len(pos) == 2
                and all(isinstance(n, (int, float)) for n in pos)):
            self.move(int(pos[0]), int(pos[1]))
        else:
            scr = self.screen() or QApplication.primaryScreen()
            area = scr.availableGeometry()
            inset = max(18, self._d // 3)
            self.move(area.right() - self.width() - inset,
                      area.bottom() - self.height() - inset)
        self.clamp_to_screen()

    def moveEvent(self, ev):
        super().moveEvent(ev)
        self.relocated.emit()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._sync_beat()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._beat_timer.stop()


class MessagePop(QWidget):
    """What GOAT is saying, popped out beside the collapsed dot — the way a
    Messenger chat head shows the incoming message (his order, 2026-09-25).

    The dot alone only says "a reply arrived"; this says what it was, without
    opening the window. It follows the voice word for word (fed from
    update_spoken), fades out a few seconds after the last word, and a click
    opens GOAT. It never takes focus — it must not steal the caret from
    whatever he is typing in.

    v7 (his ask 2026-09-30): no card — just the words, floating beside the
    sphere like subtitles: light type with a soft dark shadow so it reads on
    a bright wallpaper as well as a dark one.
    """

    clicked = Signal()

    HOLD_MS = 9000      # how long the last words stay up after the voice stops
    TAIL = 240          # longest stretch shown; older words scroll off the top

    def __init__(self, theme: dict, scale: float = 1.0):
        super().__init__(None, Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("click to open GOAT — right-click to dismiss")
        self._t = dict(theme)
        self._anchor = QRect()
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # The label only measures and wraps; the words are painted by
        # paintEvent with a subtitle halo (a blur shadow alone vanished on a
        # white wallpaper).
        self._ink = QColor("#f4f7fb")
        self._fade = QTimer(self)
        self._fade.setSingleShot(True)
        self._fade.timeout.connect(self.hide)
        self.set_scale(scale)

    def set_scale(self, scale: float):
        s = max(0.8, float(scale or 1.0))
        self._w = int(round(300 * s))
        self._pad = int(round(14 * s))
        f = self.label.font()
        f.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
        f.setPixelSize(max(13, int(round(16 * s))))
        f.setWeight(QFont.DemiBold)
        self.label.setFont(f)
        self._halo = max(1.5, 2.0 * s)
        self._restyle()
        self._relayout()

    def set_theme(self, t: dict):
        self._t = dict(t)
        self._restyle()
        self.update()

    def _restyle(self):
        # Subtitles are light on any room: a dark-ink theme's paper colour
        # would vanish into the shadow that keeps it readable.
        paper = QColor(self._t.get("paper", "#f4ede0"))
        self._ink = QColor(paper.name() if paper.lightness() > 150 else "#f4f7fb")
        self.label.setStyleSheet("color: transparent; background: transparent;")

    def show_text(self, text: str, anchor: QRect):
        text = " ".join((text or "").split())
        if not text:
            return
        if len(text) > self.TAIL:
            cut = text[-self.TAIL:]
            text = "…" + cut[cut.find(" ") + 1:] if " " in cut else "…" + cut
        self._anchor = QRect(anchor)
        if text != self.label.text():
            self.label.setText(text)
        self._relayout()
        if not self.isVisible():
            self.show()
        self.raise_()
        pin_topmost(self)
        self._fade.start(self.HOLD_MS)

    def follow(self, anchor: QRect):
        """The dot moved — keep the card glued to it."""
        self._anchor = QRect(anchor)
        if self.isVisible():
            self._relayout()

    def _relayout(self):
        a = self._anchor
        scr = ((QApplication.screenAt(a.center()) if not a.isNull() else None)
               or QApplication.primaryScreen())
        area = scr.availableGeometry()
        # Open toward the middle of the screen, like a chat head does — a dot
        # parked on the right edge talks to its left. The words hug the dot:
        # right-aligned on its left, left-aligned on its right (his ask
        # 2026-09-30: "start from the right, stacked if needed").
        right_side = a.isNull() or a.center().x() >= area.center().x()
        self._align = Qt.AlignRight if right_side else Qt.AlignLeft
        # Only as wide as the words need, up to the cap; longer replies
        # wrap into stacked lines. Measured from the font, not
        # heightForWidth(): before the label has been polished that answers
        # with a stale, far too tall guess.
        cap = self._w - 2 * self._pad
        box = self.label.fontMetrics().boundingRect(
            QRect(0, 0, cap, 100000), Qt.TextWordWrap | self._align,
            self.label.text() or " ")
        inner = min(cap, box.width() + 2)
        h = box.height() + 2
        self.label.setFixedSize(inner, h)
        self.label.move(self._pad, self._pad)
        self.setFixedSize(inner + 2 * self._pad, h + 2 * self._pad)
        if a.isNull():
            return
        gap = max(4, self._pad // 4)
        if right_side:
            x = a.left() - gap - self.width()
        else:
            x = a.right() + 1 + gap
        y = a.center().y() - self.height() // 2
        x = min(max(x, area.left()), area.right() - self.width() + 1)
        y = min(max(y, area.top()), area.bottom() - self.height() + 1)
        self.move(x, y)

    def paintEvent(self, _ev):
        # No card. An alpha-1 wash keeps the whole box clickable (Windows
        # passes clicks through fully transparent pixels of a layered window)
        # while staying invisible.
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))
        text = self.label.text()
        if not text:
            return
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setFont(self.label.font())
        box = self.label.geometry()
        flags = int(Qt.TextWordWrap | getattr(self, "_align", Qt.AlignLeft) | Qt.AlignTop)
        # Halo: a wide faint ring, then a tight dark one, then the ink.
        for radius, alpha, steps in ((self._halo * 1.8, 55, 12), (self._halo, 190, 8)):
            p.setPen(QColor(0, 0, 0, alpha))
            for i in range(steps):
                a = i * math.tau / steps
                p.drawText(box.translated(round(math.cos(a) * radius),
                                          round(math.sin(a) * radius)), flags, text)
        p.setPen(self._ink)
        p.drawText(box, flags, text)

    def mouseReleaseEvent(self, ev):
        self._fade.stop()
        self.hide()
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        ev.accept()


TRANSCRIPT = os.path.join(GOAT_ROOT, "workspace", "transcript.jsonl")
MEMORY_MD = os.path.join(GOAT_ROOT, "workspace", "memory.md")
SKILLS_DIR = os.path.join(GOAT_ROOT, "workspace", ".claude", "skills")
SELF_MD = os.path.join(GOAT_ROOT, "workspace", "self.md")

# ---- the Home desk (v8 phase c): his Open list and recent days, read from
# the same memory files the brain uses, so the desk never invents anything.
_DESK_SMALL = {"on", "in", "of", "to", "the", "and", "for", "a", "an", "at",
               "by", "with", "from", "or"}
_DESK_ACRONYMS = {"GOAT", "APK", "RAM", "CPU", "GPU", "USB", "ADB", "API",
                  "URL", "PDF", "TTS", "STT", "AEC", "UI", "AI", "FB", "IG",
                  "OK", "PC", "TV", "EUR", "USD", "VPN", "DNS", "SSD"}
_DESK_CACHE: dict = {}
_OPEN_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s+[\dx:]+)?\s*:\s*")
_CAPS_LEAD = re.compile(r"^((?:[A-Z][A-Z0-9+&'/-]*|\d[\w.]*|v\d+|\+)"
                        r"(?:\s+(?:[A-Z][A-Z0-9+&'/-]*|\d[\w.]*|v\d+|\+))*)")
_LONG_PAREN = re.compile(r"\s*\([^()]{14,}\)")
_SENTENCE_END = re.compile(r"(?<=[a-z0-9)\]])\.(?:\s|$)|;\s")
# Windows that are never "what he's on" for the ambient line.
_AMBIENT_SKIP = {"Program Manager", "Windows Input Experience", "GOAT",
                 "GOAT live", ""}


def _desk_words(s: str) -> str:
    """Shouted memory headings read as a sentence: 'FABLE 5 ON CREDITS' ->
    'Fable 5 on credits'. Acronyms (APK, RAM, AI, GOAT) stay."""
    out = []
    for w in s.split():
        core = w.strip("()[],.:;+-/")
        if (core.isupper() and core not in _DESK_ACRONYMS
                and (len(core) > 2 or core.lower() in _DESK_SMALL)):
            w = w.lower()
        out.append(w)
    s = " ".join(out)
    return s[:1].upper() + s[1:]


def _desk_clip(s: str, n: int) -> str:
    s = " ".join(s.split()).strip(" ,;:-\u2014")
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-\u2014(+&") + "\u2026"


def _desk_read(path: str) -> str:
    """File text, cached by mtime (the desk refreshes after every turn)."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return ""
    hit = _DESK_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    _DESK_CACHE[path] = (mt, text)
    return text


def _md_section(text: str, head: str) -> list:
    """The '- ' bullets under '## head', up to the next heading."""
    rows, on = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            on = line[3:].strip().lower().startswith(head.lower())
            continue
        if on and line.startswith("- "):
            rows.append(line[2:].strip())
    return rows


def _first_sentence(s: str) -> str:
    s = re.sub(r"^\s*\([^()]*\)\s*:?", "", s)     # "(his order): \u2026"
    s = _LONG_PAREN.sub("", s).strip(" :;,.(\u2014-")
    return _SENTENCE_END.split(s, maxsplit=1)[0].strip()


def desk_open(text: str | None = None, n: int = 4) -> tuple:
    """(count, [(title, sub)]) from memory.md's ## Open list, newest first.
    Title = the item's shouted lead (or its first clause); sub = what is
    next ('Next:', then 'Blocked:', then 'UNVERIFIED'), else the rest of
    that first sentence."""
    if text is None:
        text = _desk_read(MEMORY_MD)
    items = _md_section(text, "Open")
    rows = []
    for raw in items[:n]:
        body = _OPEN_DATE.sub("", raw, count=1)
        m = _CAPS_LEAD.match(body)
        lead = m.group(1).strip() if m else ""
        if len(lead) >= 5 and sum(c.isupper() for c in lead) >= 3:
            title, rest = lead, body[m.end():]
        else:
            first = re.split(r"[:;(,.\u2014]| - ", body, maxsplit=1)[0]
            title, rest = first, body[len(first):]
        sub = ""
        for key, tag in (("Next", ""), ("NEXT", ""), ("Blocked", "blocked: "),
                         ("UNVERIFIED", "untested ")):
            k = re.search(r"\b" + key + r"\b\s*:?\s*", body)
            if k:
                sub = tag + _first_sentence(body[k.end():])
                break
        if not sub:
            sub = _first_sentence(rest)
        rows.append((_desk_clip(_desk_words(title), 36), _desk_clip(sub, 70)))
    return len(items), rows


def desk_days(text: str | None = None, n: int = 4) -> list:
    """[(\"Thu 22 Oct\", first sentence)] from self.md's Recent days."""
    if text is None:
        text = _desk_read(SELF_MD)
    rows = []
    for raw in _md_section(text, "Recent days")[:n]:
        m = re.match(r"(\d{4}-\d{2}-\d{2})[^:]*:\s*(.*)", raw)
        if not m:
            continue
        try:
            day = time.strptime(m.group(1), "%Y-%m-%d")
            title = time.strftime("%a %d %b", day).replace(" 0", " ")
        except ValueError:
            title = m.group(1)
        rows.append((title, _desk_clip(_first_sentence(m.group(2)), 70)))
    return rows

# Which icon a past exchange wears in the Recent list — a glance-level hint,
# nothing rides on it. First match wins; English and Georgian stems.
TOPIC_ICONS = [
    (("fasmetri", "site", "vercel", "website", "browser", "chrome", "brave",
      "საიტ"), "globe"),
    (("code", "bug", "python", "react", "script", "git", "error", "commit",
      "deploy", "კოდ"), "code"),
    (("study", "learn", "exam", "university", "lesson", "course", "სწავლ"), "book"),
    (("money", "price", "pay", "debt", "credit", "bank", "ფას", "ფულ", "ლარ"), "money"),
    (("minecraft", "game", "steam", "roblox", "ubisoft", "თამაშ"), "game"),
    (("video", "youtube", "tiktok", "ვიდეო"), "video"),
    (("music", "song", "playlist", "spotify", "მუსიკ", "სიმღერ"), "music"),
    (("screen", "window", "open ", "close ", "volume", "ეკრან", "გახსენი",
      "დახურე"), "desktop"),
    (("file", "folder", "photo", "picture", "image", "pdf", "ფაილ", "სურათ"), "folder"),
]

# Real things GOAT can do — the Tools page and the rail's Tools card. Each
# example is a phrase that actually works today (reflexes or the brain).
CAPABILITIES = [
    ("eye", "Sight", "Sees your screen: the front window, every open app and "
     "what's busy — and takes a screenshot when it needs the pixels.",
     ["What's on my screen?", "Which app is using the most CPU?"]),
    ("hand", "Hands", "Moves the mouse, types, clicks, opens and closes apps "
     "and runs commands on this PC — then checks it really happened.",
     ["Open Brave and go to YouTube", "Close Steam"]),
    ("bolt", "Reflexes", "Instant and offline: open or close apps and tabs, "
     "click a button by name, scroll, keys, volume, media, the time — no "
     "model, no wait, no usage.",
     ["Close this tab", "Click Subscribe", "Scroll down", "Mute"]),
    ("globe", "Web", "Searches and reads the live web, then answers from "
     "what it found — not from memory.",
     ["What's the weather in Essen today?"]),
    ("phone", "Phone", "Lives on your phone too: the floating orb, the side "
     "button assistant, and hands that open apps by command, never by "
     "tapping over you. Replies speak on whichever device you used last.",
     ["Open WhatsApp on my phone", "Speak on the laptop"]),
    ("mail", "Mail & calendar", "Reads your Gmail and Google Calendar "
     "through your own signed-in Chrome — no keys, nothing stored.",
     ["Any new mail?", "What's on today?"]),
    ("bell", "Watchers", "Keeps an eye on the machine while you work: "
     "finished downloads, low disk, low battery, a flapping charger, RAM "
     "pressure — told aloud and pushed to your phone.",
     ["How's my laptop?"]),
    ("doc", "Files", "Drop, paste (Ctrl+V) or pick files and images; Goat "
     "reads them and hands back the files it makes.",
     ["Summarise the file I just sent"]),
    ("mic", "Voice", "Hears and speaks English and Georgian, cancels your "
     "music from the mic, and stops the moment you talk over it.",
     ["Speak Georgian", "Quieter"]),
    ("memory", "Memory", "Keeps memory.md — what it knows about you, this "
     "machine and itself — and reads it every turn.",
     ["Remember that I prefer short answers"]),
    ("sync", "Self-upgrade", "Updates its own engine daily with a rollback "
     "net, and can change its own code, then restarts itself.",
     ["Check yourself for updates"]),
]

QUOTES = [
    "Small steps every day lead to big results.",
    "The summit is just the last step of many.",
    "Say the word. I'm listening.",
    "Climb steady; the view waits.",
    "Done is better than perfect — then make it perfect.",
]


def _ago(ts: float) -> str:
    d = max(0.0, time.time() - float(ts or 0))
    if d < 60:
        return "now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    if d < 7 * 86400:
        return f"{int(d // 86400)}d ago"
    return time.strftime("%d %b", time.localtime(ts))


def _topic_icon(text: str) -> str:
    low = (text or "").lower()
    for stems, key in TOPIC_ICONS:
        if any(s in low for s in stems):
            return key
    return "chat"


def _sentence_case(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:1].upper() + text[1:] if text else text


def _read_skills() -> list:
    out = []
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return out
    for name in names:
        path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        desc = ""
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(4000)
        except OSError:
            continue
        if head.startswith("---"):
            for line in head.split("\n")[1:]:
                if line.strip() == "---":
                    break
                if line.lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"')
        out.append((name, desc))
    return out


LIVE_TITLE = "GOAT live"   # screen_hands / live_view skip a window by this title


class LiveWindow(QWidget):
    """What the working brain is doing, while GOAT's own window is away.

    His ask, 2026-10-16: "when I give it a task I want to SEE what the model is
    doing — I don't think we have a section or a window for that." The
    Activity card exists, but screen work minimizes GOAT (stand_aside), so the
    card vanishes exactly when there is most to watch.

    This floats bottom-right while a work turn runs and the main window is
    not on screen: brain + clock, the last screenshot the brain looked at,
    the step in progress, its thinking, and the steps before.

    It must never get in the brain's way, so it is
      - click-through for GOAT's own mouse (screen_hands._live_glass): a
        click aimed at the app underneath lands there, not here. His mouse
        can drag it anywhere; double-click puts it back in the corner;
      - left out of every screen capture (WDA_EXCLUDEFROMCAPTURE): the
        brain's screenshots show the desktop, never this window;
      - skipped by stand_aside and live_view (LIVE_TITLE).
    """

    moved = Signal()  # he dropped it somewhere: remember the spot

    HISTORY = 5
    HOLD_MS = 5000    # stays up this long after the turn ends
    # Text caps keep the card a bounded size: a busy turn used to grow it past
    # the bottom of the screen (2026-10-17, "it gets weirdly down on my right").
    TASK_MAX = 110    # ~2 lines
    STEP_MAX = 110    # ~2 lines
    HIST_MAX = 54     # one line per past step
    THINK_MAX = 220   # ~4 lines
    EDGE = 16

    def __init__(self, theme: dict):
        # Not WindowTransparentForInput any more: he drags it (2026-10-17).
        # screen_hands._live_glass makes it click-through for GOAT's own mouse.
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setWindowTitle(LIVE_TITLE)
        self.setCursor(Qt.SizeAllCursor)
        self.pos_saved = None     # [x, y] top-left he chose; None = corner
        self._press = None
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.active = False       # a work turn is running (or just ended)
        self.worked = False       # it has taken at least one step
        self._t0 = 0.0
        self._model = ""
        self._state = ""
        self._steps: list[str] = []
        self._shot_path = ""
        self._t = dict(theme)

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(6)
        self.head = QLabel("")
        self.task = QLabel("")
        self.task.setWordWrap(True)
        self.shot = QLabel()
        self.shot.setAlignment(Qt.AlignCenter)
        self.shot.hide()
        self.cur = QLabel("")
        self.cur.setWordWrap(True)
        self.think = QLabel("")
        self.think.setWordWrap(True)
        self.hist = QLabel("")
        self.hist.setWordWrap(True)
        for w in (self.head, self.task, self.shot, self.cur, self.think, self.hist):
            if w is not self.shot:
                w.setTextFormat(Qt.PlainText)
            col.addWidget(w)
        self.setFixedWidth(380)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._paint_head)
        self._end = QTimer(self)
        self._end.setSingleShot(True)
        self._end.timeout.connect(self._finish)
        # Thinking streams in tiny pieces; coalesce the re-layouts so the card
        # doesn't thrash (it looked stuck under a flood of updates).
        self._lay = QTimer(self)
        self._lay.setSingleShot(True)
        self._lay.setInterval(80)
        self._lay.timeout.connect(self._relayout_now)
        self.set_theme(theme)

    @staticmethod
    def _clip(text: str, n: int) -> str:
        s = " ".join((text or "").split())
        return s if len(s) <= n else s[:n - 1].rstrip() + "…"

    # ---- look ----
    def set_theme(self, t: dict):
        self._t = dict(t)
        paper = t.get("paper", "#e9eefa")
        dim = t.get("dim", "#8e9ab4")
        acc = t.get("accent2", t.get("accent", "#6fb3ff"))
        base = "background: transparent; font-family: 'Segoe UI Variable Text', 'Segoe UI';"
        self.head.setStyleSheet(f"{base} color: {acc}; font-size: 12px; font-weight: 600;")
        self.task.setStyleSheet(f"{base} color: {paper}; font-size: 13px; font-weight: 600;")
        self.cur.setStyleSheet(f"{base} color: {paper}; font-size: 13px;")
        self.think.setStyleSheet(f"{base} color: {dim}; font-size: 12px; font-style: italic;")
        self.hist.setStyleSheet(f"{base} color: {dim}; font-size: 12px;")
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        bg = QColor(self._t.get("card", self._t.get("bg_bot", "#0f1a2e")))
        bg.setAlpha(235)
        line = QColor(self._t.get("accent", "#3b8bff"))
        line.setAlpha(150)
        p.setPen(QPen(line, 1))
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)

    # ---- feed (same events as the Activity card) ----
    def start(self, model: str, task: str):
        self._end.stop()
        self.active = True
        self.worked = False       # talk turns never use a tool: no window
        self._t0 = time.time()
        self._model = model
        self._state = "working"
        self._steps = []
        self._shot_path = ""
        self.shot.hide()
        self.task.setText(self._clip(task, self.TASK_MAX))
        self.cur.setText("")
        self.think.setText("")
        self.hist.setText("")
        self._tick.start()
        self._paint_head()

    def step(self, desc: str):
        if desc:
            self.worked = True
        prev = self.cur.text()
        if prev:
            self._steps.append("✓ " + self._clip(prev.removeprefix("▸ "),
                                                 self.HIST_MAX))
            self._steps = self._steps[-self.HISTORY:]
            self.hist.setText("\n".join(reversed(self._steps)))
        self.cur.setText(("▸ " + self._clip(desc, self.STEP_MAX)) if desc else "")
        self.think.setText("")
        self._relayout()

    def thinking(self, piece: str):
        n = self.THINK_MAX
        txt = (self.think.text().removeprefix("…") + piece)[-n:]
        # The tail of a long thought, from a word boundary.
        if len(txt) >= n and " " in txt[:40]:
            txt = "…" + txt[txt.find(" ") + 1:]
        self.think.setText(txt)
        self._relayout()

    def set_shot(self, path: str):
        pm = QPixmap(path)
        if pm.isNull():
            return
        self._shot_path = path
        self.shot.setPixmap(pm.scaled(352, 220, Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation))
        self.shot.show()
        self._relayout()

    def done(self, failed: str = ""):
        self._state = ("failed: " + failed[:60]) if failed else "done"
        if self.cur.text():
            self.step("")              # moves the last step into history
            self.cur.setText("")
        self._tick.stop()
        self._paint_head()
        self._end.start(self.HOLD_MS)

    def _finish(self):
        self.active = False
        self.hide()

    def _paint_head(self):
        bits = ["GOAT", self._model or "", self._state]
        if self._t0:
            up = int(time.time() - self._t0)
            bits.append(f"{up // 60}:{up % 60:02d}")
        self.head.setText("  ·  ".join(b for b in bits if b))

    # ---- placement ----
    def _relayout(self):
        if self.isVisible():
            self._lay.start()      # coalesced; restarts while updates flood in
        else:
            self._relayout_now()

    def _fit_height(self) -> int:
        lay = self.layout()
        lay.activate()
        if lay.hasHeightForWidth():
            return lay.totalHeightForWidth(self.width())
        return lay.totalSizeHint().height()

    def _relayout_now(self):
        self._lay.stop()
        self.cur.setVisible(bool(self.cur.text()))
        self.think.setVisible(bool(self.think.text()))
        self.hist.setVisible(bool(self.hist.text()))
        area = QApplication.primaryScreen().availableGeometry()
        room = max(120, area.height() - 2 * self.EDGE)
        h = self._fit_height()
        if h > room and not self.shot.isHidden():
            self.shot.hide()       # a short screen drops the picture first
            h = self._fit_height()
        # adjustSize() under-measured wrapped labels; Qt then grew the window
        # downward from a fixed top, off the bottom of the screen. A fixed
        # height measured for the fixed width, anchored bottom-right, can't.
        h = min(h, room)
        self.setFixedHeight(h)
        if self._press is not None:
            return                 # mid-drag: size only, his hand moves it
        p = self.pos_saved
        if (isinstance(p, (list, tuple)) and len(p) == 2
                and all(isinstance(n, (int, float)) for n in p)):
            x, y = int(p[0]), int(p[1])
        else:
            x = area.right() - self.width() - self.EDGE
            y = area.bottom() - h - self.EDGE
        # Clamped every time: growing never pushes it off the bottom.
        x = min(max(x, area.left()), area.right() - self.width() + 1)
        y = min(max(y, area.top()), area.bottom() - h + 1)
        self.move(x, y)

    # ---- drag anywhere; double-click sends it back to the corner ----
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._press is not None and ev.buttons() & Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._press)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._press is not None:
            self._press = None
            self.pos_saved = [self.x(), self.y()]
            self._relayout_now()   # clamps a drop past the edge
            self.pos_saved = [self.x(), self.y()]
            self.moved.emit()
            ev.accept()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = None
            self.pos_saved = None
            self._relayout_now()
            self.moved.emit()
            ev.accept()

    def reveal(self, on: bool):
        if on and not self.isVisible():
            self._relayout_now()
            self.show()
            _exclude_from_capture(self)
            pin_topmost(self)
        elif not on and self.isVisible():
            self.hide()


def _exclude_from_capture(w: QWidget):
    """WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+): screenshots, BitBlt and
    screen recorders see straight through this window. Best effort."""
    if QGuiApplication.platformName() != "windows":
        return
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(
            ctypes.wintypes.HWND(int(w.winId())), 0x11)
    except Exception:  # noqa: BLE001 — older Windows: it just shows in shots
        pass


class QuoteCard(QFrame):
    """Today, at the foot of the right rail: the clock, the date and a line,
    over a small painted range — the concept's quote card, made useful."""

    def __init__(self):
        super().__init__()
        self._t = THEMES["midnight"]
        self._pm: QPixmap | None = None
        self._key = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(4)
        self.clock = QLabel("")
        self.clock.setObjectName("clock")
        lay.addWidget(self.clock)
        lay.addStretch(1)
        day = int(time.time() // 86400)
        self.quote = QLabel(f"“{QUOTES[day % len(QUOTES)]}”")
        self.quote.setObjectName("quote")
        self.quote.setWordWrap(True)
        lay.addWidget(self.quote)
        sig = QHBoxLayout()
        self.mark = GoatMark(16, glow=False)
        sig.addWidget(self.mark)
        sig.addWidget(mklabel("— Goat", "rowTime"))
        sig.addStretch(1)
        lay.addLayout(sig)

    def set_theme(self, t: dict):
        self._t = dict(t)
        self._pm = None
        self.mark.set_theme(t)
        self.update()

    def paintEvent(self, _ev):
        key = (self.width(), self.height(), self._t.get("bg_top"), self._t.get("glow"))
        if self._pm is None or key != self._key:
            pm = QPixmap(max(1, self.width()), max(1, self.height()))
            pm.fill(Qt.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.Antialiasing, True)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(pm.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
            q.setClipPath(clip)
            paint_scene(q, pm.rect(), self._t, seed=23, peak_x=0.72, stars=40,
                        fade_to=QColor(self._t["card"]))
            # Veil the left so the words stay readable over the range.
            veil = QLinearGradient(0, 0, pm.width(), 0)
            veil.setColorAt(0.0, _qc(self._t["card"], 235))
            veil.setColorAt(0.6, _qc(self._t["card"], 120))
            veil.setColorAt(1.0, _qc(self._t["card"], 20))
            q.fillRect(pm.rect(), veil)
            q.setClipping(False)
            q.setPen(QPen(_qc(self._t["line"], 230), 1))
            q.setBrush(Qt.NoBrush)
            q.drawRoundedRect(QRectF(pm.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
            q.end()
            self._pm, self._key = pm, key
        QPainter(self).drawPixmap(0, 0, self._pm)


class GoatWindow(QWidget):
    event_sig = Signal(str, str)

    PAGES = [("home", "Home"), ("chat", "Chat"), ("skills", "Skills"),
             ("files", "Files"), ("memory", "Memory"), ("tools", "Tools"),
             ("settings", "Settings")]

    def __init__(self):
        super().__init__()
        self.cfg = load_ui_config()
        self.setWindowTitle("GOAT")
        flags = Qt.FramelessWindowHint
        if self.cfg.get("ontop"):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        # Frameless still needs a floor — otherwise a resize can crush it to
        # nothing (usability pass 2026-07-12). v7 has three columns: wider.
        self.setMinimumSize(960, 560)
        self._restore_geometry()   # remembered box, or a sane default
        self.setAcceptDrops(True)
        self.on_submit = None
        self.on_work = None
        self.on_files = None
        self._drag: QPoint | None = None
        self._collapsing = False   # guards collapse() <-> changeEvent recursion
        self._reply_label: QLabel | None = None
        self._you_label: QLabel | None = None
        self._t0 = time.time()
        self._model = "…"
        self._work_model = ""
        self._statew = "booting"
        self._status_hold = 0.0  # until this time, hud_tick may not stomp
        self._usage = ""
        self._plan = ""
        self._plan_fh = None     # Claude's 5-hour use, % (ambient line)
        self._claude_out = False
        self._turnlang = ""
        self._claude_reset = ""
        self._work_mode = False    # composer sends to the working brain
        self._page = "home"
        self._prev_page = "home"
        self._side_pinned = False  # he flipped Tools/Activity by hand
        self._sent_files: list = []
        self._made_files: list = []
        self._tx_cache: tuple = (None, [])
        self._themed: list = []    # widgets with their own set_theme()
        self._scaled: list = []    # widgets with their own set_scale()
        self._mood = ""
        self._mic_hot = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.canvas = Backdrop()
        outer.addWidget(self.canvas)
        self.goat = None  # engine handle, set by bind_engine()
        self._theme_name = self.cfg["theme"]

        row = QHBoxLayout(self.canvas)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.rail = self._build_rail()
        row.addWidget(self.rail)
        self.center = self._build_center()
        row.addWidget(self.center, 1)
        self.side = self._build_side()
        row.addWidget(self.side)
        self._titlebar_h = TITLEBAR_H

        self.event_sig.connect(self._on_event)

        QShortcut(QKeySequence(Qt.Key_F11), self, self.toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._escape)
        QShortcut(QKeySequence("Ctrl+K"), self, self._show_cmd)
        QShortcut(QKeySequence("Ctrl+F"), self, self._show_search)
        QShortcut(QKeySequence("Ctrl+T"), self, self.cycle_theme)
        QShortcut(QKeySequence("Ctrl+,"), self, self.toggle_settings)
        QShortcut(QKeySequence("Ctrl+O"), self, self._pick_files)
        QShortcut(QKeySequence("Ctrl+M"), self, self.toggle_mic)
        QShortcut(QKeySequence("Ctrl+N"), self, self.new_chat)
        QShortcut(QKeySequence("Ctrl+E"), self, self.cycle_effort)
        QShortcut(QKeySequence("Ctrl+B"), self, self.toggle_bubble)
        QShortcut(QKeySequence("Ctrl+L"), self, self.cycle_lang)
        # Manual work dispatch: Ctrl+Enter → working brain, +Shift → hard.
        QShortcut(QKeySequence("Ctrl+Return"), self, lambda: self._submit_work(False))
        QShortcut(QKeySequence("Ctrl+Enter"), self, lambda: self._submit_work(False))
        QShortcut(QKeySequence("Ctrl+Shift+Return"), self, lambda: self._submit_work(True))
        QShortcut(QKeySequence("Ctrl+Shift+Enter"), self, lambda: self._submit_work(True))

        # Collapsed form. Built last so apply_theme() below has something to
        # style, and hidden until he asks for it.
        self.bubble = Bubble({**THEMES.get(self._theme_name, THEMES["midnight"]),
                              **(self.cfg.get("colors") or {})},
                             float(self.cfg.get("scale", 1.0)))
        self.bubble.clicked.connect(self.expand)
        self.bubble.moved.connect(self._save_bubble_pos)
        self.pop = MessagePop({**THEMES.get(self._theme_name, THEMES["midnight"]),
                               **(self.cfg.get("colors") or {})},
                              float(self.cfg.get("scale", 1.0)))
        self.pop.clicked.connect(self.expand)
        self.bubble.relocated.connect(
            lambda: self.pop.follow(self.bubble.frameGeometry()))
        # Windows knocks floating windows out of the topmost band and shoves
        # them around on display changes; while collapsed, a slow keeper puts
        # the dot back where he left it, on top, with the card glued beside it.
        self._keeper = QTimer(self)
        self._keeper.setInterval(1500)
        self._keeper.timeout.connect(self._keep_bubble_up)
        # Live window (2026-10-16): the work turn, watchable while the main
        # window is away. A slow check decides when it shows — the main
        # window can leave by stand_aside, collapse or his own minimize.
        self.live = LiveWindow({**THEMES.get(self._theme_name, THEMES["midnight"]),
                                **(self.cfg.get("colors") or {})})
        self.live.pos_saved = self.cfg.get("live_pos")
        self.live.moved.connect(self._save_live_pos)
        self._live_check = QTimer(self)
        self._live_check.setInterval(600)
        self._live_check.timeout.connect(self._sync_live)
        # The greeting follows the clock (evening → late → morning).
        self._hello_timer = QTimer(self)
        self._hello_timer.setInterval(30_000)   # the clock is on the page
        self._hello_timer.timeout.connect(self._refresh_greeting)
        self._hello_timer.start()
        # The ambient line under the presence (only works while Home shows).
        self._ambient_timer = QTimer(self)
        self._ambient_timer.setInterval(5_000)
        self._ambient_timer.timeout.connect(self._refresh_ambient)
        self._ambient_timer.start()
        # Recent list re-reads the transcript shortly after a turn lands.
        self._recent_timer = QTimer(self)
        self._recent_timer.setSingleShot(True)
        self._recent_timer.timeout.connect(self._refresh_recent)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._refresh_recent)

        self.apply_theme(self._theme_name)
        self._refresh_greeting()
        self._refresh_recent()
        self._refresh_side()
        self.show_page("home")

    # =====================================================================
    # construction
    # =====================================================================
    def _build_rail(self) -> QWidget:
        rail = QWidget()
        lay = self._rail_lay = QVBoxLayout(rail)
        lay.setSpacing(4)
        brand = QHBoxLayout()
        brand.setSpacing(12)
        self.rail_mark = GoatMark(50)
        self._themed.append(self.rail_mark)
        self._scaled.append(self.rail_mark)
        brand.addWidget(self.rail_mark)
        words = QVBoxLayout()
        words.setSpacing(0)
        words.addWidget(mklabel("Goat", "brand"))
        words.addWidget(mklabel("Your Personal AI Assistant", "brandSub"))
        brand.addLayout(words)
        brand.addStretch(1)
        lay.addLayout(brand)
        self._rail_gap = lay.count()
        lay.addSpacing(30)
        self.nav: dict[str, QPushButton] = {}
        for key, label in self.PAGES:
            b = QPushButton("  " + label)
            b.setObjectName("nav")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            lay.addWidget(b)
            self.nav[key] = b
        lay.addStretch(1)

        # Presence (v8): the ring on its string, small, in a status card —
        # the one thing a voice assistant must always show is whether it
        # hears you.
        card = QFrame()
        card.setObjectName("presence")
        cl = self._presence_lay = QVBoxLayout(card)
        cl.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.statedot = QLabel("●")
        self.statedot.setObjectName("statedot")
        top.addWidget(self.statedot)
        self.stateword = capsify(QLabel("booting"))
        self.stateword.setObjectName("stateword")
        self.stateword.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(self.stateword, 1)
        cl.addLayout(top)
        self.presence_mini = Presence(compact=True)
        cl.addWidget(self.presence_mini)
        self.footer = QLabel("")
        self.footer.setObjectName("footer")
        self.footer.setWordWrap(True)
        cl.addWidget(self.footer)
        lay.addWidget(card)
        return rail

    def _build_center(self) -> QWidget:
        center = QWidget()
        lay = self._center_lay = QVBoxLayout(center)
        lay.setSpacing(0)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.pages["home"] = self._build_home()
        self.pages["chat"] = self._build_chat()
        self.pages["skills"] = self._build_skills()
        self.pages["files"] = self._build_files()
        self.pages["memory"] = self._build_memory()
        self.pages["tools"] = self._build_tools()
        self.panel = SettingsPage(self)
        self.pages["settings"] = self.panel
        for key, _ in self.PAGES:
            self.stack.addWidget(self.pages[key])
        lay.addWidget(self.stack, 1)
        lay.addSpacing(14)
        lay.addWidget(self._build_composer())
        return center

    def _scroll_page(self, content: QWidget) -> QScrollArea:
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        sc.setWidget(content)
        return sc

    def _build_home(self) -> QWidget:
        """v8 Home (design.md, mockup A): GOAT in the middle of the room.
        Greeting, the presence, the brains line and the ambient line, then
        his desk: Open (memory.md), Recent days (self.md), Ask me. While he
        searches (Ctrl F) the results take the desk's place."""
        content = QWidget()
        lay = self._home_lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 8, 8)
        lay.setSpacing(0)
        self._hero_gap = QWidget()
        self._hero_gap.setFixedHeight(40)
        lay.addWidget(self._hero_gap)
        self.hello = QLabel("")
        self.hello.setObjectName("hello")
        self.hello.setAlignment(Qt.AlignHCenter)
        self.hello.setWordWrap(True)
        lay.addWidget(self.hello)
        self.hello_sub = mklabel("", "helloSub", wrap=True)
        self.hello_sub.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.hello_sub)

        # The presence at full size (ring r 50 at 1x).
        self.home_presence = Presence()
        self._themed.append(self.home_presence)
        lay.addWidget(self.home_presence)
        self.home_state = capsify(QLabel(""))
        self.home_state.setObjectName("orbCaption")
        self.home_state.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.home_state)
        lay.addSpacing(8)
        self.brains_line = QLabel("")
        self.brains_line.setObjectName("brainsLine")
        self.brains_line.setTextFormat(Qt.RichText)
        self.brains_line.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.brains_line)
        lay.addSpacing(14)
        self.ambient = ElideLabel("", "ambient")
        self.ambient.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.ambient)
        self._desk_gap = lay.count()
        lay.addSpacing(40)

        # ---- his desk: three quiet columns ----
        desk = self.desk = QWidget()
        dl = self._desk_lay = QHBoxLayout(desk)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(40)
        self.desk_cols: dict = {}
        self.desk_heads: dict = {}
        self._desk_sig: dict = {}
        for key, head in (("open", "Open"), ("days", "Recent days"),
                          ("ask", "Ask me")):
            col = QVBoxLayout()
            col.setSpacing(0)
            h = mklabel(head, "sectionTitle")
            h.setContentsMargins(8, 0, 0, 6)
            col.addWidget(h)
            box = QVBoxLayout()
            box.setSpacing(0)
            col.addLayout(box)
            col.addStretch(1)
            dl.addLayout(col, 1)
            self.desk_cols[key] = box
            self.desk_heads[key] = h
        lay.addWidget(desk)

        # ---- search results (the old Recent card), only while searching ----
        rc = self.recent_card = QFrame()
        rc.setObjectName("card")
        rl = self._recent_card_lay = QVBoxLayout(rc)
        rl.setContentsMargins(8, 12, 8, 8)
        rl.setSpacing(4)
        head = QHBoxLayout()
        head.setContentsMargins(10, 0, 8, 4)
        head.setSpacing(10)
        head.addWidget(icon_label("search", "ico"))
        self.recent_title = mklabel("Results", "sectionTitle")
        head.addWidget(self.recent_title)
        head.addStretch(1)
        clear = QPushButton("Clear  \u2715")
        clear.setObjectName("link")
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(lambda: self.search.clear())
        head.addWidget(clear)
        rl.addLayout(head)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(0)
        rl.addLayout(self.recent_box)
        rc.hide()
        lay.addWidget(rc)
        lay.addStretch(1)
        return self._scroll_page(content)

    def _fill_desk(self, key: str, rows: list, empty: str = ""):
        """Rebuild one desk column from [(title, sub, on_click)]; skipped
        when nothing changed (it runs after every turn)."""
        sig = tuple((t, s) for t, s, _cb in rows) or (empty,)
        if self._desk_sig.get(key) == sig:
            return
        self._desk_sig[key] = sig
        box = self.desk_cols[key]
        while box.count():
            w = box.takeAt(0).widget()
            if w is not None:
                if w in self._scaled:
                    self._scaled.remove(w)
                w.setParent(None)
                w.deleteLater()
        if not rows:
            e = mklabel(empty, "empty")
            e.setContentsMargins(8, 8, 8, 8)
            box.addWidget(e)
            return
        k = float(self.cfg.get("scale", 1.0))
        for i, (title, sub, cb) in enumerate(rows):
            r = DeskRow(title, sub)
            r.setProperty("first", "true" if i == 0 else "false")
            r.set_scale(k)
            r.clicked.connect(cb)
            box.addWidget(r)
            self._scaled.append(r)

    def _refresh_desk(self):
        if not hasattr(self, "desk_cols"):
            return
        n, items = desk_open()
        self.desk_heads["open"].setText(f"Open  \u00b7  {n}" if n else "Open")
        self._fill_desk("open", [
            (t, s, lambda t=t: self._prefill(f"Let's continue: {t}. "))
            for t, s in items], "Nothing open.")
        self._fill_desk("days", [
            (t, s, lambda t=t: self._prefill(f"What did we do on {t}?"))
            for t, s in desk_days()], "No days written yet.")
        self._fill_desk("ask", [
            ("Look at my screen", "what's on it, what's wrong", self._act_screen),
            ("What's in my inbox", "both Gmail accounts",
             lambda: self._ask("What's in my inbox?")),
            ("Hand Claude a task", "work mode on, then say or type it",
             self._act_work),
            ("Morning briefing", "open items, vitals, mail",
             lambda: self._ask("Give me my briefing.")),
        ])

    def _refresh_brains_line(self):
        """'● voice Gemini …      ● Claude Opus 5.5 · free' under the
        presence: who talks, who works, and whether Claude is free."""
        if not hasattr(self, "brains_line"):
            return
        import html as _html
        t = self._theme()
        brains = getattr(self, "_brains", None)
        pick = self.cfg.get("work_model", "opus 5.5").strip()
        work = ((brains[1] if brains and brains[1] else "")
                or self._work_model or pick).strip()
        work = work.title() if work.islower() else work
        busy = bool(self.goat and self.goat.busy) or bool(self.work_panel._t0)
        if self._claude_out:
            state = "out" + (f" \u00b7 back {self._claude_reset}"
                             if self._claude_reset else "")
        else:
            state = "working" if busy else "free"

        def part(col: str, who: str, name: str, tail: str = "") -> str:
            tail = f" \u00b7 {tail}" if tail else ""
            return (f"<span style='color:{col}'>\u25cf&nbsp; {who}</span>"
                    f"&nbsp;&nbsp;<span style='color:{t['dim']}'>"
                    f"{_html.escape(name)}{_html.escape(tail)}</span>")

        bits = []
        if brains and brains[0]:
            bits.append(part(t["accent"], "voice", brains[0]))
        bits.append(part(t["work"], "Claude", work, state))
        text = ("&nbsp;" * 10).join(bits)
        if text != self.brains_line.text():
            self.brains_line.setText(text)
        self._brains_sig = (busy, self._claude_out)

    def _refresh_ambient(self):
        """The ambient line: what he's on, phone, RAM, battery, Claude's
        5-hour use. Every 5 s, only while Home is on screen (~3 ms)."""
        if (not hasattr(self, "ambient") or self._page != "home"
                or not self.isVisible() or self.isMinimized()):
            return
        parts = []
        try:
            import screen_hands
            me = os.getpid()
            for w in screen_hands.windows():
                title = (w.get("title") or "").strip()
                if (w.get("pid") == me or w.get("minimized")
                        or title in _AMBIENT_SKIP):
                    continue
                parts.append("on " + _desk_clip(title, 48))
                break
        except Exception:  # noqa: BLE001 — the line is a glance, never a crash
            pass
        ph = getattr(self.goat, "phone", None) if self.goat else None
        if ph is not None and getattr(ph, "clients", None):
            parts.append("phone linked")
        try:
            import psutil
            parts.append(f"RAM {psutil.virtual_memory().percent:.0f}%")
            bat = psutil.sensors_battery()
            if bat is not None:
                parts.append(f"battery {bat.percent:.0f}%"
                             + (", charging" if bat.power_plugged else ""))
        except Exception:  # noqa: BLE001
            pass
        if self._claude_out:
            parts.append("Claude out" + (f", back {self._claude_reset}"
                                         if self._claude_reset else ""))
        elif self._plan_fh is not None:
            parts.append(f"Claude {self._plan_fh}% of 5-hour")
        text = "     \u00b7     ".join(parts)
        if text != self.ambient.text():
            self.ambient.setText(text)

    def _build_chat(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(mklabel("Conversation", "pageTitle"))
        head.addStretch(1)
        for text, cb in (("Copy reply", self.copy_last_reply),
                         ("New chat", self.new_chat)):
            b = QPushButton(text)
            b.setObjectName("actbtn")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            head.addWidget(b)
        lay.addLayout(head)

        # ---- the presence, docked over the conversation (v8: r 28 px) ----
        self.presence = Presence()
        self._themed.append(self.presence)
        lay.addWidget(self.presence)
        self.presence_caption = capsify(QLabel(""))
        self.presence_caption.setObjectName("orbCaption")
        self.presence_caption.setAlignment(Qt.AlignHCenter)
        lay.addWidget(self.presence_caption)

        # ---- the page: the conversation ----
        self.col = QVBoxLayout()
        self.col.setSpacing(12)
        self.col.setContentsMargins(*READ_MARGIN)
        self.col.addStretch(1)
        host = QWidget()
        host.setLayout(self.col)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(host)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Both fills off, or the viewport paints its own near-black over the
        # backdrop and the page reads as a faint band.
        self.scroll.viewport().setAutoFillBackground(False)
        host.setAutoFillBackground(False)
        # Yesterday's tail: repaint recent exchanges dimmed, so a restart
        # doesn't LOOK like amnesia (the engine resumes the session anyway).
        restored = self._load_transcript_tail()
        self.epigraph = None
        if not restored:
            self.epigraph = QLabel("Say the word.")
            self.epigraph.setObjectName("epigraph")
            self.epigraph.setAlignment(Qt.AlignHCenter)
            self.epigraph.setContentsMargins(0, 90, 0, 0)
            self.col.insertWidget(0, self.epigraph)
        # Follow mode: auto-scroll only while he's already at the bottom.
        self._pin = BottomFollow(self.scroll)
        lay.addWidget(self.scroll, 1)
        return page

    def _page_shell(self, title: str, sub: str):
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 8, 12)
        lay.setSpacing(14)
        lay.addWidget(mklabel(title, "pageTitle"))
        if sub:
            lay.addWidget(mklabel(sub, "pageSub", wrap=True))
        return content, lay

    def _build_skills(self) -> QWidget:
        content, lay = self._page_shell(
            "Skills", "Playbooks Goat follows for bigger jobs. Tap one to ask for it.")
        grid = CardGrid(spacing=12, min_w=280)
        self._scaled.append(grid)
        self._skill_cards = []
        for name, desc in _read_skills():
            c = Clickable("card")
            cl = QVBoxLayout(c)
            cl.setContentsMargins(16, 14, 16, 14)
            cl.setSpacing(6)
            h = QHBoxLayout()
            t = icon_label("skills", "icoTile")
            t.setFixedSize(34, 34)
            h.addWidget(t)
            h.addWidget(mklabel(name.replace("-", " ").title(), "cardTitle"), 1)
            cl.addLayout(h)
            d = mklabel((desc[:170] + "…") if len(desc) > 170 else desc,
                        "cardSub", wrap=True)
            cl.addWidget(d)
            cl.addStretch(1)
            c.setMinimumHeight(130)
            c.clicked.connect(lambda n=name: self._prefill(f"Use your {n} skill: "))
            grid.add(c)
            self._skill_cards.append(c)
        if not self._skill_cards:
            lay.addWidget(mklabel("No skills found in workspace/.claude/skills.", "empty"))
        lay.addWidget(grid)
        lay.addStretch(1)
        return self._scroll_page(content)

    def _build_files(self) -> QWidget:
        content, lay = self._page_shell(
            "Files", "Drop files anywhere on the window, paste an image with "
            "Ctrl+V, or pick them — Goat reads them. Files it makes land here too.")
        bar = QHBoxLayout()
        for text, cb in (("Send a file…", self._pick_files),
                         ("Open inbox folder", lambda: self._open_path(INBOX))):
            b = QPushButton(text)
            b.setObjectName("actbtn")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.files_box = QVBoxLayout()
        self.files_box.setSpacing(14)
        lay.addLayout(self.files_box)
        lay.addStretch(1)
        return self._scroll_page(content)

    def _build_memory(self) -> QWidget:
        content, lay = self._page_shell(
            "Memory", "What Goat keeps about you, this machine and itself "
            "(workspace/memory.md). Say “remember …” to add to it.")
        bar = QHBoxLayout()
        for text, cb in (("Open in editor", lambda: self._open_path(MEMORY_MD)),
                         ("Reload", self._refresh_memory)):
            b = QPushButton(text)
            b.setObjectName("actbtn")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(22, 18, 22, 18)
        self.memory_text = QLabel("")
        self.memory_text.setObjectName("doc")
        self.memory_text.setTextFormat(Qt.MarkdownText)
        self.memory_text.setWordWrap(True)
        self.memory_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cl.addWidget(self.memory_text)
        lay.addWidget(card)
        lay.addStretch(1)
        return self._scroll_page(content)

    def _build_tools(self) -> QWidget:
        content, lay = self._page_shell(
            "Tools", "What Goat can actually do on this PC. Tap an example to "
            "put it in the message box.")
        grid = CardGrid(spacing=12, min_w=300)
        self._scaled.append(grid)
        for key, title, desc, examples in CAPABILITIES:
            c = QFrame()
            c.setObjectName("card")
            cl = QVBoxLayout(c)
            cl.setContentsMargins(16, 14, 16, 14)
            cl.setSpacing(8)
            h = QHBoxLayout()
            t = icon_label(key, "icoTile")
            t.setFixedSize(34, 34)
            h.addWidget(t)
            h.addWidget(mklabel(title, "cardTitle"), 1)
            cl.addLayout(h)
            cl.addWidget(mklabel(desc, "cardSub", wrap=True))
            ex = FlowLayout(spacing=6)
            for e in examples:
                b = QPushButton(e)
                b.setObjectName("chip")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, s=e: self._prefill(s))
                ex.addWidget(b)
            cl.addLayout(ex)
            cl.addStretch(1)
            grid.add(c)
        lay.addWidget(grid)
        lay.addStretch(1)
        return self._scroll_page(content)

    def _build_composer(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.composer = QFrame()
        self.composer.setObjectName("composer")
        cl = self._composer_lay = QHBoxLayout(self.composer)
        cl.setSpacing(4)
        attach = QPushButton(IC["attach"])
        attach.setObjectName("cbtn")
        attach.setCursor(Qt.PointingHandCursor)
        attach.setToolTip("send a file — ctrl+o, or drop / paste it")
        attach.clicked.connect(self._pick_files)
        cl.addWidget(attach)
        self.input = QLineEdit()
        self.input.setObjectName("cmd")
        self.input.setPlaceholderText("Message Goat — or just say the word")
        self.input.returnPressed.connect(self._submit)
        cl.addWidget(self.input, 1)
        # Mic toggle lives on the composer — the single most-used switch of a
        # voice assistant stays one click away (usability pass 2026-07-12).
        self.mic_btn = QPushButton(IC["mic"])
        self.mic_btn.setObjectName("micbtn")
        self.mic_btn.setCursor(Qt.PointingHandCursor)
        self.mic_btn.setToolTip("microphone — click or ctrl+m to mute/unmute")
        self.mic_btn.clicked.connect(self.toggle_mic)
        cl.addWidget(self.mic_btn)
        send = QPushButton(IC["send"])
        send.setObjectName("sendbtn")
        send.setCursor(Qt.PointingHandCursor)
        send.setToolTip("send — enter (ctrl+enter: working brain)")
        send.clicked.connect(self._submit)
        cl.addWidget(send)
        lay.addWidget(self.composer)

        chips = FlowLayout(spacing=8)
        self.chip_model = self._chip("", self._menu_model, "working brain")
        self.chip_effort = self._chip("", self._menu_effort, "thinking depth — ctrl+e")
        self.chip_work = self._chip("  Work mode", self._toggle_work_mode,
                                    "enter sends to the working brain (ctrl+enter always does)")
        self.chip_web = self._chip("  Web", lambda: self._prefill("Search the web for "),
                                   "ask about something live")
        self.chip_screen = self._chip("  Screen", self._act_screen, "what's on my screen?")
        self.chip_file = self._chip("  File", self._pick_files, "send a file — ctrl+o")
        self.chip_lang = self._chip("", self._menu_lang, "language — ctrl+l")
        for c in (self.chip_model, self.chip_effort, self.chip_work, self.chip_web,
                  self.chip_screen, self.chip_file, self.chip_lang):
            chips.addWidget(c)
        lay.addLayout(chips)
        # Labels the row can drop to icons when it would otherwise wrap.
        self._chip_labels = {c: c.text() for c in (self.chip_work, self.chip_web,
                                                   self.chip_screen, self.chip_file)}
        self._chips_all = (self.chip_model, self.chip_effort, self.chip_work,
                           self.chip_web, self.chip_screen, self.chip_file,
                           self.chip_lang)
        return box

    def _fit_chips(self, avail: int):
        """One row of chips, always. At 150% on a 1536px screen the row
        wrapped to THREE lines and the chat kept room for one message
        (2026-10-01); when the labels don't fit, the plain ones go icon-only
        — their tooltips still name them."""
        if not hasattr(self, "_chip_labels"):
            return
        self._chips_avail = avail
        eff = self.cfg.get("effort", "high")
        # Full model/language labels, as _refresh_side last wrote them.
        for c, full in getattr(self, "_chips_full", {}).items():
            c.setText(full)

        def dress(compact: bool):
            # ("slim", not "icon": that name is QPushButton's own icon
            # property, and setting it clobbered the icon.)
            self._chips_compact = compact
            for c, label in self._chip_labels.items():
                c.setText("" if compact else label)
            for c in self._chips_all:
                c.setProperty("slim", "true" if compact else "false")
                repolish(c)
            # The paperclip in the message box already sends a file.
            self.chip_file.setVisible(not compact)
            self.chip_effort.setText(f"  {eff}  ▾" if compact
                                     else f"  Thinking: {eff}  ▾")
            if compact:
                for c in (self.chip_model, self.chip_effort, self.chip_lang):
                    c.setText(" " + " ".join(c.text().split()))

        dress(False)
        need = sum(c.sizeHint().width() for c in self._chips_all) \
            + 8 * (len(self._chips_all) - 1)
        if need > avail:
            dress(True)

    def _chip(self, text: str, cb, tip: str) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("chip")
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(cb)
        return b

    def _build_side(self) -> QWidget:
        side = QWidget()
        lay = self._side_lay = QVBoxLayout(side)
        lay.setSpacing(12)
        ctl = QHBoxLayout()
        ctl.setSpacing(2)
        ctl.addStretch(1)
        b_min = QPushButton(IC["min"])
        b_min.setObjectName("winbtn")
        # Collapse, not minimize: a voice assistant on the taskbar hides the
        # one thing worth seeing — whether it is listening (2026-09-14).
        b_min.setToolTip("collapse to a bubble — ctrl+b")
        b_min.clicked.connect(self.collapse)
        self.b_full = QPushButton(IC["max"])
        self.b_full.setObjectName("winbtn")
        self.b_full.setToolTip("fullscreen — f11")
        self.b_full.clicked.connect(self.toggle_fullscreen)
        b_close = QPushButton(IC["close"])
        b_close.setObjectName("winclose")
        b_close.setToolTip("quit GOAT")
        b_close.clicked.connect(QApplication.quit)
        for b in (b_min, self.b_full, b_close):
            ctl.addWidget(b)
        lay.addLayout(ctl)

        srow = QHBoxLayout()
        srow.setSpacing(8)
        sf = QFrame()
        sf.setObjectName("search")
        sl = self._search_lay = QHBoxLayout(sf)
        sl.setSpacing(8)
        sl.addWidget(icon_label("search", "ico"))
        self.search = QLineEdit()
        self.search.setObjectName("searchField")
        self.search.setPlaceholderText("Search conversations…")
        self.search.textChanged.connect(lambda _t: self._search_timer.start(220))
        sl.addWidget(self.search, 1)
        sl.addWidget(mklabel("Ctrl F", "kbd"))
        srow.addWidget(sf, 1)
        self.gear_btn = QPushButton(IC["settings"])
        self.gear_btn.setObjectName("gear")
        self.gear_btn.setCursor(Qt.PointingHandCursor)
        self.gear_btn.setToolTip("settings — ctrl+,")
        self.gear_btn.clicked.connect(self.toggle_settings)
        srow.addWidget(self.gear_btn)
        lay.addLayout(srow)

        # Below the search row the cards scroll: on a short window (his box
        # is 693px tall) they used to squeeze until rows clipped their text.
        cards_host = QWidget()
        outer_lay = lay
        lay = self._cards_lay = QVBoxLayout(cards_host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.side_scroll = self._scroll_page(cards_host)
        outer_lay.addWidget(self.side_scroll, 1)

        # Brains (2026-10-24, his order: "show this in the interface"): the
        # voice he talks to (Gemini) on top, the working brain doing the jobs
        # (Claude) under it — both named, both with their live state.
        mc = Clickable("sideCard")
        mc.setToolTip("brain settings")
        mc.clicked.connect(lambda: self.show_page("settings"))
        ml = self._model_lay = QVBoxLayout(mc)
        ml.setSpacing(8)
        h = QHBoxLayout()
        self.brains_title = mklabel("Brains", "sectionTitle")
        h.addWidget(self.brains_title)
        h.addStretch(1)
        h.addWidget(icon_label("chev", "chev"))
        ml.addLayout(h)
        self.voice_row = QWidget()
        h = QHBoxLayout(self.voice_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        self.voice_tile = icon_label("mic", "icoTile")
        h.addWidget(self.voice_tile)
        v = QVBoxLayout()
        v.setSpacing(2)
        nr = QHBoxLayout()
        nr.setSpacing(8)
        self.voice_name = mklabel("", "modelName")
        nr.addWidget(self.voice_name)
        self.voice_pill = mklabel("voice", "pillAcc")
        nr.addWidget(self.voice_pill)
        nr.addStretch(1)
        v.addLayout(nr)
        self.voice_sub = mklabel("talks with you · keeps an eye on Claude",
                                 "rowSub", wrap=True)
        v.addWidget(self.voice_sub)
        h.addLayout(v, 1)
        ml.addWidget(self.voice_row)
        self.voice_row.hide()        # shown once the engine says two brains
        h = QHBoxLayout()
        h.setSpacing(12)
        self.model_tile = icon_label("brain", "icoTile")
        h.addWidget(self.model_tile)
        v = QVBoxLayout()
        v.setSpacing(2)
        nr = QHBoxLayout()
        nr.setSpacing(8)
        self.model_name = mklabel("", "modelName")
        nr.addWidget(self.model_name)
        self.model_pill = mklabel("", "pillAcc")
        nr.addWidget(self.model_pill)
        nr.addStretch(1)
        v.addLayout(nr)
        self.model_sub = mklabel("", "rowSub", wrap=True)
        v.addWidget(self.model_sub)
        h.addLayout(v, 1)
        ml.addLayout(h)
        lay.addWidget(mc)

        # Plan usage: 5-hour + weekly meters (fed by the "plan" event)
        pc = QFrame()
        pc.setObjectName("card")
        pl = QVBoxLayout(pc)
        pl.setSpacing(6)
        pl.addWidget(mklabel("Claude usage", "sectionTitle"))
        self._plan_bars = {}
        for key, name in (("fh", "5-hour"), ("wk", "Weekly")):
            lab = mklabel(name + " —", "rowSub")
            bar = QProgressBar()
            bar.setObjectName("planbar")
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            pl.addWidget(lab)
            pl.addWidget(bar)
            self._plan_bars[key] = (lab, bar)
        lay.addWidget(pc)
        try:
            import json as _j
            _d = _j.load(open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), ".plan_usage.json"), encoding="utf-8"))
            for _k, _v, _n in (("fh", _d["five_hour"], "5-hour"),
                               ("wk", _d["seven_day"], "Weekly")):
                self._plan_bars[_k][0].setText(f"{_n} {int(_v)}%")
                self._plan_bars[_k][1].setValue(int(_v))
        except Exception:
            pass

        # Tools ⇄ Activity: the same slot. Activity takes it while he is in
        # the chat or while work runs; Tools the rest of the time.
        self.side_stack = QStackedWidget()
        tc = QFrame()
        tc.setObjectName("card")
        tl = self._tools_lay = QVBoxLayout(tc)
        tl.setSpacing(2)
        h = QHBoxLayout()
        h.setSpacing(8)
        h.addWidget(icon_label("tools", "icoAcc"))
        h.addWidget(mklabel("Tools", "paneltitle"))
        h.addStretch(1)
        act = QPushButton("Activity  ›")
        act.setObjectName("link")
        act.setCursor(Qt.PointingHandCursor)
        act.clicked.connect(lambda: self._show_side("activity", pin=True))
        h.addWidget(act)
        tl.addLayout(h)
        self.tool_rows = []
        for key, title, desc, _ex in CAPABILITIES[:4]:
            r = ListRow(key, title, desc.split(":")[0].split(",")[0].split(" — ")[0])
            r.setProperty("first", "true" if not self.tool_rows else "false")
            r.clicked.connect(lambda: self.show_page("tools"))
            tl.addWidget(r)
            self.tool_rows.append(r)
            self._scaled.append(r)
        tl.addStretch(1)
        self.side_stack.addWidget(tc)
        self.work_panel = WorkPanel(self)
        self.work_panel.tools_link.clicked.connect(
            lambda: self._show_side("tools", pin=True))
        self.side_stack.addWidget(self.work_panel)
        lay.addWidget(self.side_stack, 1)
        self.side_stack.setMinimumHeight(230)

        # System
        sc = QFrame()
        sc.setObjectName("card")
        sl2 = self._system_lay = QVBoxLayout(sc)
        sl2.setSpacing(0)
        h = QHBoxLayout()
        h.setSpacing(8)
        h.addWidget(icon_label("settings", "icoAcc"))
        h.addWidget(mklabel("System", "paneltitle"))
        h.addStretch(1)
        sl2.addLayout(h)
        sl2.addSpacing(4)
        self.sys_rows: dict[str, tuple] = {}
        for key, icon, title, sub, cb in (
                ("memory", "memory", "Memory", "Remembers your context",
                 lambda: self.show_page("memory")),
                ("voice", "volume", "Voice", "Talk naturally with Goat",
                 lambda: self.set_voice_opt("off" if self.cfg.get("voice", True) else "on")),
                ("mic", "mic", "Microphone", "Hears you, even over music",
                 self.toggle_mic),
                ("bubble", "desktop", "Desktop bubble", "Always by your side",
                 self.collapse)):
            r = Clickable("row")
            r.setProperty("first", "true" if not self.sys_rows else "false")
            rl = QHBoxLayout(r)
            tile = icon_label(icon, "icoTile")
            rl.addWidget(tile)
            v = QVBoxLayout()
            v.setSpacing(1)
            tr = QHBoxLayout()
            tr.setSpacing(8)
            tr.addWidget(mklabel(title, "rowTitle"))
            pill = mklabel("On", "pill")
            tr.addWidget(pill)
            tr.addStretch(1)
            v.addLayout(tr)
            v.addWidget(ElideLabel(sub, "rowSub"))
            rl.addLayout(v, 1)
            r.clicked.connect(cb)
            sl2.addWidget(r)
            self.sys_rows[key] = (r, rl, tile, pill)
        lay.addWidget(sc)

        self.today = QuoteCard()
        self.clock = self.today.clock
        self._themed.append(self.today)
        lay.addWidget(self.today)
        return side

    # =====================================================================
    # pages, greeting, recent, side cards
    # =====================================================================
    def show_page(self, key: str):
        if key not in self.pages:
            return
        if key != self._page:
            self._prev_page = self._page
        self._page = key
        self.stack.setCurrentWidget(self.pages[key])
        # v8: the mountains are a far quiet horizon on Home, so the presence
        # reads first (design.md); deeper on the reading pages.
        self.canvas.set_dim(0.5 if key == "home" else 0.62 if key == "chat" else 0.72)
        for k, b in self.nav.items():
            on = k == key
            b.setProperty("on", "true" if on else "false")
            repolish(b)
        self._paint_nav_icons()
        # One presence per screen: Home and Chat carry their own, the rail
        # card's small one shows on the other pages.
        self.presence_mini.setVisible(key not in ("home", "chat"))
        if key == "home":
            self._refresh_desk()
            QTimer.singleShot(0, self._refresh_ambient)
        if key == "files":
            self._refresh_files()
        elif key == "memory":
            self._refresh_memory()
        elif key == "chat":
            QTimer.singleShot(0, self._pin.snap)
        if not self._side_pinned or key == "chat":
            self._side_pinned = False
            self._auto_side()

    def _paint_nav_icons(self):
        t = self._theme()
        k = float(self.cfg.get("scale", 1.0))
        px = round(20 * k)
        for key, b in self.nav.items():
            on = key == self._page
            b.setIcon(glyph_icon(key, t["paper"] if on else t["dim"], px))
            b.setIconSize(QSize(px, px))

    def _show_side(self, which: str, pin: bool = False):
        self._side_pinned = pin
        self.side_stack.setCurrentIndex(1 if which == "activity" else 0)

    def _auto_side(self):
        busy = bool(self.goat and self.goat.busy) or bool(self.work_panel._t0)
        self.side_stack.setCurrentIndex(1 if (self._page == "chat" or busy) else 0)

    def _refresh_greeting(self):
        h = time.localtime().tm_hour
        ka = self.cfg.get("lang") == "ka"
        if ka:
            hi = ("დილა მშვიდობისა" if 5 <= h < 12 else
                  "გამარჯობა" if 12 <= h < 18 else "საღამო მშვიდობისა")
            name = "ბატონო"   # "Sir" — his order 2026-10-18
        else:
            hi = ("Up late" if h < 5 else "Good morning" if h < 12 else
                  "Good afternoon" if h < 18 else "Good evening")
            name = "Sir"      # his order 2026-10-18
            sub = ""
        # v8: one quiet line under the greeting, day · time · where he is.
        sub = time.strftime("%A, %d %B  \u00b7  %H:%M").replace(" 0", " ")
        city = self.cfg.get("city", "Essen")
        if city:
            sub += f"  \u00b7  {city}"
        self.hello.setText(f"{hi}, {name}.")
        self.hello_sub.setText(sub)

    def _transcript(self) -> list:
        """All exchanges on disk, oldest first, cached by mtime."""
        try:
            mt = os.path.getmtime(TRANSCRIPT)
        except OSError:
            return []
        if self._tx_cache[0] == mt:
            return self._tx_cache[1]
        rows = []
        try:
            with open(TRANSCRIPT, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        ex = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ex, dict) and (ex.get("user") or "").strip():
                        rows.append(ex)
        except OSError:
            return []
        self._tx_cache = (mt, rows)
        return rows

    def _refresh_recent(self):
        q = self.search.text().strip().lower() if hasattr(self, "search") else ""
        rows = list(reversed(self._transcript()))
        # v8: Home shows his desk; search results take its place while he
        # types in the search box, and give it back when the box is empty.
        self.recent_card.setVisible(bool(q))
        self.desk.setVisible(not q)
        if not q:
            self._refresh_desk()
            return
        if self._page != "home":
            self.show_page("home")
        rows = [r for r in rows if q in (r.get("user") or "").lower()
                or q in (r.get("reply") or "").lower()][:20]
        self.recent_title.setText(f"Results for “{self.search.text().strip()}”")
        while self.recent_box.count():
            it = self.recent_box.takeAt(0)
            w = it.widget()
            if w is not None:
                if w in self._scaled:
                    self._scaled.remove(w)
                w.setParent(None)
                w.deleteLater()
        if not rows:
            e = mklabel("Nothing matches." if q else
                        "No conversations yet — say the word.", "empty")
            e.setContentsMargins(12, 10, 12, 12)
            self.recent_box.addWidget(e)
            return
        k = float(self.cfg.get("scale", 1.0))
        for i, ex in enumerate(rows):
            user = _sentence_case(ex.get("user", ""))
            reply = " ".join((ex.get("reply") or "").split())
            r = ListRow(_topic_icon(user + " " + reply[:120]), user, reply,
                        _ago(ex.get("t", 0)), more=True)
            r.setProperty("first", "true" if i == 0 else "false")
            r.set_scale(k)
            r.clicked.connect(lambda e=ex: self._open_exchange(e))
            r.more_btn.clicked.connect(lambda _=False, e=ex, b=r.more_btn:
                                       self._exchange_menu(e, b))
            self.recent_box.addWidget(r)
            self._scaled.append(r)

    def _exchange_menu(self, ex: dict, anchor: QWidget):
        m = QMenu(self)
        m.addAction("Ask again", lambda: self._ask(ex.get("user", "")))
        m.addAction("Copy question", lambda: QApplication.clipboard().setText(
            ex.get("user", "")))
        m.addAction("Copy reply", lambda: QApplication.clipboard().setText(
            ex.get("reply", "")))
        m.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _open_exchange(self, ex: dict):
        """Show a past exchange in the conversation: scroll to it if it is on
        the page, otherwise lay it in above the loaded tail."""
        self.show_page("chat")
        want = " ".join((ex.get("user") or "").split())
        for i in range(self.col.count()):
            w = self.col.itemAt(i).widget()
            if (w is not None and w.objectName() in ("youOld", "youNow")
                    and " ".join(w.text().split()) == want):
                QTimer.singleShot(40, lambda w=w: self.scroll.ensureWidgetVisible(w, 0, 60))
                return
        you = self._make_line(want, "youOld")
        self.col.insertWidget(0, you, 0, Qt.AlignRight)
        reply = (ex.get("reply") or "").strip()
        if reply:
            self.col.insertWidget(1, self._make_line(reply, "replyOld"))
        QTimer.singleShot(40, lambda: self.scroll.verticalScrollBar().setValue(0))

    def _refresh_files(self):
        while self.files_box.count():
            it = self.files_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        inbox = []
        try:
            for name in os.listdir(INBOX):
                p = os.path.join(INBOX, name)
                if os.path.isfile(p):
                    inbox.append(p)
        except OSError:
            pass
        inbox.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        k = float(self.cfg.get("scale", 1.0))
        for title, paths in (("Made by Goat", self._made_files[::-1]),
                             ("Sent to Goat", self._sent_files[::-1]),
                             ("Inbox", inbox[:40])):
            card = QFrame()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(8, 12, 8, 8)
            cl.setSpacing(0)
            hd = mklabel(title, "sectionTitle")
            hd.setContentsMargins(10, 0, 0, 6)
            cl.addWidget(hd)
            if not paths:
                e = mklabel("Nothing yet.", "empty")
                e.setContentsMargins(10, 4, 0, 8)
                cl.addWidget(e)
            for i, p in enumerate(paths):
                ext = os.path.splitext(p)[1].lower()
                key = ("photo" if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
                       else "video" if ext in (".mp4", ".mov", ".mkv", ".webm")
                       else "code" if ext in (".py", ".js", ".ts", ".tsx", ".json", ".html",
                                              ".css", ".ps1", ".md")
                       else "doc")
                try:
                    when = _ago(os.path.getmtime(p))
                except OSError:
                    when = "missing"
                r = ListRow(key, os.path.basename(p), os.path.dirname(p), when)
                r.setProperty("first", "true" if i == 0 else "false")
                r.set_scale(k)
                r.clicked.connect(lambda p=p: self._reveal(p))
                cl.addWidget(r)
            self.files_box.addWidget(card)

    def _refresh_memory(self):
        try:
            with open(MEMORY_MD, encoding="utf-8") as f:
                # Qt's Markdown reads "<key>" / "<name>" as raw HTML tags,
                # swallows them and garbles every bullet after (seen
                # 2026-10-01: a page of empty dots). Entities render as
                # plain angle brackets.
                self.memory_text.setText(f.read().replace("<", "&lt;"))
        except OSError:
            self.memory_text.setText("memory.md isn't there yet.")

    def _refresh_side(self):
        """Active brain + System pills from the live config/engine."""
        self._refresh_brains_line()
        # Auto brain (2026-10-01): the card names the brain answering NOW
        # (Sonnet 5.5 for talk and simple tasks, his pick for code and
        # serious questions); the chip stays his pick — it is a selector.
        pick = self.cfg.get("work_model", "opus 5.5").strip()
        name = (self._work_model or pick).strip()
        self.model_name.setText(name.title() if name.islower() else name)
        eff = self.cfg.get("effort", "high")
        live = getattr(self, "_turn_brain", None)
        self.model_pill.setText(live[2] if live else eff)
        brains = getattr(self, "_brains", None)
        if brains and brains[0]:
            # Two brains: voice row on top, Claude's row under it.
            voice, work, note = brains
            self.voice_row.show()
            self.voice_name.setText(voice)
            g = self.goat
            if g is not None and getattr(g, "talk_busy", False):
                self.voice_pill.setText("thinking")
            elif g is not None and g.audio.is_tts_playing:
                self.voice_pill.setText("speaking")
            else:
                self.voice_pill.setText("voice")
            self.voice_sub.setText(getattr(self, "_voice_note", "")
                                   or "talks with you · keeps an eye on Claude")
            self.model_name.setText(work or name.title())
            bits = ["does the work"]
            wp = self.work_panel
            if wp._t0:
                bits.append("working " + wp._elapsed_str())
                if getattr(wp, "_last_step", ""):
                    bits.append(wp._last_step[:60])
            else:
                bits.append("free")
            if note:
                bits.append(note)
            if self._claude_out:
                bits = ["Out of usage" + (f" — back at {self._claude_reset}"
                                          if self._claude_reset else "")]
            self.model_sub.setText(" · ".join(bits))
        elif live:
            big = pick.title().replace(" ", "\u00a0")   # never split a name
            bits = [{"talk": "Auto · talking",
                     "task": "Auto · simple task",
                     "deep": "Auto · code & serious work"}.get(live[1], "Auto")]
            if live[1] != "deep":
                bits.append(f"{big} takes the hard work")
        else:
            bits = [f"Thinks at {eff}"]
        if not (brains and brains[0]):
            if self.cfg.get("hard_model") and not live:
                bits.append(f"hard tasks: {self.cfg['hard_model'].title()}")
            if self._claude_out:
                bits = ["Out of usage" + (f" — back at {self._claude_reset}"
                                          if self._claude_reset else "")]
            self.model_sub.setText(" · ".join(bits))
        self.chip_model.setText("  " + (pick.title() if pick.islower() else pick) + "  ▾")
        self.chip_effort.setText(f"  {eff}  ▾" if getattr(self, "_chips_compact", False)
                                 else f"  Thinking: {eff}  ▾")
        lang = next((l for l, c in LANGS.items() if c == self.cfg.get("lang")), "english")
        self.chip_lang.setText("  " + {"english": "EN", "ქართული": "KA",
                                       "ორივე": "EN + KA"}.get(lang, lang) + "  ▾")
        self._chips_full = {self.chip_model: self.chip_model.text(),
                            self.chip_lang: self.chip_lang.text()}
        if hasattr(self, "_chips_avail"):
            self._fit_chips(self._chips_avail)   # labels just reset to full
        muted = bool(self.goat and self.goat.mic_muted)
        states = {
            "memory": (os.path.exists(MEMORY_MD), "On", "Off"),
            "voice": (bool(self.cfg.get("voice", True)), "On", "Off"),
            "mic": (not muted, "Live", "Muted"),
            "bubble": (True, "Ctrl+B", ""),
        }
        for key, (on, yes, no) in states.items():
            pill = self.sys_rows[key][3]
            pill.setText(yes if on else no)
            pill.setProperty("off", "false" if on else "true")
            pill.setProperty("warn", "true" if (key == "mic" and not on) else "false")
            repolish(pill)
        self._paint_chip_icons()

    def _paint_chip_icons(self):
        t = self._theme()
        k = float(self.cfg.get("scale", 1.0))
        px = round(15 * k)
        for chip, key in ((self.chip_model, "brain"), (self.chip_effort, "bolt"),
                          (self.chip_work, "work"), (self.chip_web, "globe"),
                          (self.chip_screen, "eye"), (self.chip_file, "doc"),
                          (self.chip_lang, "lang")):
            chip.setIcon(glyph_icon(key, t["accent2"] if key in ("brain", "work")
                                    and (key == "brain" or self._work_mode) else t["dim"], px))
            chip.setIconSize(QSize(px, px))

    def _theme(self) -> dict:
        return {**THEMES.get(self._theme_name, THEMES["midnight"]),
                **(self.cfg.get("colors") or {})}

    # ---- composer actions ----
    def _prefill(self, text: str):
        if self._page not in ("home", "chat"):
            self.show_page("home")
        self.input.setText(text)
        self.input.setFocus()
        self.input.end(False)

    def _ask(self, text: str):
        text = (text or "").strip()
        if text and self.on_submit:
            self.on_submit(text)

    def _act_talk(self):
        self._set_work_mode(False)
        self.input.setFocus()

    def _act_work(self):
        self._set_work_mode(True)
        self.input.setFocus()

    def _act_screen(self):
        ka = self.cfg.get("lang") == "ka" or (
            self.cfg.get("lang") == "auto" and self._turnlang == "ka")
        self._ask("რა მაქვს ახლა ეკრანზე?" if ka else "What's on my screen right now?")

    def _toggle_work_mode(self):
        self._set_work_mode(not self._work_mode)
        self.input.setFocus()

    def _set_work_mode(self, on: bool):
        self._work_mode = bool(on)
        self.chip_work.setProperty("on", "true" if on else "false")
        repolish(self.chip_work)
        self.composer.setProperty("work", "true" if on else "false")
        repolish(self.composer)
        self.input.setPlaceholderText(
            "Give Goat a task — it works it end to end" if on
            else "Message Goat — or just say the word")
        self._paint_chip_icons()

    def _popup(self, anchor: QWidget, items: list, current: str, cb):
        m = QMenu(self)
        for label in items:
            a = m.addAction(("✓  " if label == current else "     ") + label)
            a.triggered.connect(lambda _=False, v=label: cb(v))
        m.exec(anchor.mapToGlobal(QPoint(0, -m.sizeHint().height() - 6)))

    def _menu_model(self):
        self._popup(self.chip_model, WORK_OPTS, self.cfg.get("work_model"),
                    self.set_work_opt)

    def _menu_effort(self):
        self._popup(self.chip_effort, EFFORT_OPTS, self.cfg.get("effort"),
                    self.set_effort_opt)

    def _menu_lang(self):
        cur = next((l for l, c in LANGS.items() if c == self.cfg.get("lang")), "")
        self._popup(self.chip_lang, list(LANGS), cur, self.set_lang_opt)

    def _open_path(self, path: str):
        try:
            if os.path.isdir(path) or os.path.exists(path):
                os.startfile(path)  # noqa: S606 — his own files, his own shell
            else:
                os.makedirs(path, exist_ok=True)
                os.startfile(path)
        except OSError:
            self._on_event("status", "couldn't open " + os.path.basename(path))

    def _reveal(self, path: str):
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except OSError:
            pass

    # ---- window controls ----
    # ---- collapsed bubble ----
    def collapse(self):
        """Put the window away and leave GOAT on screen as a dot."""
        if self.bubble.isVisible():
            return
        self._save_geometry()          # remember the box before it goes
        self.bubble.place(self.cfg.get("bubble"))
        self.bubble.set_state(self._statew)
        self.bubble.set_unread(False)
        self.bubble.show()
        self.bubble.raise_()
        pin_topmost(self.bubble)
        self._keeper.start()
        self._collapsing = True        # hide() fires changeEvent; don't recurse
        try:
            self.hide()
        finally:
            self._collapsing = False

    def expand(self):
        """Back to the full window, exactly where it was."""
        self.bubble.set_unread(False)
        self._keeper.stop()
        self.pop.hide()
        self.bubble.hide()
        self._collapsing = True
        try:
            if self.isMinimized():
                self.showNormal()
            else:
                self.show()
        finally:
            self._collapsing = False
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def toggle_bubble(self):
        self.expand() if self.bubble.isVisible() else self.collapse()

    def _sync_live(self):
        """Live window up while a work turn runs and the main window is off
        screen (aside, collapsed, minimized); down otherwise. Off entirely
        with cfg live_window = "off"."""
        live = self.live
        if not live.active:
            live.reveal(False)
            self._live_check.stop()
            return
        if (live._state == "working" and self.goat is not None
                and not self.goat.busy):
            live.done()           # a cut turn never sends work_done
        main_shown = self.isVisible() and not self.isMinimized()
        live.reveal(self.cfg.get("live_window", "on") != "off" and live.worked
                    and not main_shown)

    def _keep_bubble_up(self):
        if not self.bubble.isVisible():
            self._keeper.stop()
            return
        if self.bubble._press is None:      # never fight his drag
            self.bubble.place(self.cfg.get("bubble"))
        pin_topmost(self.bubble)
        if self.pop.isVisible():
            self.pop.follow(self.bubble.frameGeometry())
            pin_topmost(self.pop)

    def _save_bubble_pos(self):
        self.cfg["bubble"] = [self.bubble.x(), self.bubble.y()]
        save_ui_config(self.cfg)

    def _save_live_pos(self):
        if self.live.pos_saved is None:
            self.cfg.pop("live_pos", None)
        else:
            self.cfg["live_pos"] = list(self.live.pos_saved)
        save_ui_config(self.cfg)

    def changeEvent(self, ev):
        """Minimizing by any route — taskbar, Win+D, shake — collapses too.

        Qt reports the state change after the fact, so the collapse is queued
        to the next event-loop turn rather than run inside the handler.
        """
        super().changeEvent(ev)
        if ev.type() == QEvent.WindowStateChange:
            if hasattr(self, "b_full"):
                self.b_full.setText(IC["restore"] if (self.isFullScreen()
                                                      or self.isMaximized()) else IC["max"])
            if not self.isFullScreen():
                QTimer.singleShot(0, self._native_frame)   # fullscreen exit resets it
        if (ev.type() == QEvent.WindowStateChange and self.isMinimized()
                and not self._collapsing and not self.bubble.isVisible()):
            QTimer.singleShot(0, self.collapse)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _show_cmd(self):
        self.input.setFocus()
        self.input.selectAll()

    def _show_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _escape(self):
        if self.search.hasFocus():
            if self.search.text():
                self.search.clear()
            else:
                self.search.clearFocus()
        elif self.input.hasFocus() and self.input.text():
            self.input.clear()
        elif self.input.hasFocus():
            self.input.clearFocus()
        elif self.goat and self.goat.tts.speaking():
            # Keyboard barge-in: esc shuts GOAT up mid-sentence.
            self.goat.tts.cancel()
            self._on_event("status", "quieted")
        elif self._page not in ("home", "chat"):
            self.show_page(self._prev_page if self._prev_page != self._page else "home")
        elif self.isFullScreen():
            self.showNormal()

    def toggle_mic(self):
        """Composer mic button / system card / ctrl+m — one switch."""
        if not self.goat:
            return
        self.set_mic_opt("live" if self.goat.mic_muted else "muted")

    def _refresh_mic_btn(self):
        muted = bool(self.goat and self.goat.mic_muted)
        self.mic_btn.setText(IC["mute"] if muted else IC["mic"])
        self.mic_btn.setProperty("muted", "true" if muted else "false")
        repolish(self.mic_btn)
        if hasattr(self, "sys_rows"):
            self._refresh_side()

    # ---- theme / appearance ----
    def apply_theme(self, name: str):
        base = THEMES.get(name) or THEMES["midnight"]
        # Live per-part color overrides GOAT set (e.g. text→blue) ride on top
        # of whatever theme is active.
        t = {**base, **(self.cfg.get("colors") or {})}
        self._theme_name = name
        self.cfg["theme"] = name
        self.setStyleSheet(build_style(t, TEXT_SIZES[self.cfg["text"]],
                                       float(self.cfg.get("scale", 1.0))))
        self.canvas.set_theme(t)
        self.presence_mini.set_theme(t)
        self.work_panel.set_theme(t)
        for w in self._themed:
            w.set_theme(t)
        if hasattr(self, "bubble"):
            self.bubble.set_theme(t)
            self.bubble.set_scale(float(self.cfg.get("scale", 1.0)))
        if hasattr(self, "pop"):
            self.pop.set_theme(t)
            self.pop.set_scale(float(self.cfg.get("scale", 1.0)))
        if hasattr(self, "live"):
            self.live.set_theme(t)
        self.panel.set_theme(t)
        self.panel.refresh()
        self._apply_metrics()
        self._paint_nav_icons()
        if hasattr(self, "sys_rows"):
            self._refresh_side()
        if hasattr(self, "hello"):
            self._refresh_greeting()

    def _apply_metrics(self):
        """Scale the page's BONES, not just its type (v6 lesson): rails,
        margins, cards and marks all follow the global zoom."""
        if not hasattr(self, "_side_lay"):
            return  # apply_theme also runs mid-construction; nothing to move
        k = float(self.cfg.get("scale", 1.0))

        def m(box):
            return tuple(max(0, round(v * k)) for v in box)

        # Rails grow with the zoom but never eat the centre.
        rail_w = min(round(RAIL_W * k), int(self.width() * 0.26))
        side_w = min(round(SIDE_W * k), int(self.width() * 0.30))
        self.rail.setFixedWidth(rail_w)
        self.side.setFixedWidth(side_w)
        self.canvas.set_rails(rail_w, side_w)
        self._rail_lay.setContentsMargins(*m(RAIL_MARGIN))
        self._center_lay.setContentsMargins(*m(CENTER_MARGIN))
        self._side_lay.setContentsMargins(*m(SIDE_MARGIN))
        self._presence_lay.setContentsMargins(*m((14, 12, 14, 12)))
        self._composer_lay.setContentsMargins(*m((10, 6, 7, 6)))
        self._search_lay.setContentsMargins(*m((14, 0, 10, 0)))
        self._model_lay.setContentsMargins(*m((16, 14, 16, 14)))
        self._tools_lay.setContentsMargins(*m((14, 12, 10, 8)))
        self._system_lay.setContentsMargins(*m((14, 12, 10, 8)))
        self.today.layout().setContentsMargins(*m((18, 14, 18, 14)))
        self.work_panel.layout().setContentsMargins(*m(WORK_MARGIN))
        self.model_tile.setFixedSize(round(44 * k), round(44 * k))
        self.voice_tile.setFixedSize(round(44 * k), round(44 * k))
        for r, rl, tile, _pill in self.sys_rows.values():
            rl.setContentsMargins(*m((4, 7, 4, 7)))
            rl.setSpacing(round(12 * k))
            tile.setFixedSize(round(34 * k), round(34 * k))
        for w in self._scaled:
            w.set_scale(k)
        self.presence_mini.setFixedHeight(round(34 * k))
        self.today.setMinimumHeight(round(120 * k))
        # Docked presence: ring radius = 0.3 x height = 28 px at 1x (design
        # mockup B), so the conversation keeps the room.
        self.presence.setFixedHeight(round(94 * k))
        # Home presence: r = 0.3 x height = 50 px at 1x on a 900 px window,
        # smaller on short windows so the desk still shows.
        self.home_presence.setFixedHeight(
            round(min(166 * k, max(110 * k, self.height() * 0.185))))
        self._desk_lay.setSpacing(round(40 * k))
        self._cards_lay.setSpacing(round(12 * k))
        self.side_stack.setMinimumHeight(round(230 * k))
        self._hero_gap.setFixedHeight(round(max(16, min(64, self.height() * 0.05)) * k))
        self._titlebar_h = round(TITLEBAR_H * k)
        self._fit_side()
        self._fit_chips(self.width() - rail_w - side_w
                        - round((CENTER_MARGIN[0] + CENTER_MARGIN[2]) * k))
        self._apply_measure()

    def _fit_side(self):
        """On a short window the Today card gives its room to Activity."""
        k = float(self.cfg.get("scale", 1.0))
        self.today.setVisible(self.height() >= 820 * k)

    def _apply_measure(self):
        """Hold the transcript to a readable measure, centred in the column —
        extra width becomes margin on both sides, never longer lines."""
        k = float(self.cfg.get("scale", 1.0))
        left, top, right, bottom = (round(v * k) for v in READ_MARGIN)
        avail = self.scroll.viewport().width() - left - right
        if avail > 0:
            cap = round(TEXT_SIZES[self.cfg["text"]] * k * 0.52 * READ_MEASURE_CH)
            if avail > cap:
                extra = avail - cap
                left += extra // 2
                right += extra - extra // 2
        self.col.setContentsMargins(left, top, right, bottom)
        for i in range(self.col.count()):
            w = self.col.itemAt(i).widget()
            if isinstance(w, ChatBubble):
                w.set_scale(k)

    def cycle_effort(self):
        """Ctrl+E — step the work lane's thinking depth up the ladder and
        wrap. Same dial as the settings row; the engine reopens its session."""
        i = EFFORT_OPTS.index(self.cfg["effort"]) if self.cfg["effort"] in EFFORT_OPTS else len(EFFORT_OPTS) - 1
        self.set_effort_opt(EFFORT_OPTS[(i + 1) % len(EFFORT_OPTS)])
        self.work_panel.set_effort(self.cfg["effort"])
        self._on_event("status", f"thinking: {self.cfg['effort']}")
        self.panel.refresh()

    def cycle_lang(self):
        """Ctrl+L — english → ქართული → ორივე (bilingual) and round again."""
        codes = list(LANGS.values())
        i = codes.index(self.cfg["lang"]) if self.cfg["lang"] in codes else 0
        nxt = codes[(i + 1) % len(codes)]
        label = next(l for l, c in LANGS.items() if c == nxt)
        self.set_lang_opt(label)
        self._on_event("status", f"language: {label}")
        self.panel.refresh()

    def cycle_theme(self):
        i = THEME_ORDER.index(self._theme_name) if self._theme_name in THEME_ORDER else 0
        name = THEME_ORDER[(i + 1) % len(THEME_ORDER)]
        self.apply_theme(name)
        save_ui_config(self.cfg)

    # ---- settings ----
    def toggle_settings(self):
        if self._page == "settings":
            self.show_page(self._prev_page if self._prev_page != "settings" else "home")
        else:
            self.show_page("settings")

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply_metrics()   # rails, the reading column, the Today card
        self._debounce_geom_save()

    def _save(self):
        save_ui_config(self.cfg)
        self.panel.refresh()
        if hasattr(self, "sys_rows"):
            self._refresh_side()

    def set_theme_opt(self, name: str):
        self.apply_theme(name)
        save_ui_config(self.cfg)

    def set_text_opt(self, size: str):
        self.cfg["text"] = size
        self.apply_theme(self._theme_name)  # rebuilds the stylesheet
        save_ui_config(self.cfg)

    def set_scale_opt(self, label: str):
        """Settings preset click ('150%')."""
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
        """Live global UI zoom — from settings OR from GOAT itself (voice:
        'make your interface 50% bigger'). Clamped, applied, saved."""
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

    def set_character_opt(self, name: str):
        """Who GOAT sounds like. The sentence already in the air belongs to
        the old voice, so it gets cut rather than finished in two voices."""
        import tts_edge
        if not tts_edge.set_character(name):
            return
        self.cfg["character"] = name
        if self.goat:
            self.goat.tts.cancel()
        self._on_event("status", f"voice character: {name}")
        self._save()

    def set_wake_opt(self, opt: str):
        self.cfg["wake"] = opt == "on"
        if self.goat:
            self.goat.wake_enabled = self.cfg["wake"]
        self._save()

    def set_voice_lock_opt(self, opt: str):
        """Voice lock (voice_id.py): on = only his voice gets through; learn
        = the next ~20s he speaks become his voiceprint (then it locks)."""
        vid = getattr(self.goat, "vid", None) if self.goat else None
        if vid is None:
            self._on_event("status", "voice lock unavailable — voiceid models missing")
        elif opt == "learn" or (opt == "on" and not vid.prints):
            vid.start_enroll(None)
            self._on_event("status", "learning your voice — talk or read aloud for ~20s")
        else:
            vid.set_lock(opt == "on")
            if opt == "on":
                self.goat._voice_ok_t = time.monotonic()
            self._on_event("status", "voice lock on — only your voice"
                           if opt == "on" else "voice lock off — hearing everyone")
        self._save()

    def set_mic_opt(self, opt: str):
        if self.goat:
            self.goat.mic_muted = opt == "muted"
            self._on_event("status", "mic muted" if self.goat.mic_muted
                           else "listening")
        self._refresh_mic_btn()
        self._save()

    def set_work_opt(self, name: str):
        self.cfg["work_model"] = name if name in WORK_OPTS else "opus 5.5"
        if self.goat:
            self.goat.set_work_model(self.cfg["work_model"])
        self._save()

    def set_hard_opt(self, name: str):
        self.cfg["hard_model"] = name if name in WORK_OPTS else "opus 5.5"
        if self.goat:
            self.goat.set_hard_model(self.cfg["hard_model"])
        self._save()

    def set_effort_opt(self, level: str):
        """Thinking depth on the work lane. The engine can't re-effort a live
        session, so it reopens the work client (same conversation) itself."""
        if level not in EFFORT_OPTS:
            level = "max"
        self.cfg["effort"] = level
        if self.goat:
            self.goat.set_effort(level)
        self.work_panel.set_effort(level)
        self._save()

    def set_lang_opt(self, label: str):
        code = LANGS.get(label, "en")
        if code == self.cfg.get("lang"):
            return
        self.cfg["lang"] = code
        if self.goat:
            self.goat.set_language(code)
        self._save()
        self._refresh_greeting()

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
        import tts_edge     # already loaded by the engine import by now
        tts_edge.set_character(self.cfg.get("character", "goat"))
        goat.wake_enabled = self.cfg["wake"]
        # Before the engine thread starts: run() applies voice + hearing
        # model + persona note itself from this attribute.
        goat.language = self.cfg["lang"]
        goat.work_model = self.cfg.get("work_model", "opus 5.5")
        goat.hard_model = self.cfg.get("hard_model", "opus 5.5")
        goat.effort = self.cfg.get("effort", "high")
        if self.cfg.get("lang") == "auto":
            goat.turn_lang = self.cfg.get("last_lang", "en")
            self._turnlang = goat.turn_lang
        self.work_panel.set_effort(self.cfg.get("effort", "max"))
        self.panel.refresh()
        self._refresh_mic_btn()

    # ---- session actions ----
    def new_chat(self):
        """Fresh conversation in the running app — see GoatApp.new_chat.
        The page is cleared too: old lines on screen next to a brain that
        doesn't have them is what read as "the memory is cleared"."""
        if self.goat is None or not hasattr(self.goat, "new_chat"):
            # Engine not up yet: the old route, through the restart gate.
            try:
                os.remove(os.path.join(GOAT_ROOT, ".goat-session-py"))
            except OSError:
                pass
            self.restart_goat()
            return
        self._clear_page()
        self.epigraph = QLabel("New chat. Say the word.")
        self.epigraph.setObjectName("epigraph")
        self.epigraph.setAlignment(Qt.AlignHCenter)
        self.epigraph.setContentsMargins(0, 90, 0, 0)
        self.col.insertWidget(0, self.epigraph)
        self.show_page("chat")
        self.goat.new_chat()

    def restart_goat(self):
        self._on_event("status", "restarting…")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", os.path.join(GOAT_ROOT, "python", "restart-goat.ps1")],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def mousePressEvent(self, ev):
        # Fallback drag only — on Windows the native hit-test below turns the
        # top band into a real caption (OS handles move, Aero Snap, double-
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
        # Top band, but anything you can press or type in keeps its clicks —
        # checked up the parent chain, since a card's label is what the
        # cursor actually lands on.
        if y < self._titlebar_h:
            child = self.childAt(p)
            while child is not None and child is not self:
                if isinstance(child, (QAbstractButton, QLineEdit, Clickable)):
                    return None
                child = child.parentWidget()
            return 2          # HTCAPTION — drag/snap/double-click-maximize
        return None           # HTCLIENT (default)

    # Hit-testing alone isn't enough for Snap: Windows only snaps (Win+arrow,
    # drag-to-edge, Snap Layouts, half-and-half with Chrome) a window whose
    # STYLE says it has a sizing frame and a maximize box. Qt's frameless
    # window is a bare WS_POPUP, so Windows refused (2026-09-26: "I can't
    # have half GOAT and half Chrome"). Add the styles back; WM_NCCALCSIZE
    # below keeps the drawn frame at zero so the look doesn't change.
    _SNAP_STYLE = (0x00C00000     # WS_CAPTION
                   | 0x00040000   # WS_THICKFRAME
                   | 0x00020000   # WS_MINIMIZEBOX
                   | 0x00010000   # WS_MAXIMIZEBOX
                   | 0x00080000)  # WS_SYSMENU

    def _native_frame(self):
        if sys.platform != "win32" or self.isFullScreen():
            return
        try:
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            st = u.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF   # GWL_STYLE
            want = (st | self._SNAP_STYLE) & ~0x80000000    # drop WS_POPUP
            if want == st:
                return
            u.SetWindowLongW(hwnd, -16, ctypes.c_long(want))
            # SWP_FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER | NOACTIVATE
            u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x20 | 0x2 | 0x1 | 0x4 | 0x10)
        except Exception:  # noqa: BLE001 — cosmetic; never break the window
            pass

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._native_frame)

    def _nc_calc_size(self, msg):
        """WM_NCCALCSIZE: whole window is client area (no drawn frame).
        Maximized, Windows oversizes the window by the frame width — clamp
        the client to the monitor's work area so nothing hangs off-screen."""
        if not msg.wParam:
            return None
        if self.isMaximized():
            rc = ctypes.wintypes.RECT.from_address(msg.lParam)

            class _MI(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong),
                            ("rcMonitor", ctypes.wintypes.RECT),
                            ("rcWork", ctypes.wintypes.RECT),
                            ("dwFlags", ctypes.c_ulong)]
            mi = _MI()
            mi.cbSize = ctypes.sizeof(_MI)
            u = ctypes.windll.user32
            mon = u.MonitorFromWindow(ctypes.wintypes.HWND(msg.hWnd), 2)
            if mon and u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
                rc.left, rc.top = mi.rcWork.left, mi.rcWork.top
                rc.right, rc.bottom = mi.rcWork.right, mi.rcWork.bottom
        return 0

    def nativeEvent(self, eventType, message):
        if eventType == "windows_generic_MSG" and not self.isFullScreen():
            try:
                addr = int(message)
                if not addr:
                    return super().nativeEvent(eventType, message)
                msg = ctypes.wintypes.MSG.from_address(addr)
                if msg.message == 0x0083:  # WM_NCCALCSIZE
                    r = self._nc_calc_size(msg)
                    if r is not None:
                        return True, r
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
            if self.bubble.isVisible():
                # Collapsed: say it beside the dot, word for word with the voice.
                self.pop.show_text(text, self.bubble.frameGeometry())

    # How the presence dot reads each state: green = here and listening,
    # blue = busy (thinking, speaking, working), grey = muted/asleep, red =
    # something he should know about.
    MOODS = {"speaking": "busy", "thinking": "busy", "working": "busy",
             "booting": "off"}

    def hud_tick(self, mic_level: float, state: str, _listening: bool):
        # Minimized, nothing on screen can move — skip the animations
        # entirely (Qt still counts a minimized window as visible).
        shown = self.isVisible() and not self.isMinimized()
        if getattr(self, "_brains", None) and state != getattr(self, "_voice_state", ""):
            # Brains card: what the voice is doing right now.
            self._voice_state = state
            self.voice_pill.setText({"speaking": "speaking", "thinking": "thinking",
                                     "listening": "listening"}.get(state, "voice"))
        if shown:
            # The presence reads three things: the voice's state, Claude's
            # work turn (its orbiting light) and asleep/error from the words.
            muted_ = bool(self.goat and self.goat.mic_muted)
            pstate = state
            if muted_ or (self.goat and getattr(self.goat, "_asleep", False)):
                pstate = "asleep"
            w_ = self.stateword.text()
            if "crash" in w_ or "failed" in w_ or " down" in w_:
                pstate = "error"
            work = bool(self.goat and self.goat.busy)
            self.presence_mini.tick(mic_level, pstate, work)
            self.presence.tick(mic_level, pstate, work)
            self.home_presence.tick(mic_level, pstate, work)
            if getattr(self, "_brains_sig", None) != (work, self._claude_out):
                self._refresh_brains_line()
        # Status lines ("calibrating…", "reconnected") hold the word for a
        # few seconds — without the hold, the 33ms audio-state ticker stomps
        # every status within one frame and none of them are ever seen.
        if (state != self._statew and self._statew != "booting"
                and time.time() >= self._status_hold):
            self._statew = state
            self.stateword.setText(state)
        if self.bubble.isVisible():
            self.bubble.set_level(mic_level)
            self.bubble.set_state(state)
        if self._page in ("chat", "home"):
            cap = self.stateword.text()
            lab = self.presence_caption if self._page == "chat" else self.home_state
            if cap != lab.text():
                lab.setText(cap)
        muted = bool(self.goat and self.goat.mic_muted)
        word = self.stateword.text()
        mood = ("bad" if (self._claude_out or "out of usage" in word
                          or "crash" in word or "down" in word)
                else "off" if muted else self.MOODS.get(state, "ok"))
        if mood != self._mood:
            self._mood = mood
            self.statedot.setProperty("mood", mood)
            repolish(self.statedot)
        hot = state == "listening" and not muted
        if hot != self._mic_hot:
            self._mic_hot = hot
            self.mic_btn.setProperty("hot", "true" if hot else "false")
            repolish(self.mic_btn)
        up = int(time.time() - self._t0)
        mic = "mic muted" if muted else "mic live"
        # Claude usage meter: OUT (+reset) when spent, else session tokens.
        if self._claude_out:
            claude = " · claude OUT" + (
                f" · resets {self._claude_reset}" if self._claude_reset else "")
        elif self._usage:
            claude = f" · {self._usage}"
        else:
            claude = ""
        if self._plan and not self._claude_out:
            claude += f" · {self._plan}"
        # In bilingual mode the ear can flip per sentence — show which
        # language the last turn landed in, so a mishearing is visible.
        heard = (f" · hearing {self._turnlang}"
                 if self._turnlang and self.cfg.get("lang") == "auto" else "")
        line = f"{mic} · up {up // 3600}:{up // 60 % 60:02d}{heard}{claude}"
        if line != self.footer.text():
            self.footer.setText(line)
        clock = time.strftime("%H:%M  ·  %A, %d %B")
        if clock != self.clock.text():
            self.clock.setText(clock)

    # ---- input ----
    def _submit(self):
        if self._work_mode:
            self._submit_work(False)
            return
        text = self.input.text().strip()
        if text and self.on_submit:
            self.on_submit(text)
            self.input.clear()

    def _submit_work(self, hard: bool = False):
        """Dispatch the typed order to the working brain, or the hard-task
        brain when hard=True."""
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
            else:
                continue
            repolish(wdg)

    def _load_transcript_tail(self, keep: int = 8) -> bool:
        """Old exchanges from workspace/transcript.jsonl, painted dimmed.
        Returns True when anything was restored."""
        try:
            with open(TRANSCRIPT, encoding="utf-8") as f:
                lines = f.readlines()[-keep:]
        except OSError:
            return False
        pairs = []
        for line in lines:
            try:
                ex = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ex.get("new_chat"):
                pairs = []      # nothing from before his last New chat
                continue
            user = (ex.get("user") or "").strip()
            reply = (ex.get("reply") or "").strip()
            if user:
                pairs.append((user, reply))
        for user, reply in pairs:
            self._add_line(_sentence_case(user), "youOld")
            if reply:
                self._add_line(reply, "replyOld")
        return bool(pairs)

    # The page kept every line of every exchange forever. Two costs, both
    # real in a long session: _dim_previous() re-polishes the whole column on
    # each new line, and the widget count only ever grows. Cap it — his
    # scrollback is the on-disk transcript (and Search), not the live layout.
    PAGE_MAX = 240

    def _trim_page(self):
        extra = self.col.count() - 1 - self.PAGE_MAX
        if extra <= 0:
            return
        stale = []
        for _ in range(extra):
            item = self.col.itemAt(0)
            if item is None:
                break
            w = item.widget()
            self.col.removeItem(item)
            if w is not None:
                stale.append(w)
                w.setParent(None)
                w.deleteLater()
        # Any Python name still pointing at a widget Qt just deleted is a
        # loaded gun: touching it later is an access violation inside
        # shiboken, not a Python exception — exactly the class of crash
        # logged at 01:56:55 on 2026-09-15. Drop the references here.
        for attr in ("_you_label", "_reply_label", "epigraph"):
            if getattr(self, attr, None) in stale:
                setattr(self, attr, None)

    def _clear_page(self):
        """Empty the conversation column (New chat). The stretch at the end
        stays; every dropped widget's Python name is cleared, as in
        _trim_page, so nothing can touch a deleted label later."""
        while self.col.count() > 1:
            item = self.col.itemAt(0)
            w = item.widget()
            self.col.removeItem(item)
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for attr in ("_you_label", "_reply_label", "epigraph"):
            setattr(self, attr, None)

    def _make_line(self, text: str, name: str) -> QLabel:
        if name in ("youNow", "youOld"):
            lbl = ChatBubble(text)
            lbl.set_scale(float(self.cfg.get("scale", 1.0)))
        else:
            lbl = PageLabel(text)
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if name == "notice":
                k = float(self.cfg.get("scale", 1.0))
                lbl.setContentsMargins(round(14 * k), round(9 * k),
                                       round(14 * k), round(10 * k))
        # Conversation is plain text. On AutoText, a reply that explains
        # HTML ("wrap it in <b>…</b>") was rendered AS HTML and the code
        # vanished from the page.
        lbl.setTextFormat(Qt.PlainText)
        lbl.setObjectName(name)
        return lbl

    def _add_line(self, text: str, name: str) -> QLabel:
        lbl = self._make_line(text, name)
        align = Qt.AlignRight if isinstance(lbl, ChatBubble) else Qt.Alignment()
        self.col.insertWidget(self.col.count() - 1, lbl, 0, align)
        self._trim_page()
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
            420, 280, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lbl.setToolTip(path)
        self.col.insertWidget(self.col.count() - 1, lbl, 0, Qt.AlignRight)
        QTimer.singleShot(30, self._scroll_down)

    def _scroll_down(self):
        self._pin.snap()  # no-op while he's scrolled up reading

    # ---- events from GoatApp (any thread) ----
    def post_event(self, kind: str, data: str):
        self.event_sig.emit(kind, str(data))

    def _on_event(self, kind: str, data: str):
        if kind == "status":
            self._statew = data.lower()
            self.stateword.setText(self._statew)
            self._status_hold = time.time() + 4.0
        elif kind == "talkmodel":
            self._model = data
        elif kind == "model":
            self._work_model = data  # the brain actually answering
            self.work_panel.set_model(data)
            self._refresh_side()
        elif kind == "brain":
            # "sonnet 5.5|talk|low" — the auto brain's pick for this turn.
            parts = (data.split("|") + ["", "", ""])[:3]
            self._turn_brain = parts
            self.work_panel.set_turn_effort(parts[2])
            if self._reply_label is not None and not getattr(self, "_brains", None):
                self._reply_label.setToolTip(f"answered by {parts[0]}")
            self._refresh_side()
        elif kind == "brains":
            # Two brains: "voice|work|note" (voice empty = one-brain mode).
            voice, work, note, vnote = (data.split("|") + ["", "", "", ""])[:4]
            voice = voice_display(voice)
            self._brains = (voice, work.strip(), note.strip())
            self._voice_note = vnote.strip()
            self.work_panel.set_brains(voice, work.strip())
            self._refresh_side()
        elif kind == "talkbrain":
            # The voice model that answered this turn (a hedge may win).
            b = getattr(self, "_brains", None)
            if b and b[0] != voice_display(data):
                self._brains = (voice_display(data), b[1], b[2])
                self.work_panel.set_brains(self._brains[0], b[1])
                self._refresh_side()
            if self._reply_label is not None:
                self._reply_label.setToolTip(f"spoken by {data}")
        elif kind == "voice_note":
            # The voice speaking up on its own (Claude's job finished, stuck,
            # a progress word): a fresh line in the chat, not his last answer.
            self._dim_previous()
            self._reply_label = self._add_line("", "replyNow")
            self._reply_label.setToolTip(f"{data} · news about Claude's job")
        elif kind == "work_voice":
            # What the voice told him about the job, in the Activity log too:
            # the two brains' hand-offs read top to bottom.
            self.work_panel.voice(data)
        elif kind == "claude":
            # Claude usage meter: "ok" or "out|HH:MM".
            if data == "ok":
                self._claude_out = False
            elif data.startswith("out"):
                self._claude_out = True
                _, _, reset = data.partition("|")
                self._claude_reset = reset.strip()
            self._refresh_side()
        elif kind == "ui_scale":
            # GOAT resizing its own interface. Payload: "<factor>" absolute,
            # or "*<factor>" relative (e.g. "*1.5" = 50% bigger).
            try:
                if data.startswith("*"):
                    self.set_ui_scale(float(data[1:]), relative=True)
                else:
                    self.set_ui_scale(float(data))
            except (ValueError, TypeError):
                pass
        elif kind == "ui_character":
            self.set_character_opt(data.strip())
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
        elif kind == "plan":
            try:
                fh, wk, rs = data.split("|")
                self._plan = f"plan {fh}% (resets {rs}) · week {wk}%"
                self._plan_fh = int(float(fh))
                for k, v, nm, tail in (("fh", fh, "5-hour", f" · resets {rs}"),
                                       ("wk", wk, "Weekly", "")):
                    lab, bar = self._plan_bars[k]
                    n = int(float(v))
                    lab.setText(f"{nm} {n}%{tail}")
                    bar.setValue(n)
                    bar.setProperty("hot", "true" if n >= 85 else "false")
                    repolish(bar)
            except ValueError:
                pass
        elif kind == "limit":
            # out of quota — say it on the page, and hold the state word long
            # enough to actually register
            self._add_line(data, "notice")
            self._statew = "out of usage"
            self.stateword.setText("out of usage")
            self._status_hold = time.time() + 15.0
        elif kind == "you":
            if self.epigraph is not None:
                self.epigraph.deleteLater()
                self.epigraph = None
            # He spoke: the home page hands over to the conversation. Any
            # other page he chose on purpose stays put.
            if self._page == "home" and not self.search.hasFocus():
                self.show_page("chat")
            self._dim_previous()
            self._reply_label = None
            self._pin.follow = True  # he spoke — bring him to the reply
            self._pin.snap()
            self._you_label = self._add_line(_sentence_case(data), "youNow")
            self.pop.hide()  # last reply's card must not stand in for the next one
            spacer = self._add_line("", "replyNow")
            spacer.setFixedHeight(2)
        elif kind == "files":
            # thumbnails of what he just sent, right under his line
            for path in data.split("\n"):
                path = path.strip()
                if path:
                    self._sent_files.append(path)
                self._add_thumbnail(path)
        elif kind == "delta":
            if self.bubble.isVisible():
                self.bubble.set_unread(True)  # he is collapsed; mark the dot
            # Text is NOT shown from the model's stream — it would race far
            # ahead of the voice. The label is created here; its words are
            # revealed by update_spoken(), synced to actual playback.
            if self._reply_label is None:
                self._reply_label = self._add_line("", "replyNow")
                tb = getattr(self, "_turn_brain", None)
                if tb:
                    self._reply_label.setToolTip(f"answered by {tb[0]}")
        elif kind == "reply_drop":
            # The brain answered [[quiet]] (side talk, see side_talk.py):
            # take away the reply label it opened, if nothing reached it.
            lbl = self._reply_label
            if lbl is not None and not lbl.text().strip():
                self._reply_label = None
                lbl.hide()
                lbl.deleteLater()
        elif kind == "tool":
            # Keep _reply_label — the whole turn reveals into ONE label
            # (spoken_text() is cumulative; a second label would duplicate).
            self._add_line(f"·  {data.lower()}", "toolLine")
        elif kind == "work_start":
            model, _, task = data.partition("|")
            self.work_panel.start(model.strip(), task)
            self.live.start(model.strip(), task)
            self._live_check.start()
            if not self._side_pinned:
                self._show_side("activity")
        elif kind == "work_tool" or kind == "work_step":
            self.work_panel.step(data)
            self.live.step(data)
            self._sync_live()
            if getattr(self, "_brains", None):
                self._refresh_side()     # the Brains card shows Claude's step
        elif kind == "work_shot":
            # The screenshot the brain just looked at (screen_tools).
            self.live.set_shot(data)
        elif kind == "work_text":
            self.work_panel.text(data)
        elif kind == "turnlang":
            # Remember it: in bilingual mode the next boot resumes in the
            # language the conversation was actually in.
            self._turnlang = data
            if data in ("en", "ka") and self.cfg.get("last_lang") != data:
                self.cfg["last_lang"] = data
                self._save()
        elif kind == "work_ctx":
            used, _, trim = data.partition("|")
            try:
                self.work_panel.context(int(used), int(trim))
            except ValueError:
                pass
        elif kind == "work_think":
            self.work_panel.think(data)
            self.live.thinking(data)
        elif kind == "effort":
            self.cfg["effort"] = data if data in EFFORT_OPTS else "max"
            self.work_panel.set_effort(self.cfg["effort"])
            self.panel.refresh()
            self._save()
        elif kind == "work_add":
            self.work_panel.add(data)
        elif kind == "work_files":
            paths = [p.strip() for p in data.split("\n") if p.strip()]
            self._made_files.extend(paths)
            self.work_panel.files(paths)
        elif kind == "work_done":
            self.work_panel.done()
            self.live.done()
            self._recent_timer.start(1500)
            if not self._side_pinned:
                QTimer.singleShot(4000, self._auto_side)
        elif kind == "work_fail":
            self.work_panel.fail(data)
            self.live.done(failed=data)
        elif kind == "turn_done":
            # Do NOT drop _reply_label here: the model finishes generating
            # seconds before the voice finishes speaking (often before it
            # even starts), and the word reveal keeps landing in this label
            # until the next "you" resets it. Nulling it here is what made
            # the screen stay permanently blank.
            self.stateword.setText("listening")
            self._recent_timer.start(1500)


def main():
    # Hard-crash capture (2026-09-15). GOAT died at 01:56:55 that morning with
    # no trace of why: Windows logged "python.exe faulting module
    # shiboken6.abi3.dll, exception 0xc0000005" (an access violation inside
    # PySide6's binding layer), goat-app.log was EMPTY, and no Python
    # traceback existed anywhere — a native crash never raises, so nothing in
    # the app can log it. faulthandler installs an OS-level handler that dumps
    # the Python stack of every thread at the moment of the fault, which turns
    # the next occurrence from "it crashed" into a file and a line number.
    # Kept permanently: it costs nothing until something goes very wrong.
    # The launcher's `>>` redirect makes stdout strict cp1252: one arrow or
    # Georgian letter in a print() raised and killed the engine at boot
    # (2026-10-24). A log line may lose a glyph; GOAT may not die of it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 — pythonw has no stream at all
            pass

    try:
        import faulthandler
        crash_log = open(os.path.join(GOAT_ROOT, "python", "goat-crash.log"),
                         "a", buffering=1, encoding="utf-8", errors="replace")
        crash_log.write(f"\n==== session {time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"pid {os.getpid()} ====\n")
        faulthandler.enable(file=crash_log, all_threads=True)
    except Exception:  # noqa: BLE001 — never let instrumentation stop the app
        pass

    # Engine dies with GOAT, always (2026-09-27): an orphaned claude.exe kept
    # goat-app.log locked after a self-restart and the relaunch never came
    # up. See child_guard.py.
    try:
        import child_guard
        child_guard.start()
    except Exception:  # noqa: BLE001 — never let the guard stop the app
        pass

    # Own taskbar identity (otherwise Windows groups us under "Python").
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KingKaglu.GOAT")
    except Exception:  # noqa: BLE001
        pass

    _sweep_inbox()
    # Qt walks every installed font at startup and DirectWrite can't open
    # the two DOS bitmap fonts (8514oem, Fixedsys) — two warnings at the top
    # of every log, about fonts GOAT never uses.
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts.warning=false")
    # Keep the laptop awake while GOAT runs (his order 2026-10-18) so the
    # phone can always reach it. Screen may still turn off; ends when GOAT
    # exits. ES_CONTINUOUS | ES_SYSTEM_REQUIRED, on the main thread.
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:  # noqa: BLE001
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("GOAT")
    if os.path.exists(ICON):
        app.setWindowIcon(QIcon(ICON))
    win = GoatWindow()

    # Boot latency (2026-07-15, "it needs so much time to turn on"): the
    # goat_app import is heavy (Qt, the SDK, audio stack) — seconds warm,
    # much longer on a cold disk cache — and used to run BEFORE the window
    # existed, so launching looked like nothing was happening. Paint the
    # window immediately, import the engine on a side thread, bind it on
    # the main thread the moment the import lands.
    # --startup: Windows launched us at login (Startup-folder shortcut).
    # Come up as the dot, silent, until he says my name.
    startup = "--startup" in sys.argv
    win.showFullScreen()
    if startup:
        win.collapse()   # before the loop paints: no full-screen flash
    else:
        win.home_presence.ignite()  # boot ritual: the ring draws itself

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
        goat.quiet_boot = startup
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
