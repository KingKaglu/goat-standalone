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
- Voice-first: no permanent input. Ctrl+K or "/" summons a bare underline
  field at the bottom; Esc dismisses it (then Esc leaves fullscreen).
- Footer is one small line of plain words: state · model · uptime.

Threading: Qt owns the main thread; GoatApp's asyncio loop runs on a
daemon thread. Events cross via a Signal; typed input crosses back via
run_coroutine_threadsafe inside submit_text.
"""
import ctypes
import math
import os
import random
import sys
import threading
import time

from PySide6.QtCore import Qt, QPoint, QPointF, QTimer, Signal
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

BG_TOP = QColor(15, 14, 12)      # warm near-black
BG_BOT = QColor(11, 10, 9)
PAPER = "#ece5d8"                # GOAT's type
DIM = "#6f6a60"                  # secondary text
FAINT = "#3d3a34"                # tertiary
AMBER = "#ffa94d"                # the only accent
AMBER_Q = QColor(255, 169, 77)

STYLE = f"""
QWidget {{ color: {PAPER}; font-family: 'Segoe UI'; }}
QLabel#wordmark {{
  color: {DIM}; font-size: 13px; letter-spacing: 5px; font-weight: 600;
}}
QLabel#stateword {{ color: {AMBER}; font-size: 13px; letter-spacing: 2px; }}
QPushButton#winbtn {{
  background: transparent; color: {FAINT}; border: none;
  font-size: 13px; padding: 2px 10px;
}}
QPushButton#winbtn:hover {{ color: {PAPER}; }}
QLabel#youNow {{
  color: {AMBER}; font-size: 15px; letter-spacing: 1px; margin-top: 18px;
}}
QLabel#replyNow {{
  color: {PAPER}; font-size: 30px; font-weight: 300;
}}
QLabel#youOld {{ color: #a37c4f; font-size: 13px; margin-top: 18px; }}
QLabel#replyOld {{ color: #948d7f; font-size: 17px; font-weight: 300; }}
QLabel#toolLine {{ color: {FAINT}; font-size: 13px; font-style: italic; }}
QLabel#footer {{ color: {FAINT}; font-size: 12px; letter-spacing: 1px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 4px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {FAINT}; border-radius: 2px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLineEdit#cmd {{
  background: transparent; border: none; border-bottom: 1px solid {DIM};
  padding: 8px 2px; font-size: 16px; color: {PAPER};
  selection-background-color: #4a3722;
}}
QLineEdit#cmd:focus {{ border-bottom: 1px solid {AMBER}; }}
"""


class Backdrop(QWidget):
    """Warm dark gradient. Nothing else — the silence behind the string."""

    def paintEvent(self, _ev):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, BG_TOP)
        g.setColorAt(1.0, BG_BOT)
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
        self.setMinimumHeight(140)

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

        # color: warm gray at rest, amber where alive
        base = QColor(111, 106, 96)
        col = QColor(
            int(base.red() + (AMBER_Q.red() - base.red()) * heat),
            int(base.green() + (AMBER_Q.green() - base.green()) * heat),
            int(base.blue() + (AMBER_Q.blue() - base.blue()) * heat),
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


class GoatWindow(QWidget):
    event_sig = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GOAT")
        self.setWindowFlags(Qt.FramelessWindowHint)
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
        self.setStyleSheet(STYLE)

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

        # ---- command field (hidden — voice-first) ----
        self.input = QLineEdit()
        self.input.setObjectName("cmd")
        self.input.setPlaceholderText("type — enter to send, esc to dismiss")
        self.input.returnPressed.connect(self._submit)
        self.input.hide()
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
        hint = QLabel("ctrl+k type · ctrl+o file · drop files anywhere · f11 screen")
        hint.setObjectName("footer")
        foot_row.addWidget(hint)
        lay.addLayout(foot_row)

        self.event_sig.connect(self._on_event)

        QShortcut(QKeySequence(Qt.Key_F11), self, self.toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._escape)
        QShortcut(QKeySequence("Ctrl+K"), self, self._show_cmd)
        QShortcut(QKeySequence(Qt.Key_Slash), self, self._show_cmd)
        QShortcut(QKeySequence("Ctrl+O"), self, self._pick_files)

    # ---- window controls ----
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _show_cmd(self):
        self.input.show()
        self.input.setFocus()

    def _escape(self):
        if self.input.isVisible():
            self.input.hide()
        elif self.isFullScreen():
            self.showNormal()

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
        note = self.input.text().strip() if self.input.isVisible() else ""
        self.input.clear()
        self.input.hide()
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
        self.footer.setText(f"{self._model} · mic live · {up // 60:02d}:{up % 60:02d}{usage}")

    # ---- input ----
    def _submit(self):
        text = self.input.text().strip()
        if text and self.on_submit:
            self.on_submit(text)
            self.input.clear()
            self.input.hide()

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
        lbl = QLabel(text)
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
    code = app.exec()
    goat.shutdown_audio()
    os._exit(code)  # asyncio daemon thread has no clean cross-thread stop


if __name__ == "__main__":
    main()
