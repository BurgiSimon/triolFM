#!/usr/bin/env python3
"""triolFM — a Dynamic Island for Spotify.

A black pill pinned to the top edge of the screen. Idle it shows the cover and
a spectrum; a track change makes it peek; hovering springs it open into full
transport controls. Music only — nothing else lives in this island.

Drawing only. Everything it talks to is in spotify_backend.py.
"""

import os
import queue
import random
import sys
import threading
import time
import urllib.error

if "--version" in sys.argv[1:]:   # before Qt: asking the version needs no GUI
    from spotify_backend import __version__
    print("triolFM " + __version__)
    sys.exit()

# Wayland gives no reliable always-on-top, no input mask and no absolute
# placement — all three are load-bearing here. WSLg and every Wayland session
# ship Xwayland, so xcb is the safe default. Override with QT_QPA_PLATFORM.
if sys.platform.startswith("linux") and os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PySide6.QtCore import (QPointF, QRect, QRectF, Qt, QTimer,
                            QVariantAnimation)
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication,
                           QIcon, QImage, QPainter, QPainterPath, QPen,
                           QPixmap, QRegion)
from PySide6.QtWidgets import (QApplication, QInputDialog, QMenu, QMessageBox,
                               QSystemTrayIcon, QWidget)

import spotify_backend as be
from spotify_backend import (FAST_POLLS, AuthError, Spotify, backoff, clamp01,
                             dominant, elapsed, fetch_art, lerp, marquee_step,
                             mmss, save_cfg, spring, theme)

# ---------------------------------------------------------------- geometry

CLOSED_W, CLOSED_H = 210, 34    # idle pill: cover + spectrum, nothing else
NOTIF_W, NOTIF_H = 302, 54      # track-change peek: cover + title
OPEN_W, OPEN_H = 392, 172       # hovered: art, text, transport, progress
SHOULDER = 10                   # concave flare where the pill meets the bezel
PAD = 18
CTL_AT = 122                    # pill height at which transport starts fading in
OVERSHOOT = 1.13                # peak of spring(), sizes the host window
NOTIF_MS = 2600                 # how long a track change stays peeked
FPS_MS = 33

BLACK = QColor(0, 0, 0)
WHITE = QColor(255, 255, 255)
GREY = QColor(155, 155, 155)

SIZES = {"closed": (CLOSED_W, CLOSED_H),
         "notif": (NOTIF_W, NOTIF_H),
         "open": (OPEN_W, OPEN_H)}


def island_path(cx, w, h, rb):
    """The notch silhouette: square at the screen edge, round at the bottom,
    with the two concave shoulders that make it read as part of the bezel."""
    l, r, s = cx - w / 2, cx + w / 2, SHOULDER
    p = QPainterPath()
    p.moveTo(l - s, 0)
    p.quadTo(l, 0, l, s)
    p.lineTo(l, h - rb)
    p.quadTo(l, h, l + rb, h)
    p.lineTo(r - rb, h)
    p.quadTo(r, h, r, h - rb)
    p.lineTo(r, s)
    p.quadTo(r, 0, r + s, 0)
    p.closeSubpath()
    return p


def pil_to_pixmap(img):
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qi = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qi.copy())  # copy: qi does not own `data`


# ------------------------------------------------------------------ island

