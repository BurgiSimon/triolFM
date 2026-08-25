"""Self-check for the bits that silently break: PKCE, easing, progress.

Backend tests run headless. The Island tests need PySide6 and are skipped
without it — spotify_backend covers everything that does not touch a screen.
"""
import base64
import hashlib
import json
import os
import queue
import stat
import sys
import tempfile
import threading
import time
import types

for name in ("PIL", "PIL.Image"):
    sys.modules.setdefault(name, types.ModuleType(name))

import spotify_backend as be
from spotify_backend import (ACCENT, BG, HOLD, backoff, clamp01, dominant,
                             elapsed, lerp, marquee_step, mix, mmss,
                             parse_body, pkce, shade, spring, theme)

try:
    import triolfm
except ImportError:                      # no PySide6 here
    triolfm = None


def test_pkce():
    v, c = pkce()
    # challenge must be exactly S256(verifier), base64url, unpadded
    want = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest())
    assert c == want.rstrip(b"=").decode(), c
    assert "=" not in v + c and "+" not in v + c and "/" not in v + c
    assert 43 <= len(v) <= 128, len(v)   # RFC 7636 length bounds
    assert pkce()[0] != v                # fresh verifier each call


def test_spring():
    assert spring(0.0) == 0.0            # starts where it is
    assert spring(1.0) == 1.0            # and lands exactly, no drift
    assert max(spring(t / 100) for t in range(101)) > 1.05   # real overshoot
    assert abs(spring(0.9) - 1) < 0.02   # settled well before the end


def test_lerp_clamp():
    assert (lerp(10, 20, 0), lerp(10, 20, 1), lerp(10, 20, 0.5)) == (10, 20, 15)
    assert (clamp01(-3), clamp01(0.4), clamp01(9)) == (0.0, 0.4, 1.0)


def test_marquee_step():
    assert marquee_step(0, -1, 3, 20) == (0, -1, 2)   # holding: frozen
    assert marquee_step(0, -1, 0, 20) == (-1, -1, 0)  # then creeps left
    # bounces at the far end and holds, never overshooting the overflow
    assert marquee_step(-19, -1, 0, 20) == (-20, 1, HOLD)
    assert marquee_step(-1, 1, 0, 20) == (0, -1, HOLD)  # and back at the start
    # a full round trip stays inside [-over, 0] and returns to where it began
    x, step, hold, seen = 0, -1, 0, []
    for _ in range(4 * (20 + HOLD)):
        x, step, hold = marquee_step(x, step, hold, 20)
        seen.append(x)
    assert min(seen) == -20 and max(seen) == 0, (min(seen), max(seen))
    assert (x, step) == (0, -1), (x, step)


def test_parse_body():
    JSON = "application/json; charset=utf-8"
    assert parse_body(JSON, b'{"a": 1}') == {"a": 1}
    assert parse_body(JSON, b"") is None            # 204, no content
    assert parse_body("", b"") is None              # 204, no headers either
    assert parse_body("text/plain", b"OK") is None  # 200, non-JSON body
    assert parse_body("text/html", b"<html>") is None


def test_elapsed():
    # paused: frozen regardless of clock
    assert elapsed(5000, 10.0, 60000, False, 999.0) == 5000
    # playing: 2s of wall clock = +2000ms
    assert elapsed(5000, 10.0, 60000, True, 12.0) == 7000
    # never runs past the track end
    assert elapsed(5000, 10.0, 6000, True, 99.0) == 6000


def test_shade():
    assert shade((255, 0, 0), 0.5) == "#ff0000"          # hue kept, mid light
    assert shade((255, 0, 0), 0.0) == "#000000"
    assert shade((255, 0, 0), 0.5, 0.0, 0.0) == "#808080"  # saturation capped
    # gray has no hue, so forcing saturation up must not invent one
    assert shade((128, 128, 128), 0.5, 0.55) == "#808080"


def test_theme():
    assert theme(None) == (BG, "#2a2a2a", ACCENT)
    bg, surface, accent = theme((255, 0, 0))
    # background stays a dark tint, accent stays bright enough to see
    assert bg < surface < accent, (bg, surface, accent)
    assert accent.startswith("#f") or accent.startswith("#e"), accent


