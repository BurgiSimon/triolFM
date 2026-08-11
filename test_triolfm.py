"""Self-check for the bits that silently break: PKCE, ellipsizing, progress.

Stubs tkinter/PIL so this runs headless without the GUI deps installed.
"""
import base64
import hashlib
import sys
import types

for name in ("tkinter", "tkinter.font", "PIL", "PIL.Image", "PIL.ImageDraw",
             "PIL.ImageTk"):
    sys.modules.setdefault(name, types.ModuleType(name))

from triolfm import (HOLD, corner_rows, elapsed, fit, fmt, marquee_step,
                     parse_body, pkce)


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


def test_fmt():
    assert fmt(0) == "0:00"
    assert fmt(102000) == "1:42"        # the scrubber's left-hand label
    assert fmt(240000) == "4:00"
    assert fmt(9000) == "0:09"          # seconds always two digits
    assert fmt(-5) == "0:00"            # clamped, never "-1:59"
    assert fmt(3600000) == "60:00"      # minutes just keep counting


def test_corner_rows():
    w, h, r = 40, 20, 6
    rows = corner_rows(w, h, r)
    # every rectangle stays inside the window
    for x, y, rw, rh in rows:
        assert 0 <= x and x + rw <= w and 0 <= y and y + rh <= h, (x, y, rw, rh)
    # the straight middle spans the full width; corner rows are inset
    assert (0, r, w, h - 2 * r) in rows
    widths = {y: rw for x, y, rw, rh in rows if rh == 1}
    assert widths[0] < widths[r - 1] <= w                # curve opens outward
    assert widths[0] == widths[h - 1]                    # top/bottom symmetric
    # degenerate radii don't produce negative or overlapping geometry
    assert corner_rows(10, 10, 0) == [(0, 0, 10, 10)]
    assert all(rw > 0 and rh > 0 for _, _, rw, rh in corner_rows(8, 8, 99))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
