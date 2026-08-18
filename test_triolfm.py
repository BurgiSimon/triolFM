"""Self-check for the bits that silently break: PKCE, ellipsizing, progress.

Stubs tkinter/PIL so this runs headless without the GUI deps installed.
"""
import base64
import hashlib
import sys
import types

for name in ("tkinter", "tkinter.font", "tkinter.simpledialog", "PIL",
             "PIL.Image", "PIL.ImageTk"):
    sys.modules.setdefault(name, types.ModuleType(name))

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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