def test_dominant():
    from PIL import Image
    if not hasattr(Image, "new"):
        return  # PIL stubbed out in this environment; shade/theme cover the math
    img = Image.new("RGB", (64, 64), (0, 0, 0))
    img.paste(Image.new("RGB", (16, 16), (200, 30, 30)), (0, 0))
    r, g, b = dominant(img)
    assert r > 120 and g < 90 and b < 90, (r, g, b)  # the red patch, not the black
    # an all-black cover has no color to find: falls back to neutral gray
    assert dominant(Image.new("RGB", (64, 64), (0, 0, 0))) == (110, 110, 110)


def test_mmss():
    assert mmss(0) == "0:00"
    assert mmss(-5) == "0:00"            # a clock that never runs backwards
    assert mmss(59_999) == "0:59"        # truncates, so it never shows 1:00 early
    assert mmss(60_000) == "1:00"
    assert mmss(3_723_000) == "62:03"    # long podcasts stay in minutes


def test_mix():
    assert mix("#000000", "#ffffff", 0.0) == "#000000"
    assert mix("#000000", "#ffffff", 1.0) == "#ffffff"   # lands exactly on target
    assert mix("#000000", "#ffffff", 0.5) == "#808080"
    assert mix("#1db954", "#1db954", 0.4) == "#1db954"   # no drift when equal


def test_backoff():
    assert backoff(0, 3.0) == 3.0                 # healthy: the configured rate
    assert backoff(1, 3.0) == 6.0
    assert backoff(3, 3.0) == 24.0
    assert backoff(9, 3.0) == 60.0                # capped, not 25 minutes
    assert backoff(4, 3.0, "7") == 7.0            # Retry-After wins
    assert backoff(4, 3.0, None) == 48.0
    # after a press, re-check fast so a 30s rate doesn't show a stale track
    assert backoff(0, 30.0, urgent=2) == 0.5
    assert backoff(0, 30.0, urgent=0) == 30.0     # burst spent: back to normal
    assert backoff(0, 0.3, urgent=1) == 0.3       # already faster: leave it
    assert backoff(2, 3.0, urgent=2) == 12.0      # errors outrank the burst
    assert backoff(0, 30.0, "7", urgent=2) == 7.0  # and so does Retry-After


# Real /me/player payload shapes. apply() is the one function fed arbitrary
# JSON by a service we don't control, so pin the shapes that differ.
TRACK = {"is_playing": True, "progress_ms": 1000,
         "device": {"volume_percent": 33},
         "item": {"id": "t1", "name": "Song", "duration_ms": 200_000,
                  "artists": [{"name": "A"}, {"name": "B"}],
                  "album": {"release_date": "1998-04-01",
                            "images": [{"url": "big"}, {"url": "small"}]}}}
# podcasts: no album, so the year and the art hang off the item itself
EPISODE = {"is_playing": False, "device": None,
           "item": {"id": "e1", "name": "Ep 1", "duration_ms": 3_600_000,
                    "artists": [], "release_date": "2024-02-03",
                    "images": [{"url": "epbig"}, {"url": "epsmall"}]}}
# local files: no id, no art, no release date
LOCAL = {"is_playing": True, "progress_ms": 5,
         "item": {"uri": "spotify:local:x", "name": "Demo", "duration_ms": 0,
                  "artists": [{"name": "Me"}]}}


def fake_island():
    """An Island with just enough state for apply(): no Qt, no network."""
    w = triolfm.Island.__new__(triolfm.Island)
    w.track = w.art_url = w.art = None
    w._vol_pending = False
    w.playing, w.dur, w.pos, w.volume, w.msg = False, 1, 0, 50, "connecting…"
    w.stamp, w.year, w.title, w.artist = 0.0, "", "", ""
    w.state, w._mq, w._mq_next, w._notif_at = "closed", [0, -1, 0], 0.0, 0.0
    w.recolor = lambda rgb: None
    w.morph = lambda state: setattr(w, "state", state)
    w._fetch = lambda url: None
    return w


def test_apply_track():
    if triolfm is None:
        return
    w = fake_island()
    w.apply(TRACK)
    assert w.track == "t1" and w.msg is None
    assert (w.title, w.artist) == ("Song", "A, B"), (w.title, w.artist)
    assert w.year == "1998" and w.dur == 200_000 and w.playing
    assert w.art_url == "small"          # smallest image, not the first
    assert w.volume == 33


