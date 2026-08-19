#!/usr/bin/env python3
"""triolFM backend — Spotify auth, Web API calls and the pure helpers.

No GUI toolkit imports live here: the Dynamic Island frontend in triolfm.py
owns all the drawing, this file owns everything it talks to.
"""

import base64
import colorsys
import hashlib
import math
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from io import BytesIO

from PIL import Image

__version__ = "2.0.0"

CFG_PATH = os.path.expanduser("~/.config/triolfm/config.json")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state"
POLL = 3.0            # seconds between /me/player reads; overridable in config
AUTH_TIMEOUT = 300.0  # give up waiting for the OAuth callback after 5 min
FAST_POLL, FAST_POLLS = 0.5, 2   # re-checks after a control press, see backoff
SCROLL_MS, HOLD = 60, 12   # marquee: 1px per 60ms, ~0.7s pause at each end

BG, FG, DIM, ACCENT = "#121212", "#ffffff", "#8a8a8a", "#1db954"
SURFACE = "#2a2a2a"   # art placeholder / progress trough
# ------------------------------------------------------------------- colors

def dominant(img):
    """(r, g, b) of the color a person would name when shown `img`.

    The mean pixel of album art is nearly always mud, so quantize instead and
    score palette entries by area × saturation, penalizing near-black and
    near-white — those cover most of the frame but read as "no color".
    """
    pal = img.convert("RGB").resize((64, 64)).quantize(colors=8)
    raw, best, top = pal.getpalette(), (110, 110, 110), -1.0
    for count, i in pal.getcolors():
        rgb = tuple(raw[i * 3:i * 3 + 3])
        _, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
        score = count * (s ** 2 + 0.02) * max(0.0, 1 - abs(l - 0.5) * 1.7)
        if score > top:
            best, top = rgb, score
    return best


def shade(rgb, light, smin=0.0, smax=1.0):
    """`rgb`'s hue at lightness `light`, saturation clamped to [smin, smax]."""
    h, _, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    if s > 0.08:  # a near-gray cover has no hue worth boosting — stay neutral
        s = max(smin, min(smax, s))
    out = colorsys.hls_to_rgb(h, light, s)
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in out)


def mix(a, b, t):
    """The hex color `t` of the way from hex `a` to hex `b`."""
    return "#%02x%02x%02x" % tuple(
        round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t)
        for i in (1, 3, 5))


def theme(rgb):
    """(bg, surface, accent) for an album color, or the defaults for None."""
    if rgb is None:
        return BG, SURFACE, ACCENT
    return (shade(rgb, 0.07, 0, 0.55),   # background: barely-there tint
            shade(rgb, 0.17, 0, 0.40),
            shade(rgb, 0.48, 0.55))      # accent: forced vivid, always legible

# ---------------------------------------------------------------- auth / api

def load_cfg():
    try:
        with open(CFG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cfg(cfg):
    """Write the config atomically.

    It holds the refresh token, so a crash mid-write costs the user a full
    re-login. 0600 from creation, so the token is never briefly world-readable.
    """
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                   "w") as f:
        json.dump(cfg, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CFG_PATH)


class AuthError(RuntimeError):
    """A setup problem the user has to fix, and can.

    Raised instead of a bare error so the widget knows to show a dialog: these
    messages don't fit the status line, and a truncated one helps nobody.
    """


