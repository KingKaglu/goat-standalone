"""GOAT v8 phase (e) (design.md, mockups C and D): the Ctrl K command
palette and the proactive cards.

Palette: one box over the room that reaches every action, setting, skill
and page (Ctrl K), or only past conversations (Ctrl F). Type to filter,
Up/Down to move, Enter to run, Esc or a click outside to close.

Proactive card: a quiet note that rises 12 px and fades in (220 ms) above
the composer when GOAT has something for him (a watcher alert, a thought it
spoke first, the usage limit, a failed job). Never modal, never takes focus,
leaves after 30 s unless the pointer is on it.

Both are plain children of the main window, styled by build_style()'s
#palette / #pcard rules, so they follow the theme and the text scale."""
from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint, QPropertyAnimation,
                            QRect, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

ROLE_KIND = Qt.UserRole + 1      # "head" | "item"
ROLE_HINT = Qt.UserRole + 2      # right-hand hint ("Ctrl N", "skill", "2h ago")
ROLE_SUB = Qt.UserRole + 3       # second line (conversation reply)
ROLE_IDX = Qt.UserRole + 4       # index into the palette's entries

MONO = ["Cascadia Mono", "Consolas"]
BODY = ["Segoe UI Variable Text", "Segoe UI"]


def _font(fams: list, px: int) -> QFont:
    f = QFont()
    f.setFamilies(fams)
    f.setPixelSize(px)
    return f


# ---------------------------------------------------------------- ranking --
def rank(query: str, title: str, extra: str = "") -> int:
    """How well `query` matches an entry; 0 = no match. Pure (tested).
    Whole-title prefix > word-start > substring > in-order letters; the
    extra text (keywords, description) matches weaker."""
    q = " ".join(query.lower().split())
    if not q:
        return 1
    t = title.lower()
    if t.startswith(q):
        return 1000 - len(t)
    if re.search(r"(?:^|[\s·\-_/])" + re.escape(q), t):
        return 800 - len(t)
    if q in t:
        return 600 - len(t)
    words = q.split()
    hay = t + " " + extra.lower()
    if len(words) > 1 and all(w in hay for w in words):
        return 450
    if q in extra.lower():
        return 300
    # letters in order ("rsg" -> restart goat), titles only
    i = 0
    for ch in t:
        if i < len(q) and ch == q[i]:
            i += 1
    if i == len(q) and len(q) >= 2:
        return 100
    return 0


class Entry:
    __slots__ = ("section", "title", "hint", "run", "extra", "sub")

    def __init__(self, section: str, title: str, hint: str, run: Callable,
                 extra: str = "", sub: str = ""):
        self.section, self.title, self.hint = section, title, hint
        self.run, self.extra, self.sub = run, extra, sub


def filter_entries(entries: list, query: str, limit: int = 60) -> list:
    """Entries that match, grouped by section in their first-seen order,
    best match first inside a section. Pure (tested)."""
    scored = [(rank(query, e.title, e.extra), i, e) for i, e in enumerate(entries)]
    scored = [x for x in scored if x[0] > 0]
    # loose letters-in-order hits only show when nothing better matched
    if any(x[0] >= 300 for x in scored):
        scored = [x for x in scored if x[0] >= 300]
    if not query.strip():
        return [e for _, _, e in scored][:limit]
    # sections keep their places (mockup C: Actions, Settings, Skills,
    # Pages), so the eye always knows where to look; best first inside each
    order: dict = {}
    for e in entries:
        order.setdefault(e.section, len(order))
    scored.sort(key=lambda x: (order[x[2].section], -x[0], x[1]))
    return [e for _, _, e in scored][:limit]