def test_apply_episode():
    if triolfm is None:
        return
    w = fake_island()
    w.apply(EPISODE)
    assert (w.title, w.artist) == ("Ep 1", "") and w.year == "2024"
    assert w.art_url == "epsmall"        # art on the item, not on an album
    assert w.volume == 50                # device: null must not wipe it
    assert not w.playing and w.pos == 0  # no progress_ms in the payload


def test_apply_local_file():
    if triolfm is None:
        return
    w = fake_island()
    w.apply(LOCAL)
    assert w.track == "spotify:local:x"  # no id: fall back to the uri
    assert w.dur == 1                    # never 0 — the progress bar divides
    assert w.art_url is None and w.year == ""


def test_apply_nothing_playing():
    if triolfm is None:
        return
    for payload in (None, {}, {"item": None}):
        w = fake_island()
        w.apply(payload)
        assert w.track is None and not w.playing, payload
        assert w.msg == "nothing playing", payload


def test_apply_keeps_pending_volume():
    if triolfm is None:
        return
    w = fake_island()
    w._vol_pending, w.volume = True, 70
    w.apply(TRACK)
    assert w.volume == 70  # a scroll we haven't sent yet outranks the poll


def test_apply_peeks_on_track_change():
    if triolfm is None:
        return
    w = fake_island()
    w.apply(TRACK)
    assert w.state == "closed"       # first track ever: no notification
    w.apply(dict(TRACK, item=dict(TRACK["item"], id="t2", name="Next")))
    assert w.state == "notif" and w._notif_at   # a real change peeks
    w.state, w._notif_at = "closed", 0.0
    w.apply(dict(TRACK, item=dict(TRACK["item"], id="t2", name="Next")))
    assert w.state == "closed"       # same track again: stays quiet


def test_ask_cid_hands_off_to_main_thread():
    if triolfm is None:
        return
    w = triolfm.Island.__new__(triolfm.Island)
    w.q, w._declined = queue.Queue(), False
    out = []

    def asker():
        out.append(w.ask_cid())

    t = threading.Thread(target=asker)   # stands in for the poller thread
    t.start()
    kind, box = w.q.get(timeout=5)       # what drain() sees on the GUI thread
    assert kind == "ask"
    box.put("ABC")
    t.join(5)
    assert out == ["ABC"] and not w._declined

    w._declined = True                   # the user dismissed the dialog
    # latched: the poller retries forever, and must not reopen a dialog each time
    assert w.ask_cid() == "" and w.q.empty()


