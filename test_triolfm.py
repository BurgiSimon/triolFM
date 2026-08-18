"""Self-check for the bits that silently break: PKCE, ellipsizing, progress.

Stubs tkinter/PIL so this runs headless without the GUI deps installed.
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
import types

for name in ("tkinter", "tkinter.font", "tkinter.messagebox",
             "tkinter.simpledialog", "PIL", "PIL.Image", "PIL.ImageTk"):
    sys.modules.setdefault(name, types.ModuleType(name))

import triolfm
from triolfm import (ACCENT, BG, HOLD, backoff, dominant, elapsed, fit,
                     marquee_step, mix, mmss, parse_body, pkce, shade,
                     theme)


class FakeFont:
    """7 px per character."""
    def measure(self, s):
        return len(s) * 7


def test_pkce():
    v, c = pkce()
    # challenge must be exactly S256(verifier), base64url, unpadded
    want = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest())
    assert c == want.rstrip(b"=").decode(), c
    assert "=" not in v + c and "+" not in v + c and "/" not in v + c
    assert 43 <= len(v) <= 128, len(v)   # RFC 7636 length bounds
    assert pkce()[0] != v                # fresh verifier each call


def test_fit():
    f = FakeFont()
    assert fit(f, "short", 70) == "short"          # fits, untouched
    assert fit(f, "abcdefghij", 70) == "abcdefghij"  # exactly 70px
    out = fit(f, "abcdefghijklmnop", 70)
    assert out.endswith("…") and f.measure(out) <= 70, out
    assert fit(f, "", 70) == ""
    assert fit(f, "xxxxx", 0) == "…"               # degenerate width


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


def fake_widget():
    """A Widget with just enough state for apply(): no Tk, no network."""
    w = triolfm.Widget.__new__(triolfm.Widget)
    w.track = w.art_url = w._vol_job = None
    w.playing, w.dur, w.pos, w.volume, w.msg = False, 1, 0, 50, "connecting…"
    w.stamp, w._year, w.info, w._shown = 0.0, "", ("", ""), None
    w.fetch_art = lambda url: None
    w._text = lambda: None
    w.lbl_title = types.SimpleNamespace(config=lambda **kw: None)
    return w


def test_apply_track():
    w = fake_widget()
    w.apply(TRACK)
    assert w.track == "t1" and w.msg is None
    assert w.info == ("Song", "A, B"), w.info
    assert w._year == "1998" and w.dur == 200_000 and w.playing
    assert w.art_url == "small"          # smallest image, not the first
    assert w.volume == 33


def test_apply_episode():
    w = fake_widget()
    w.apply(EPISODE)
    assert w.info == ("Ep 1", "") and w._year == "2024"
    assert w.art_url == "epsmall"        # art on the item, not on an album
    assert w.volume == 50                # device: null must not wipe it
    assert not w.playing and w.pos == 0  # no progress_ms in the payload


def test_apply_local_file():
    w = fake_widget()
    w.apply(LOCAL)
    assert w.track == "spotify:local:x"  # no id: fall back to the uri
    assert w.dur == 1                    # never 0 — the progress bar divides
    assert w.art_url is None and w._year == ""


def test_apply_nothing_playing():
    for payload in (None, {}, {"item": None}):
        w = fake_widget()
        w.apply(payload)
        assert w.track is None and not w.playing, payload
        assert w.msg == "nothing playing", payload


def test_apply_keeps_pending_volume():
    w = fake_widget()
    w._vol_job, w.volume = "pending", 70
    w.apply(TRACK)
    assert w.volume == 70  # a scroll we haven't sent yet outranks the poll


def test_onscreen():
    w = triolfm.Widget.__new__(triolfm.Widget)
    w.W, w.H = 340, 104
    w.root = types.SimpleNamespace(winfo_screenwidth=lambda: 1920,
                                   winfo_screenheight=lambda: 1080)
    assert w.onscreen(100, 100) == (100, 100)      # already visible: untouched
    assert w.onscreen(5000, 5000) == (1580, 976)   # monitor unplugged
    assert w.onscreen(-50, -9) == (0, 0)           # off the top-left corner


def test_ask_cid_hands_off_to_main_thread():
    w = triolfm.Widget.__new__(triolfm.Widget)
    w.q, w._declined = queue.Queue(), False
    out = []

    def asker():
        out.append(w.ask_cid())

    t = threading.Thread(target=asker)   # stands in for the poller thread
    t.start()
    kind, box = w.q.get(timeout=5)       # what tick() sees on the main thread
    assert kind == "ask"
    box.put("ABC")
    t.join(5)
    assert out == ["ABC"] and not w._declined

    t = threading.Thread(target=asker)   # this time the user dismisses it
    t.start()
    w.q.get(timeout=5)[1].put("")
    t.join(5)
    assert w._declined
    # latched: the poller retries forever, and must not reopen a dialog each time
    assert w.ask_cid() == "" and w.q.empty()


def test_login_without_client_id():
    # No ID and nothing to ask: a dialog-worthy AuthError, not an input()
    # prompt at a terminal that a .desktop launch doesn't have.
    try:
        triolfm.login({}, lambda: "")
    except triolfm.AuthError as e:
        assert triolfm.REDIRECT in str(e), e   # tells them the exact URI
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
        triolfm._catch_code("http://example.invalid", "state")
    except triolfm.AuthError as e:
        assert "8888" in str(e), e
    else:
        assert False, "no AuthError"
    finally:
        s.close()


def test_save_cfg():
    keep = triolfm.CFG_PATH
    try:
        d = tempfile.mkdtemp()
        triolfm.CFG_PATH = os.path.join(d, "triolfm", "config.json")
        triolfm.save_cfg({"refresh": "tok"})       # creates the directory
        assert json.load(open(triolfm.CFG_PATH)) == {"refresh": "tok"}
        # the file holds a refresh token: never group- or world-readable
        assert stat.S_IMODE(os.stat(triolfm.CFG_PATH).st_mode) == 0o600
        assert not os.path.exists(triolfm.CFG_PATH + ".tmp")  # no litter
        triolfm.save_cfg({"refresh": "tok2"})      # overwrite in place
        assert json.load(open(triolfm.CFG_PATH))["refresh"] == "tok2"
    finally:
        triolfm.CFG_PATH = keep


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