# ------------------------------------------------------------- delegate --
class _RowPaint(QStyledItemDelegate):
    def __init__(self, pal: "CommandPalette"):
        super().__init__(pal)
        self.p = pal

    def sizeHint(self, opt, idx):
        s = self.p.scale
        if idx.data(ROLE_KIND) == "head":
            return QSize(10, round(30 * s))
        return QSize(10, round((56 if idx.data(ROLE_SUB) else 44) * s))

    def paint(self, qp: QPainter, opt, idx):
        t, s = self.p.t, self.p.scale
        r = opt.rect
        qp.save()
        qp.setRenderHint(QPainter.Antialiasing)
        if idx.data(ROLE_KIND) == "head":
            f = _font(MONO, max(8, round(10 * s)))
            f.setLetterSpacing(QFont.AbsoluteSpacing, 1.6 * s)
            qp.setFont(f)
            qp.setPen(QColor(t.get("faint", "#56627d")))
            qp.drawText(r.adjusted(round(22 * s), round(8 * s), 0, 0),
                        Qt.AlignLeft | Qt.AlignVCenter, idx.data(Qt.DisplayRole).upper())
            qp.restore()
            return
        sel = bool(opt.state & QStyle.State_Selected)
        hov = bool(opt.state & QStyle.State_MouseOver)
        box = r.adjusted(round(10 * s), round(2 * s), -round(10 * s), -round(2 * s))
        if sel or hov:
            c = QColor(t.get("sel", "#1d3b6e"))
            c.setAlpha(235 if sel else 110)
            qp.setPen(Qt.NoPen)
            qp.setBrush(c)
            qp.drawRoundedRect(box, 10 * s, 10 * s)
            if sel:
                qp.setBrush(QColor(t.get("accent", "#5aa2ff")))
                bar = QRect(box.left(), box.top() + round(6 * s), max(2, round(3 * s)),
                            box.height() - round(12 * s))
                qp.drawRoundedRect(bar, 1.5 * s, 1.5 * s)
        hint = idx.data(ROLE_HINT) or ""
        fh = _font(MONO, max(8, round(10 * s)))
        hw = QFontMetrics(fh).horizontalAdvance(hint) + round(16 * s) if hint else 0
        text_r = box.adjusted(round(16 * s), 0, -(hw + round(8 * s)), 0)
        ft = _font(BODY, max(9, round(15 * s)))
        if sel:
            ft.setWeight(QFont.DemiBold)
        sub = idx.data(ROLE_SUB) or ""
        qp.setFont(ft)
        qp.setPen(QColor(t.get("paper", "#e9eefa")))
        title = idx.data(Qt.DisplayRole) or ""
        if sub:
            top = text_r.adjusted(0, round(7 * s), 0, -text_r.height() // 2)
            qp.drawText(top, Qt.AlignLeft | Qt.AlignVCenter,
                        QFontMetrics(ft).elidedText(title, Qt.ElideRight, top.width()))
            fs = _font(BODY, max(8, round(12 * s)))
            qp.setFont(fs)
            qp.setPen(QColor(t.get("dim", "#8e9ab4")))
            bot = text_r.adjusted(0, text_r.height() // 2, 0, -round(7 * s))
            qp.drawText(bot, Qt.AlignLeft | Qt.AlignVCenter,
                        QFontMetrics(fs).elidedText(sub, Qt.ElideRight, bot.width()))
        else:
            qp.drawText(text_r, Qt.AlignLeft | Qt.AlignVCenter,
                        QFontMetrics(ft).elidedText(title, Qt.ElideRight, text_r.width()))
        if hint:
            qp.setFont(fh)
            qp.setPen(QColor(t.get("faint", "#56627d") if not sel else t.get("dim", "#8e9ab4")))
            qp.drawText(box.adjusted(0, 0, -round(14 * s), 0),
                        Qt.AlignRight | Qt.AlignVCenter, hint)
        qp.restore()


# --------------------------------------------------------------- palette --
class CommandPalette(QWidget):
    """Covers the window (a soft scrim); the panel sits centred over the
    room, 150 px down, 660 px wide (mockup C). `source(mode)` returns the
    entries for "all" (Ctrl K) or "chat" (Ctrl F)."""

    closed = Signal()

    def __init__(self, parent: QWidget, source: Callable[[str], list]):
        super().__init__(parent)
        self.source = source
        self.t: dict = {}
        self.scale = 1.0
        self.mode = "all"
        self._entries: list = []
        self._shown: list = []
        self.area: Callable[[], QRect] | None = None     # the room, for centring
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.hide()
        self.panel = QFrame(self)
        self.panel.setObjectName("palette")
        v = QVBoxLayout(self.panel)
        v.setContentsMargins(0, 0, 0, 8)
        v.setSpacing(0)
        top = QFrame()
        top.setObjectName("paletteTop")
        h = QHBoxLayout(top)
        h.setContentsMargins(20, 14, 20, 14)
        h.setSpacing(12)
        self.glass = QLabel("⌕")
        self.glass.setObjectName("paletteGlass")
        h.addWidget(self.glass)
        self.box = QLineEdit()
        self.box.setObjectName("paletteField")
        self.box.textChanged.connect(self._refill)
        self.box.installEventFilter(self)
        h.addWidget(self.box, 1)
        self.esc = QLabel("Esc to close")
        self.esc.setObjectName("paletteKbd")
        h.addWidget(self.esc)
        v.addWidget(top)
        self.list = QListWidget()
        self.list.setObjectName("paletteList")
        self.list.setItemDelegate(_RowPaint(self))
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.itemClicked.connect(lambda it: self._run(it))
        v.addWidget(self.list)
        self.empty = QLabel("Nothing matches.")
        self.empty.setObjectName("paletteEmpty")
        self.empty.setContentsMargins(22, 10, 22, 12)
        v.addWidget(self.empty)
        self.empty.hide()
        self._fx = QGraphicsOpacityEffect(self.panel)
        self._fx.setOpacity(1.0)
        self.panel.setGraphicsEffect(self._fx)
        self._fade = QPropertyAnimation(self._fx, b"opacity", self)
        self._fade.setDuration(140)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

    # ---- look ----
    def set_theme(self, t: dict):
        self.t = dict(t)
        self.list.viewport().update()
        self.update()

    def set_scale(self, k: float):
        self.scale = float(k or 1.0)
        self._place()

    def paintEvent(self, _ev):
        qp = QPainter(self)
        c = QColor(self.t.get("bg_bot", "#060a14"))
        c.setAlpha(120)
        qp.fillRect(self.rect(), c)

    # ---- open / close ----
    def open(self, mode: str = "all", text: str = ""):
        self.mode = mode
        self._entries = list(self.source(mode) or [])
        self.box.setPlaceholderText(
            "Search conversations…" if mode == "chat"
            else "Type a command, setting, skill or page…")
        self.box.blockSignals(True)
        self.box.setText(text)
        self.box.blockSignals(False)
        self._refill()
        self.setGeometry(self.parentWidget().rect())
        self._place()
        self.raise_()
        self.show()
        self._fx.setOpacity(0.0)
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self.box.setFocus()

    def close_palette(self):
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def _place(self):
        if not self.parentWidget():
            return
        s = self.scale
        self.setGeometry(self.parentWidget().rect())
        room = self.area() if self.area else self.rect()
        w = min(round(660 * s), room.width() - round(48 * s))
        rows = sum(self._row_h(i) for i in range(self.list.count()))
        list_h = min(rows + round(8 * s), round(460 * s),
                     self.height() - round(260 * s))
        self.list.setFixedHeight(max(round(40 * s), list_h))
        self.list.setVisible(self.list.count() > 0)
        self.panel.adjustSize()
        h = self.panel.sizeHint().height()
        x = room.left() + (room.width() - w) // 2
        y = room.top() + min(round(150 * s), max(round(24 * s), (room.height() - h) // 3))
        self.panel.setGeometry(x, y, w, h)

    def _row_h(self, i: int) -> int:
        it = self.list.item(i)
        return self.list.itemDelegate().sizeHint(None, self.list.indexFromItem(it)).height()

    # ---- filtering ----
    def _refill(self, *_):
        q = self.box.text()
        self._shown = filter_entries(self._entries, q)
        self.list.clear()
        last = None
        first_item = None
        # the cursor starts on the first row of the best tier (prefix >
        # word start > substring > keywords), so a weak keyword hit in an
        # earlier section never steals Enter from an exact page or action
        tiers = [rank(q, e.title, e.extra) // 100 for e in self._shown]
        top = max(tiers) if tiers else 0
        for n, e in enumerate(self._shown):
            if e.section != last:
                hd = QListWidgetItem(e.section)
                hd.setData(ROLE_KIND, "head")
                hd.setFlags(Qt.NoItemFlags)
                self.list.addItem(hd)
                last = e.section
            it = QListWidgetItem(e.title)
            it.setData(ROLE_KIND, "item")
            it.setData(ROLE_HINT, e.hint)
            it.setData(ROLE_SUB, e.sub)
            it.setData(ROLE_IDX, n)
            self.list.addItem(it)
            if first_item is None and tiers[n] == top:
                first_item = it
        if first_item is not None:
            self.list.setCurrentItem(first_item)
            self.list.scrollToTop()
            self.list.scrollToItem(first_item)
        self.empty.setText("Nothing matches." if self._entries else (
            "No conversations yet." if self.mode == "chat" else "Nothing here."))
        self.empty.setVisible(first_item is None)
        if self.isVisible():
            self._place()

    def _move(self, step: int):
        n = self.list.count()
        if not n:
            return
        i = self.list.currentRow()
        for _ in range(n):
            i = (i + step) % n
            if self.list.item(i).data(ROLE_KIND) == "item":
                self.list.setCurrentRow(i)
                self.list.scrollToItem(self.list.item(i))
                return

    def _run(self, it: QListWidgetItem | None):
        if it is None or it.data(ROLE_KIND) != "item":
            return
        e = self._shown[it.data(ROLE_IDX)]
        self.close_palette()
        QTimer.singleShot(0, e.run)

    # ---- keys / clicks ----
    def eventFilter(self, obj, ev):
        if obj is self.box and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Tab):
                self._move(1)
                return True
            if k in (Qt.Key_Up, Qt.Key_Backtab):
                self._move(-1)
                return True
            if k == Qt.Key_PageDown:
                for _ in range(5):
                    self._move(1)
                return True
            if k == Qt.Key_PageUp:
                for _ in range(5):
                    self._move(-1)
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self._run(self.list.currentItem())
                return True
            if k == Qt.Key_Escape:
                if self.box.text():
                    self.box.clear()
                else:
                    self.close_palette()
                return True
            if (ev.modifiers() & Qt.ControlModifier) and k in (Qt.Key_K, Qt.Key_F):
                want = "chat" if k == Qt.Key_F else "all"
                if want == self.mode:
                    self.close_palette()
                else:
                    self.open(want, self.box.text())
                return True
        return super().eventFilter(obj, ev)

    def mousePressEvent(self, ev):
        if not self.panel.geometry().contains(ev.position().toPoint()):
            self.close_palette()
        ev.accept()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.isVisible():
            self._place()


# ------------------------------------------------------- proactive card --
class ProactiveCard(QFrame):
    """One note at a time; later ones wait in a short queue (max 4)."""

    LIFE_MS = 30_000
    RISE_PX = 12

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("pcard")
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.scale = 1.0
        self.anchor: Callable[[], QRect] | None = None   # bottom-right corner box
        self._queue: list = []
        self._cur: dict | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 14, 14)
        v.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.kicker = QLabel("")
        self.kicker.setObjectName("pcardKicker")
        top.addWidget(self.kicker, 1)
        self.x = QPushButton("✕")
        self.x.setObjectName("pcardX")
        self.x.setFocusPolicy(Qt.NoFocus)
        self.x.setCursor(Qt.PointingHandCursor)
        self.x.clicked.connect(self.dismiss)
        top.addWidget(self.x)
        v.addLayout(top)
        self.text = QLabel("")
        self.text.setObjectName("pcardText")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.NoTextInteraction)
        v.addWidget(self.text)
        self.btns = QHBoxLayout()
        self.btns.setSpacing(8)
        self.btn_row = QWidget()
        self.btn_row.setLayout(self.btns)
        self.btns.setContentsMargins(0, 4, 0, 0)
        v.addWidget(self.btn_row)
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._fade = QPropertyAnimation(self._fx, b"opacity", self)
        self._rise = QPropertyAnimation(self, b"pos", self)
        for a in (self._fade, self._rise):
            a.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.finished.connect(self._faded)
        self._life = QTimer(self)
        self._life.setSingleShot(True)
        self._life.timeout.connect(self.dismiss)
        self._leaving = False
        self.hide()

    def set_scale(self, k: float):
        self.scale = float(k or 1.0)
        if self.isVisible():
            self._place(final=True)

    # ---- public ----
    def push(self, text: str, kicker: str = "GOAT", tone: str = "accent",
             actions: list | None = None, key: str = ""):
        """actions: [(label, callable), ...], first one is the primary.
        `key` replaces a waiting/showing card with the same key (no pile-up)."""
        card = {"text": text.strip(), "kicker": kicker, "tone": tone,
                "actions": list(actions or [])[:2], "key": key}
        if not card["text"]:
            return
        if key:
            self._queue = [c for c in self._queue if c.get("key") != key]
            if self._cur and self._cur.get("key") == key and self.isVisible():
                self._fill(card)
                self._cur = card
                self._life.start(self.LIFE_MS)
                return
        self._queue.append(card)
        self._queue = self._queue[-4:]
        if not self.isVisible():
            self._next()

    def dismiss(self):
        if not self.isVisible() or self._leaving:
            return
        self._life.stop()
        self._leaving = True
        self._fade.stop()
        self._fade.setDuration(180)
        self._fade.setStartValue(self._fx.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def showing(self) -> str:
        return self.text.text() if self.isVisible() and not self._leaving else ""

    # ---- inner ----
    def _fill(self, c: dict):
        self.kicker.setText(c["kicker"].upper())
        self.kicker.setProperty("tone", c["tone"])
        self.setProperty("tone", c["tone"])
        for w in (self, self.kicker):
            w.style().unpolish(w)
            w.style().polish(w)
        self.text.setText(c["text"])
        while self.btns.count():
            it = self.btns.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for n, (label, fn) in enumerate(c["actions"]):
            b = QPushButton(label)
            b.setObjectName("pcardBtn")
            b.setProperty("primary", "true" if n == 0 else "false")
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, f=fn: self._act(f))
            self.btns.addWidget(b)
        self.btns.addStretch(1)
        self.btn_row.setVisible(bool(c["actions"]))

    def _act(self, fn):
        self.dismiss()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"[pcard] action failed: {e}")

    def _next(self):
        if not self._queue:
            self._cur = None
            return
        c = self._cur = self._queue.pop(0)
        self._fill(c)
        self._leaving = False
        self._place(final=False)
        self.raise_()
        self.show()
        end = self._target()
        self._rise.stop()
        self._rise.setDuration(220)
        self._rise.setStartValue(end + QPoint(0, round(self.RISE_PX * self.scale)))
        self._rise.setEndValue(end)
        self._fade.stop()
        self._fade.setDuration(220)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._rise.start()
        self._fade.start()
        self._life.start(self.LIFE_MS)

    def _faded(self):
        if self._leaving:
            self.hide()
            self._leaving = False
            QTimer.singleShot(260, self._next)

    def _target(self) -> QPoint:
        box = self.anchor() if self.anchor else self.parentWidget().rect()
        return QPoint(box.right() - self.width(), box.bottom() - self.height())

    def _place(self, final: bool):
        s = self.scale
        w = round(380 * s)
        self.setFixedWidth(w)
        self.layout().activate()
        self.setFixedHeight(self.layout().heightForWidth(w)
                            if self.layout().hasHeightForWidth()
                            else self.sizeHint().height())
        if final:
            self.move(self._target())

    def enterEvent(self, ev):
        self._life.stop()
        if self._leaving:          # caught on its way out: stay
            self._leaving = False
            self._fade.stop()
            self._fx.setOpacity(1.0)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self.isVisible() and not self._leaving:
            self._life.start(10_000)
        super().leaveEvent(ev)
