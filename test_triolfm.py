"""Self-check for the bits that silently break: PKCE, ellipsizing, progress.

Stubs tkinter/PIL so this runs headless without the GUI deps installed.
"""
import base64
import hashlib
import sys
import types

for name in ("tkinter", "tkinter.font", "PIL", "PIL.Image", "PIL.ImageTk"):
    sys.modules.setdefault(name, types.ModuleType(name))

from triolfm import HOLD, elapsed, fit, marquee_step, parse_body, pkce


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