def _post_token(data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", body,
        {"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _catch_code(url, state):
    """One-shot loopback server that captures the OAuth redirect."""
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<body style='background:#121212;color:#1db954;"
                b"font:16px sans-serif;padding:3em'>Authorized. "
                b"You can close this tab.</body>")

        def log_message(self, *a):
            pass

    try:
        srv = http.server.HTTPServer(("127.0.0.1", 8888), H)
    except OSError:
        raise AuthError(
            "triolFM needs port 8888 to finish the Spotify login, and "
            "something else is using it.\n\nClose that program (another copy "
            "of triolFM?) and try again.") from None
    srv.timeout = 1  # so the wait below can give up instead of hanging forever
    webbrowser.open(url)
    print("If no browser opened, visit:\n" + url)
    # AuthError, not SystemExit: this runs in a worker thread, where SystemExit
    # just ends the thread and nobody hears about it
    try:
        deadline = time.monotonic() + AUTH_TIMEOUT
        while not got:
            if time.monotonic() > deadline:
                raise AuthError(
                    "Spotify never sent the login back to triolFM.\n\n"
                    "Check that your Client ID is correct, and that your app's "
                    "redirect URI is exactly:\n" + REDIRECT +
                    "\n\n(127.0.0.1, not localhost — Spotify rejects that.)\n\n"
                    "Fix it, then use \u2699 \u2192 Reconnect to Spotify.")
            srv.handle_request()  # favicon etc. leave `got` empty, so we loop
    finally:
        srv.server_close()  # always free the port, even on timeout
    if got.get("state") != state:
        raise AuthError("The Spotify login came back mismatched and was "
                        "discarded. Try connecting again.")
    if "code" not in got:
        raise AuthError("Spotify refused the login: " +
                        got.get("error", "unknown") +
                        "\n\nUse \u2699 \u2192 Reconnect to Spotify to retry.")
    return got["code"]


def parse_body(ctype, body):
    """Spotify's playback endpoints answer 200/202/204 with empty or non-JSON
    bodies, so status alone doesn't tell us whether there's JSON to read."""
    if not body or "json" not in ctype:
        return None
    return json.loads(body)


def pkce():
    """RFC 7636 S256 pair: (code_verifier, code_challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(cfg, ask):
    """PKCE authorization-code flow. Returns (access_token, expiry_epoch).

    `ask` supplies a Client ID when the config has none. It is a callback
    because this runs in the poller thread, where input() has no terminal
    under a .desktop launch — the widget hands us a main-thread dialog.
    """
    cid = (cfg.get("client_id") or os.environ.get("SPOTIFY_CLIENT_ID")
           or ask() or "").strip()
    if not cid:
        raise AuthError(
            "triolFM needs a Spotify Client ID before it can connect.\n\n"
            "1. Open https://developer.spotify.com/dashboard and create an "
            "app.\n"
            "2. Tick Web API, and add this redirect URI exactly:\n"
            "   " + REDIRECT + "\n"
            "3. Copy the Client ID.\n\n"
            "Then use \u2699 \u2192 Reconnect to Spotify and paste it in.")
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES, "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    })
    code = _catch_code(url, state)
    try:
        tok = _post_token({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "client_id": cid,
            "code_verifier": verifier,
        })
    except urllib.error.HTTPError as e:
        raise AuthError(
            "Spotify rejected the login (HTTP %d).\n\nCheck that the Client "
            "ID is right and that the redirect URI on your app is exactly:\n%s"
            "\n\nThen use \u2699 \u2192 Reconnect to Spotify."
            % (e.code, REDIRECT)) from e
    cfg["client_id"] = cid
    cfg["refresh"] = tok["refresh_token"]
    save_cfg(cfg)
    return tok["access_token"], time.time() + tok["expires_in"]


class Spotify:
    def __init__(self, ask=None):
        self.cfg = load_cfg()
        self.token, self.exp = None, 0.0
        # where a Client ID comes from when the config has none; the widget
        # swaps in a main-thread dialog, since _tok() runs off the main thread
        self.ask = ask or (lambda: input("Spotify Client ID: "))

    def _tok(self):
        if self.token and time.time() < self.exp - 30:
            return self.token
        if self.cfg.get("refresh"):
            try:
                t = _post_token({
                    "grant_type": "refresh_token",
                    "refresh_token": self.cfg["refresh"],
                    "client_id": self.cfg["client_id"],
                })
                self.token = t["access_token"]
                self.exp = time.time() + t["expires_in"]
                if t.get("refresh_token"):
                    self.cfg["refresh"] = t["refresh_token"]
                    save_cfg(self.cfg)
                return self.token
            except urllib.error.HTTPError:
                self.cfg.pop("refresh", None)  # revoked — fall through to login
        self.token, self.exp = login(self.cfg, self.ask)
        return self.token

    def call(self, method, path, **params):
        url = "https://api.spotify.com/v1" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = b"" if method in ("PUT", "POST") else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": "Bearer " + self._tok()})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return parse_body(r.headers.get("Content-Type", ""), r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.exp = 0  # force refresh on next call
            raise

def elapsed(pos, stamp, dur, playing, t):
    """Interpolated playback position (ms) at monotonic time `t`."""
    return min(dur, pos + (t - stamp) * 1000 if playing else pos)


def mmss(ms):
    """`ms` as m:ss."""
    s = max(0, int(ms)) // 1000
    return f"{s // 60}:{s % 60:02d}"


def backoff(fails, poll, retry_after=None, urgent=0):
    """Seconds until the next poll.

    `urgent` counts the fast re-checks still owed after a control press:
    /me/player reports the previous track for a moment after a skip, so the
    poll that a press triggers often reads stale. Following it up twice at
    FAST_POLL catches the real state whatever the configured rate is —
    otherwise a 30s rate means the title is wrong for 30s. Errors outrank it;
    a rate-limited server does not want a burst.
    """
    if retry_after:
        return float(retry_after)
    if fails:
        return min(60.0, poll * 2 ** fails)
    return min(poll, FAST_POLL) if urgent else poll

def marquee_step(x, step, hold, over):
    """Next (x, step, hold) for a title bouncing between 0 and -over px."""
    if hold:
        return x, step, hold - 1
    x += step
    if x <= -over or x >= 0:
        return max(-over, min(0, x)), -step, HOLD
    return x, step, 0



def fetch_art(url):
    """Cover art at `url` as a PIL image, or None if it can't be had."""
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            img = Image.open(BytesIO(r.read()))
            img.load()
        return img
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------- easing

def spring(t):
    """Underdamped spring: fast rise, ~12% overshoot, settled by t=1."""
    return 1.0 if t >= 1.0 else 1 - math.exp(-6.0 * t) * math.cos(9.0 * t)


def lerp(a, b, t):
    return a + (b - a) * t


def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x