class Island(QWidget):
    def __init__(self):
        super().__init__()
        self.sp = Spotify(self.ask_cid)
        self.q = queue.Queue()
        self.wake = threading.Event()
        self.alive = True

        self.track = None
        self.art_url = None
        self.art = None            # QPixmap of the cover
        self.playing = False
        self.pos, self.dur = 0, 1
        self.stamp = time.monotonic()
        self.volume = 50
        self.title = self.artist = ""
        self.year = ""
        self.msg = "connecting…"
        self.poll = float(self.sp.cfg.get("poll", be.POLL))
        self.show_year = bool(self.sp.cfg.get("year", True))
        self.urgent = 0
        self._declined = False
        self._shown_fatal = None
        self._vol_pending = False
        self._vol_until = 0.0      # progress bar shows volume until this time
        self._seeking = None       # fraction being dragged, or None
        self._mq = [0, -1, 0]      # marquee (x, step, hold)
        self._mq_next = 0.0
        self._bars = [0.2, 0.5, 0.35, 0.7]
        self._notif_at = 0.0

        self.accent = QColor(theme(None)[2])
        self._accent_to = QColor(self.accent)
        self.state = "closed"
        self.cur = QRectF(0, 0, CLOSED_W, CLOSED_H)

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # A plain managed window gets cascade-placed: weston under WSLg drops
        # it at 32,32 and then ignores every move request. A dock is put
        # exactly where it asks for, which is the whole point of the island.
        self.setAttribute(Qt.WidgetAttribute.WA_X11NetWmWindowTypeDock)
        self.setMouseTracking(True)
        # The window never moves or resizes; only the pill painted inside it
        # does, so the spring costs no window-manager round trips. Sized for
        # the overshoot, or the rubber-band would be cropped.
        peak_w = CLOSED_W + OVERSHOOT * (OPEN_W - CLOSED_W)
        peak_h = CLOSED_H + OVERSHOOT * (OPEN_H - CLOSED_H)
        scr = QGuiApplication.primaryScreen().geometry()
        self.win_w = int(peak_w) + 2 * SHOULDER + 4
        self.setGeometry(scr.x() + (scr.width() - self.win_w) // 2, scr.y(),
                         self.win_w, int(peak_h) + 4)

        # The spring is applied here rather than through QEasingCurve's custom
        # type: handing Qt a Python easing callback segfaults under PySide6.
        self.anim = QVariantAnimation(self, duration=440, startValue=0.0,
                                      endValue=1.0)
        self.anim.valueChanged.connect(self._on_morph)
        self._from = (CLOSED_W, CLOSED_H)

        self.fade = QVariantAnimation(self, duration=350)
        self.fade.valueChanged.connect(self._on_fade)

        self._sync_mask()
        self._menu = self._build_menu()
        self.tray = self._build_tray()

        threading.Thread(target=self.poller, daemon=True).start()
        self._vol_timer = QTimer(self, singleShot=True, timeout=self._flush_vol)
        self._pump = QTimer(self, interval=100, timeout=self.drain)
        self._pump.start()
        self._paint_timer = QTimer(self, interval=FPS_MS, timeout=self.frame)
        self._paint_timer.start()

    # -- geometry ---------------------------------------------------------

    @property
    def openness(self):
        return clamp01((self.cur.height() - CLOSED_H) / (OPEN_H - CLOSED_H))

    @property
    def text_in(self):
        """0 while collapsed, 1 once the pill is at least peek-width."""
        return clamp01((self.cur.width() - CLOSED_W) / (NOTIF_W - CLOSED_W))

    @property
    def ctl_in(self):
        """0 until there is room for them, 1 fully open — transport, progress."""
        return clamp01((self.cur.height() - CTL_AT) / (OPEN_H - CTL_AT))

    def morph(self, state):
        if state == self.state:
            return
        self.state = state
        self.anim.stop()
        self._from = (self.cur.width(), self.cur.height())
        self.anim.start()

    def _on_morph(self, t):
        k = spring(t)                     # overshoots ~12%, then settles
        w, h = SIZES[self.state]
        self.cur = QRectF(0, 0, lerp(self._from[0], w, k),
                          lerp(self._from[1], h, k))
        self._sync_mask()
        self.update()

    def _sync_mask(self):
        """Input and paint only where the pill is; the rest of the window is
        click-through, so a plain rect keeps the painted corners antialiased."""
        w = int(self.cur.width()) + 2 * SHOULDER + 2
        self.setMask(QRegion(QRect((self.win_w - w) // 2, 0, w,
                                   int(self.cur.height()) + 2)))

    def pill(self):
        cx = self.win_w / 2
        w, h = self.cur.width(), self.cur.height()
        return cx - w / 2, w, h

    def art_rect(self):
        """Cover art lerps from the idle thumbnail to the open panel's art."""
        x, w, h = self.pill()
        o = self.openness
        size = lerp(22, 84, o)
        return QRectF(x + lerp(8, PAD, o), lerp(6, 24, o), size, size)

    def buttons(self):
        """name -> center, for the transport row (open state only)."""
        left, right = self.text_span()
        c, y = (left + right) / 2, self.cur.height() - 80
        return {"prev": QPointF(c - 46, y), "play": QPointF(c, y),
                "next": QPointF(c + 46, y)}

    def text_span(self):
        """(left, right) of the column right of the art, in window pixels."""
        x, w, _ = self.pill()
        return self.art_rect().right() + lerp(10, 14, self.openness), x + w - PAD

    def bar_rect(self):
        x, w, h = self.pill()
        return QRectF(x + PAD, h - 40, w - 2 * PAD, 4)

    def close_rect(self):
        """Quit cross in the pill's top-right corner (open state only)."""
        x, w, _ = self.pill()
        return QRectF(x + w - PAD - 12, 12, 12, 12)

    # -- painting ---------------------------------------------------------

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x, w, h = self.pill()
        rb = lerp(15, 28, self.openness)
        path = island_path(self.win_w / 2, w, h, rb)
        p.fillPath(path, BLACK)
        p.setClipPath(path)   # contents ride the morph instead of spilling out

        self._paint_art(p)
        if self.text_in > 0.01:
            self._paint_text(p)
        if self.text_in < 0.99:
            self._paint_bars(p, 1 - self.text_in)
        if self.ctl_in > 0.01:
            self._paint_controls(p)

    def _paint_art(self, p):
        r = self.art_rect()
        p.save()
        path = QPainterPath()
        path.addRoundedRect(r, r.width() * 0.24, r.width() * 0.24)
        p.setClipPath(path)
        if self.art:
            p.drawPixmap(r.toRect(), self.art)
        else:
            p.fillRect(r, QColor(30, 30, 30))
        p.restore()

    def _paint_text(self, p):
        a = self.text_in
        left, right = self.text_span()
        avail = right - left
        if avail < 20:
            return
        top = lerp(9, 26, self.openness)

        p.save()
        p.setOpacity(a)
        f = QFont(self.font())
        f.setPointSizeF(lerp(9.5, 11.5, self.openness))
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        fm = QFontMetricsF(f)
        title = self.title if self.track else (self.msg or "")
        over = max(0.0, fm.horizontalAdvance(title) - avail)
        p.setPen(WHITE if self.track else GREY)
        if over and self.openness > 0.6:   # marquee only when there is room
            p.save()
            p.setClipRect(QRectF(left, top, avail, fm.height() + 2))
            p.drawText(QPointF(left + self._mq[0], top + fm.ascent()), title)
            p.restore()
        else:
            p.drawText(QRectF(left, top, avail, fm.height() + 2),
                       int(Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter),
                       fm.elidedText(title, Qt.TextElideMode.ElideRight, avail))

        sub = self.artist if self.track else ""
        if sub and self.show_year and self.year:
            sub += " · " + self.year
        if sub:
            f2 = QFont(self.font())
            f2.setPointSizeF(lerp(8.0, 9.5, self.openness))
            p.setFont(f2)
            fm2 = QFontMetricsF(f2)
            p.setPen(GREY)
            p.drawText(QRectF(left, top + fm.height() + lerp(0, 4, self.openness),
                              avail, fm2.height() + 2),
                       int(Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter),
                       fm2.elidedText(sub, Qt.TextElideMode.ElideRight, avail))
        p.restore()

    def _paint_bars(self, p, a):
        """Four-bar spectrum on the idle pill."""
        x, w, h = self.pill()
        p.save()
        p.setOpacity(a)
        p.setBrush(self.accent)
        p.setPen(Qt.PenStyle.NoPen)
        right = x + w - 9
        for i, lvl in enumerate(self._bars):
            bh = 3 + lvl * 15
            p.drawRoundedRect(QRectF(right - (4 - i) * 6, (h - bh) / 2, 3.4, bh),
                              1.7, 1.7)
        p.restore()

    def _paint_controls(self, p):
        a = self.ctl_in
        p.save()
        p.setOpacity(a)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(WHITE)
        b = self.buttons()
        self._glyph_skip(p, b["prev"], -1)
        self._glyph_skip(p, b["next"], 1)
        c = b["play"]
        if self.playing:
            p.drawRoundedRect(QRectF(c.x() - 7, c.y() - 10, 5, 20), 2, 2)
            p.drawRoundedRect(QRectF(c.x() + 2, c.y() - 10, 5, 20), 2, 2)
        else:
            tri = QPainterPath()
            tri.moveTo(c.x() - 6, c.y() - 11)
            tri.lineTo(c.x() + 10, c.y())
            tri.lineTo(c.x() - 6, c.y() + 11)
            tri.closeSubpath()
            p.drawPath(tri)

        r = self.bar_rect()
        vol = time.monotonic() < self._vol_until
        frac = (self.volume / 100 if vol else
                self._seeking if self._seeking is not None else
                (self.now() / self.dur if self.track else 0.0))
        p.setBrush(QColor(255, 255, 255, 46))
        p.drawRoundedRect(r, 2, 2)
        p.setBrush(self.accent)
        p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * clamp01(frac), r.height()),
                          2, 2)

        f = QFont(self.font())
        f.setPointSizeF(8.0)
        p.setFont(f)
        p.setPen(GREY)
        box = QRectF(r.x(), r.bottom() + 4, r.width(), 14)
        if vol:
            p.drawText(box, int(Qt.AlignmentFlag.AlignHCenter),
                       f"volume {self.volume}%")
        elif self.track:
            p.drawText(box, int(Qt.AlignmentFlag.AlignLeft), mmss(self.now()))
            p.drawText(box, int(Qt.AlignmentFlag.AlignRight), mmss(self.dur))

        cr = self.close_rect().adjusted(3, 3, -3, -3)
        pen = QPen(GREY, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(cr.topLeft(), cr.bottomRight())
        p.drawLine(cr.topRight(), cr.bottomLeft())
        p.restore()

    @staticmethod
    def _glyph_skip(p, c, d):
        path = QPainterPath()
        for k in (0, 1):
            path.moveTo(c.x() + d * (k * 8 - 8), c.y() - 8)
            path.lineTo(c.x() + d * (k * 8 + 1), c.y())
            path.lineTo(c.x() + d * (k * 8 - 8), c.y() + 8)
            path.closeSubpath()
        p.drawPath(path)
        p.drawRoundedRect(QRectF(c.x() + d * 9 - 1.6, c.y() - 8, 3.2, 16), 1.4, 1.4)

    # -- per-frame --------------------------------------------------------

    def frame(self):
        t = time.monotonic()
        if self.text_in < 0.99:
            # ponytail: the Web API exposes no audio levels, so the spectrum is
            # a smoothed random walk gated on play state. Swap in a PipeWire
            # monitor tap if it ever needs to match the actual audio.
            tgt = (lambda: random.random()) if self.playing else (lambda: 0.06)
            self._bars = [l * 0.62 + tgt() * 0.38 for l in self._bars]
        if self.openness > 0.6 and t >= self._mq_next:
            self._mq_next = t + be.SCROLL_MS / 1000
            self._mq = list(marquee_step(self._mq[0], self._mq[1], self._mq[2],
                                         self._over()))
        self.update()

    def _over(self):
        f = QFont(self.font())
        f.setPointSizeF(11.5)
        f.setWeight(QFont.Weight.DemiBold)
        left, right = self.text_span()
        return max(0.0, QFontMetricsF(f).horizontalAdvance(self.title)
                   - (right - left))

    def _on_fade(self, c):
        self.accent = c

    def recolor(self, rgb):
        target = QColor(theme(rgb)[2])
        if target == self._accent_to:
            return
        self._accent_to = target
        self.fade.stop()
        self.fade.setStartValue(QColor(self.accent))
        self.fade.setEndValue(target)
        self.fade.start()

    # -- input ------------------------------------------------------------

    def enterEvent(self, _):
        self._notif_at = 0.0
        self.morph("open")

    def leaveEvent(self, _):
        if self._seeking is None:
            self.morph("closed")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self._menu.exec(e.globalPosition().toPoint())
            return
        if self.ctl_in < 0.8:
            return
        pt = e.position()
        if self.close_rect().adjusted(-6, -6, 6, 6).contains(pt):
            self.quit()
            return
        for name, c in self.buttons().items():
            if (pt - c).manhattanLength() < 26:
                self.press(name)
                return
        r = self.bar_rect()
        if r.adjusted(-6, -10, 6, 10).contains(pt) and self.track:
            self._seeking = clamp01((pt.x() - r.x()) / r.width())

    def mouseMoveEvent(self, e):
        if self._seeking is not None:
            r = self.bar_rect()
            self._seeking = clamp01((e.position().x() - r.x()) / r.width())

    def mouseReleaseEvent(self, _):
        if self._seeking is not None:
            self.press("seek", int(self._seeking * self.dur))
            self._seeking = None
            if not self.rect().contains(self.mapFromGlobal(self.cursor().pos())):
                self.morph("closed")

    def wheelEvent(self, e):
        self.press("vol+" if e.angleDelta().y() > 0 else "vol-")

    def _build_menu(self):
        m = QMenu(self)
        m.setStyleSheet(
            "QMenu{background:#1b1b1b;color:#eee;border:1px solid #333;"
            "border-radius:8px;padding:4px}"
            "QMenu::item{padding:5px 18px;border-radius:5px}"
            "QMenu::item:selected{background:#333}")
        rate = m.addMenu("Refresh rate")
        for s in (1, 3, 5, 10, 30):
            a = rate.addAction(f"{s}s")
            a.setCheckable(True)
            a.setChecked(abs(self.poll - s) < 0.01)
            a.triggered.connect(lambda _=False, s=s: self.set_rate(s))
        y = m.addAction("Show release year")
        y.setCheckable(True)
        y.setChecked(self.show_year)
        y.triggered.connect(self.set_year)
        m.addSeparator()
        m.addAction("Reconnect to Spotify…", self.reconnect)
        m.addAction("Quit triolFM", self.quit)
        return m

    def _build_tray(self):
        """Tray icon carrying the same right-click menu, quit included.

        None where the desktop has no tray — WSLg has none, and there the
        island's own right-click stays the way out.
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        icon = QIcon.fromTheme("triolfm")   # installed under hicolor/…/apps
        if icon.isNull():                   # running from a checkout
            icon = QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "icon.png"))
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("triolFM")
        tray.setContextMenu(self._menu)
        tray.show()
        return tray

    def set_rate(self, s):
        self.poll = float(s)
        self.sp.cfg["poll"] = self.poll
        save_cfg(self.sp.cfg)
        self.wake.set()

    def set_year(self, on):
        self.show_year = on
        self.sp.cfg["year"] = on
        save_cfg(self.sp.cfg)

    def reconnect(self):
        self.sp.cfg.pop("client_id", None)
        self.sp.cfg.pop("refresh", None)
        save_cfg(self.sp.cfg)
        self.sp.token, self.sp.exp = None, 0.0
        self._declined = self._shown_fatal = None
        self.msg = "connecting…"
        self.wake.set()

    def quit(self):
        self.alive = False
        QApplication.quit()

    # -- dialogs ----------------------------------------------------------

    def ask_cid(self):
        """Called on the poller thread; hands the prompt to the GUI thread."""
        if self._declined:
            return ""
        reply = queue.Queue()
        self.q.put(("ask", reply))
        return reply.get()

    def _ask_now(self):
        text, ok = QInputDialog.getText(self, "triolFM", "Spotify Client ID:")
        if not ok:
            self._declined = True
        return text.strip() if ok else ""

    # -- network ----------------------------------------------------------

    def now(self):
        return elapsed(self.pos, self.stamp, self.dur, self.playing,
                       time.monotonic())

    def press(self, key, arg=None):
        self._declined = False
        if key == "play":
            self.playing = not self.playing   # optimistic; the poll confirms
            self.pos, self.stamp = self.now(), time.monotonic()
        elif key.startswith("vol"):
            self.volume = max(0, min(100, self.volume
                                     + (5 if key == "vol+" else -5)))
            self._vol_until = time.monotonic() + 1.4
            self._vol_pending = True
            self._vol_timer.start(200)  # one PUT per burst of notches, not each
            return
        threading.Thread(target=self._do, args=(key, arg), daemon=True).start()

    def _flush_vol(self):
        self._vol_pending = False
        threading.Thread(target=self._do, args=("vol", None), daemon=True).start()

    def _do(self, key, arg):
        try:
            if key == "play":
                self.sp.call("PUT", "/me/player/"
                             + ("play" if self.playing else "pause"))
            elif key == "next":
                self.sp.call("POST", "/me/player/next")
            elif key == "prev":
                self.sp.call("POST", "/me/player/previous")
            elif key == "seek":
                self.sp.call("PUT", "/me/player/seek", position_ms=arg)
            elif key.startswith("vol"):
                self.sp.call("PUT", "/me/player/volume",
                             volume_percent=self.volume)
        except AuthError as e:
            self.q.put(("fatal", str(e)))
        except urllib.error.HTTPError as e:
            self.q.put(("err", "premium required" if e.code == 403
                        else "no active device" if e.code == 404
                        else f"http {e.code}"))
        except OSError:
            self.q.put(("err", "offline"))
        except Exception as e:
            self.q.put(("err", str(e)[:40] or type(e).__name__))
        if not key.startswith("vol"):
            self.urgent = FAST_POLLS
        self.wake.set()

    def poller(self):
        fails = 0
        while self.alive:
            try:
                self.q.put(("state", self.sp.call("GET", "/me/player")))
                fails = 0
                wait = backoff(0, self.poll, urgent=self.urgent)
                if self.urgent:
                    self.urgent -= 1
            except urllib.error.HTTPError as e:
                fails += 1
                self.q.put(("err", f"http {e.code}"))
                wait = backoff(fails, self.poll, e.headers.get("Retry-After"))
            except AuthError as e:
                fails += 1
                self.q.put(("fatal", str(e)))
                wait = backoff(fails, self.poll)
            except Exception as e:   # incl. a failed re-auth: report, never die
                fails += 1
                self.q.put(("err", "offline" if isinstance(e, OSError)
                            else str(e)[:40] or type(e).__name__))
                wait = backoff(fails, self.poll)
            self.wake.wait(wait)
            self.wake.clear()

    def _fetch(self, url):
        img = fetch_art(url)
        if img is not None:
            self.q.put(("art", (url, img)))

    def drain(self):
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self.apply(payload)
            elif kind == "err":
                self.msg = payload
            elif kind == "ask":
                payload.put(self._ask_now())
            elif kind == "fatal":
                self.msg = "setup needed"
                if payload != self._shown_fatal:   # once, not every retry
                    self._shown_fatal = payload
                    QMessageBox.critical(self, "triolFM", payload)
            elif kind == "art" and payload[0] == self.art_url:
                self.art = pil_to_pixmap(payload[1])
                self.recolor(dominant(payload[1]))
        if (self._notif_at and self.state == "notif"
                and time.monotonic() - self._notif_at > NOTIF_MS / 1000):
            self._notif_at = 0.0
            self.morph("closed")

    def apply(self, d):
        if not d or not d.get("item"):
            self.track, self.playing = None, False
            self.msg = "nothing playing"
            self.title, self.artist, self.year = self.msg, "", ""
            self.art, self.art_url = None, None
            self.recolor(None)
            return
        it = d["item"]
        self.playing = bool(d.get("is_playing"))
        self.dur = max(1, it.get("duration_ms", 1))
        self.pos = d.get("progress_ms") or 0
        self.stamp = time.monotonic()
        if not self._vol_pending:   # don't clobber a scroll we haven't sent yet
            self.volume = ((d.get("device") or {}).get("volume_percent")
                           or self.volume)
        self.msg = None
        was = self.track
        self.track = it.get("id") or it.get("uri") or it.get("name")
        alb = it.get("album") or {}
        self.year = (alb.get("release_date") or it.get("release_date") or "")[:4]
        self.title = it.get("name", "")
        self.artist = ", ".join(a["name"] for a in it.get("artists", []))
        if self.track != was:
            self._mq, self._mq_next = [0, -1, 0], 0.0
            if self.state == "closed" and was is not None:
                self._notif_at = time.monotonic()
                self.morph("notif")
        imgs = alb.get("images") or it.get("images") or []
        url = imgs[-1]["url"] if imgs else None   # smallest available
        if url and url != self.art_url:
            self.art_url = url
            threading.Thread(target=self._fetch, args=(url,), daemon=True).start()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("triolFM")
    w = Island()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