def live_island():
    """A real Island on Qt's offscreen platform, with no Spotify behind it."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication

    class FakeSpotify:
        def __init__(self, ask=None):
            self.cfg = {}

        def call(self, *a, **kw):
            return None

    triolfm.Spotify = FakeSpotify
    triolfm.Island.poller = lambda self: None
    app = QApplication.instance() or QApplication([])
    w = triolfm.Island()
    # The offscreen platform has no pointer, and frame() closes the island the
    # moment the pointer is off the pill. Tests that hand it an enterEvent
    # have to fake the pointer being there too.
    w.cursor = lambda: types.SimpleNamespace(
        pos=lambda: w.mapToGlobal(w.mask().boundingRect().center()))
    return app, w


def settle(app, w, seconds=2.0):
    end = time.monotonic() + seconds
    while w.anim.state() and time.monotonic() < end:
        app.processEvents()
    app.processEvents()


def test_hover_morphs_open_and_back():
    if triolfm is None:
        return
    app, w = live_island()
    assert w.cur.height() == triolfm.CLOSED_H
    w.enterEvent(None)
    settle(app, w)
    assert w.state == "open" and abs(w.cur.height() - triolfm.OPEN_H) < 0.5
    assert w.ctl_in == 1.0 and w.text_in == 1.0   # everything faded in
    # frame() polls hover in both directions now, so a leave has to come with
    # the pointer actually off the pill or the next frame reopens the island
    away = w.mask().boundingRect().bottomLeft()
    w.cursor = lambda: types.SimpleNamespace(pos=lambda: w.mapToGlobal(away))
    w.leaveEvent(None)
    settle(app, w)
    assert w.state == "closed" and abs(w.cur.height() - triolfm.CLOSED_H) < 0.5
    assert w.ctl_in == 0.0                         # transport gone again
    w.close()


def test_collapse_keeps_the_band_it_un_masks_for_one_paint():
    """A shrinking mask must lag one frame, or Xwayland keeps showing the
    pixels the shape just dropped -- ghost pills all over the top bezel."""
    if triolfm is None:
        return
    app, w = live_island()
    box = lambda: w.mask().boundingRect()
    w.cur = w.cur.__class__(0, 0, triolfm.OPEN_W, triolfm.OPEN_H)
    w._sync_mask()
    grown = box()
    w.cur = w.cur.__class__(0, 0, triolfm.CLOSED_W, triolfm.CLOSED_H)
    w._sync_mask()                               # shrink: band still masked in
    assert box() == grown, (box(), grown)
    w._sync_mask()                               # and only now does it go
    assert box().width() < grown.width()
    # a real morph must not leave that last late band masked in either
    w.enterEvent(None)
    settle(app, w)
    away = w.mask().boundingRect().bottomLeft()
    w.cursor = lambda: types.SimpleNamespace(pos=lambda: w.mapToGlobal(away))
    w.leaveEvent(None)
    settle(app, w)
    assert box() == w.pill_region().adjusted(-triolfm.HALO, 0,
                                             triolfm.HALO, triolfm.HALO), box()
    w.close()


def test_pinned_to_primary_screen_top():
    if triolfm is None:
        return
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    _, w = live_island()
    scr = QGuiApplication.primaryScreen().geometry()
    g = w.geometry()
    assert g.y() == scr.y(), (g, scr)                      # flush to the top
    assert abs(g.center().x() - scr.center().x()) <= 1, (g, scr)
    # The dock type is what makes a window manager honour that position.
    # Without it weston cascade-places the island off in a corner and then
    # ignores every move request, which reads as "hover does nothing".
    assert w.testAttribute(Qt.WidgetAttribute.WA_X11NetWmWindowTypeDock)
    w.close()


def test_open_transport_hit_targets():
    if triolfm is None:
        return
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    app, w = live_island()
    w.track, w.dur, w.playing = "t1", 100_000, False
    w.enterEvent(None)
    settle(app, w)
    pressed = []
    w._do = lambda key, arg: pressed.append((key, arg))

    def click(pt):
        w.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, pt, Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))

    click(w.buttons()["play"])
    assert w.playing                       # optimistic flip, before any request
    click(w.buttons()["next"])
    r = w.bar_rect()
    click(QPointF(r.x() + r.width() / 2, r.y() + 2))
    assert w._seeking is not None and abs(w._seeking - 0.5) < 0.02
    w.mouseReleaseEvent(None)
    time.sleep(0.3)                        # _do runs on a worker thread
    assert ("next", None) in pressed, pressed
    assert ("seek", 50_000) in [(k, a) for k, a in pressed], pressed
    assert w._seeking is None
    w.close()


def test_close_button_quits():
    if triolfm is None:
        return
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QMouseEvent

    app, w = live_island()
    w.track, w.dur = "t1", 100_000
    w.enterEvent(None)
    settle(app, w)
    quit_calls = []
    w.quit = lambda: quit_calls.append(True)
    w.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, w.close_rect().center(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    assert quit_calls
    w.close()


def test_login_without_client_id():
    # No ID and nothing to ask: a dialog-worthy AuthError, not an input()
    # prompt at a terminal that a .desktop launch doesn't have.
    try:
        be.login({}, lambda: "")
    except be.AuthError as e:
        assert be.REDIRECT in str(e), e   # tells them the exact URI
    else:
        assert False, "no AuthError"


def test_catch_code_port_busy():
    import socket
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", 8888))
        s.listen(1)
    except OSError:                     # already busy: the point is proven
        pass
    try:
        be._catch_code("http://example.invalid", "state")
    except be.AuthError as e:
        assert "8888" in str(e), e
    else:
        assert False, "no AuthError"
    finally:
        s.close()


def test_save_cfg():
    keep = be.CFG_PATH
    try:
        d = tempfile.mkdtemp()
        be.CFG_PATH = os.path.join(d, "triolfm", "config.json")
        be.save_cfg({"refresh": "tok"})       # creates the directory
        assert json.load(open(be.CFG_PATH)) == {"refresh": "tok"}
        # the file holds a refresh token: never group- or world-readable
        assert stat.S_IMODE(os.stat(be.CFG_PATH).st_mode) == 0o600
        assert not os.path.exists(be.CFG_PATH + ".tmp")  # no litter
        be.save_cfg({"refresh": "tok2"})      # overwrite in place
        assert json.load(open(be.CFG_PATH))["refresh"] == "tok2"
    finally:
        be.CFG_PATH = keep


def test_open_collapses_when_pointer_left_without_a_leave_event():
    # X11 drops the odd LeaveNotify when the mask reshapes under the pointer
    # mid-morph, which left the island stuck open until the next hover.
    if triolfm is None:
        return
    from PySide6.QtCore import QPoint

    app, w = live_island()
    w.enterEvent(None)
    settle(app, w)
    assert w.state == "open"
    away = QPoint(0, 0)                     # masked out: click-through window
    w.cursor = lambda: types.SimpleNamespace(pos=lambda: away)
    w.frame()                               # no leaveEvent ever arrives
    settle(app, w)
    assert w.state == "closed" and abs(w.cur.height() - triolfm.CLOSED_H) < 0.5
    w.close()


def test_halo_is_hittable_but_does_not_count_as_hovering():
    # WSLg hands the surface to Windows as a per-pixel-alpha layered window and
    # hit-tests it by alpha, so alpha 0 pixels never deliver a pointer event at
    # all: no leave, no motion, QCursor frozen on the pill's last pixel, island
    # stuck open on every sideways exit. The halo is an alpha-1 ring that keeps
    # the pointer ours for a few more pixels. It only works if it is masked in
    # (or it is not painted, so it is alpha 0 again) AND reads as not-hovered
    # (or the island just hangs open on the halo instead of on the pill).
    if triolfm is None:
        return
    app, w = live_island()
    w.enterEvent(None)
    settle(app, w)
    assert w.state == "open"
    pill, mask = w.pill_region(), w.mask().boundingRect()
    assert mask.left() <= pill.left() - triolfm.HALO, (mask, pill)
    assert mask.right() >= pill.right() + triolfm.HALO, (mask, pill)
    assert mask.bottom() >= pill.bottom() + triolfm.HALO, (mask, pill)
    # a pointer in the halo on any free edge has to read as gone, not hovering
    from PySide6.QtCore import QPoint
    for pt in (QPoint(pill.left() - 2, pill.center().y()),
               QPoint(pill.right() + 2, pill.center().y()),
               QPoint(pill.center().x(), pill.bottom() + 2)):
        assert mask.contains(pt), pt          # painted, so alpha 1, so hittable
        w.cursor = lambda p=pt: types.SimpleNamespace(pos=lambda: w.mapToGlobal(p))
        assert not w.hovered(), pt            # but off the pill
    w.frame()
    settle(app, w)
    assert w.state == "closed", w.state
    w.close()


def test_halo_is_invisible_but_not_transparent():
    # alpha 0 would be click-through and would not deliver pointer events;
    # anything much higher would be a visible grey rim around the island
    if triolfm is None:
        return
    from PySide6.QtGui import QImage
    app, w = live_island()
    w.enterEvent(None)
    settle(app, w)
    pill = w.pill_region()
    w.clearMask()          # render() clips to the mask but drops its offset
    img = QImage(w.size(), QImage.Format.Format_ARGB32)
    img.fill(0)
    w.render(img)
    w._sync_mask()
    y = pill.center().y()
    assert img.pixelColor(pill.center().x(), y).alpha() == 255   # the pill
    for x in (pill.left() - 2, pill.right() + 2, w.width() - 1):
        assert img.pixelColor(x, y).alpha() == 1, (x, img.pixelColor(x, y))
    assert img.pixelColor(pill.center().x(), pill.bottom() + 2).alpha() == 1
    w.close()


if __name__ == "__main__":
    if triolfm is None:
        print("note: PySide6 missing, Island tests skipped")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
