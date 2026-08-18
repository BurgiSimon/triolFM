#!/usr/bin/env python3
"""triolFM — always-on-top Spotify remote widget.

Does not play audio. Talks to the Spotify Web API and drives whatever device
your Spotify app is already playing on.
"""

import base64
import colorsys
import ctypes
import hashlib
import http.server
import json
import os
import queue
import secrets
import sys
import threading
import time
import tkinter as tk
import tkinter.font
import tkinter.messagebox
import tkinter.simpledialog
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from io import BytesIO

from PIL import Image, ImageTk

__version__ = "1.0.0"

CFG_PATH = os.path.expanduser("~/.config/triolfm/config.json")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state"
POLL = 3.0            # seconds between /me/player reads; overridable in config
AUTH_TIMEOUT = 300.0  # give up waiting for the OAuth callback after 5 min
FAST_POLL, FAST_POLLS = 0.5, 2   # re-checks after a control press, see backoff
SCROLL_MS, HOLD = 60, 12   # marquee: 1px per 60ms, ~0.7s pause at each end
FADE, FADE_MS = 8, 40      # recolor crossfade: 8 steps of 40ms

W, H, ART, PAD = 340, 104, 76, 8  # at scale 1.0; settings scales these

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


# ------------------------------------------------------------------- widget

def _x11_undecorate(root):
    """Ask the window manager for no decorations via _MOTIF_WM_HINTS.

    Set while withdrawn, walking up to the child-of-root window, so the WM
    reads it when it maps us. libX11 is already loaded by Tk; ctypes avoids
    needing xprop or python-xlib.
    """
    x = ctypes.CDLL("libX11.so.6")
    ulong, void = ctypes.c_ulong, ctypes.c_void_p
    x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = void, [ctypes.c_char_p]
    x.XInternAtom.restype = ulong
    x.XInternAtom.argtypes = [void, ctypes.c_char_p, ctypes.c_int]
    x.XChangeProperty.argtypes = [void, ulong, ulong, ulong, ctypes.c_int,
                                  ctypes.c_int, void, ctypes.c_int]
    x.XQueryTree.argtypes = [void, ulong, void, void, void, void]
    x.XFlush.argtypes = x.XCloseDisplay.argtypes = [void]

    dpy = x.XOpenDisplay(None)
    if not dpy:
        return
    atom = x.XInternAtom(dpy, b"_MOTIF_WM_HINTS", False)
    # flags=MWM_HINTS_DECORATIONS, functions, decorations=0, input_mode, status
    hints = (ulong * 5)(2, 0, 0, 0, 0)

    root.withdraw()
    root.update()
    xroot, parent, kids, n = ulong(), ulong(), ctypes.POINTER(ulong)(), ctypes.c_uint()
    wid = root.winfo_id()
    for _ in range(8):  # Tk wraps toplevels; hint every window up to root's child
        x.XChangeProperty(dpy, wid, atom, atom, 32, 0, ctypes.byref(hints), 5)
        if not x.XQueryTree(dpy, wid, ctypes.byref(xroot), ctypes.byref(parent),
                            ctypes.byref(kids), ctypes.byref(n)):
            break
        if kids:
            x.XFree(kids)
        if parent.value in (0, xroot.value):
            break
        wid = parent.value
    x.XFlush(dpy)
    x.XCloseDisplay(dpy)
    root.deiconify()


def frameless(root):
    """Strip the title bar.

    overrideredirect is the usual trick, but WSLg's compositor never forwards
    override-redirect windows to the Windows desktop — the app runs invisibly.
    On X11 stay WM-managed and ask the WM to skip decorations instead.
    """
    if root.tk.call("tk", "windowingsystem") == "x11":
        _x11_undecorate(root)
    else:
        root.overrideredirect(True)


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


def fit(font, text, px):
    """Ellipsize `text` to fit `px` pixels."""
    if font.measure(text) <= px:
        return text
    while text and font.measure(text + "…") > px:
        text = text[:-1]
    return text + "…"


def marquee_step(x, step, hold, over):
    """Next (x, step, hold) for a title bouncing between 0 and -over px."""
    if hold:
        return x, step, hold - 1
    x += step
    if x <= -over or x >= 0:
        return max(-over, min(0, x)), -step, HOLD
    return x, step, 0


class Widget:
    def __init__(self):
        self.sp = Spotify(self.ask_cid)
        self.q = queue.Queue()
        self.wake = threading.Event()
        self.track = None          # current track id
        self.art_url = None
        self.playing = False
        self.pos = 0               # ms at self.stamp
        self.dur = 1
        self.stamp = time.monotonic()
        self.volume = 50
        self.poll = float(self.sp.cfg.get("poll", POLL))
        self.msg = "connecting…"
        self.alive = True
        self.urgent = 0            # fast re-polls owed after a press
        self._declined = False     # user closed the Client ID prompt
        self._shown_fatal = None   # last setup error already shown in a dialog
        self._art_ref = None
        self._art_img = None       # last PIL image, re-resized on rescale
        self.info = ("", "")       # raw title/artist, re-ellipsized on rescale
        self._year = ""
        self._shown = None         # self.info currently in the labels
        self._job = None           # pending marquee callback
        self._vol_job = None       # pending volume PUT
        self.bg, self.surface, self.accent = self._target = theme(None)
        self._fade = None          # pending crossfade callback
        self._build()

    # -- ui ---------------------------------------------------------------

    def _dims(self):
        s = self.scale
        self.W, self.H, self.ART, self.PAD = (
            round(v * s) for v in (W, H, ART, PAD))

    def _build(self):
        cfg = self.sp.cfg
        self.scale = float(cfg.get("scale", 1.0))
        self._dims()
        r = self.root = tk.Tk()
        r.title("triolFM")
        r.configure(bg=self.bg)
        r.geometry(f"{self.W}x{self.H}")
        frameless(r)  # after geometry: the remap shouldn't move the widget
        r.attributes("-topmost", True)
        # No saved position: let the WM place us. Blind guesses like +80+80 can
        # land off the usable area on multi-monitor setups and get clamped.
        self.moved = False
        if cfg.get("pos"):
            r.after(2000, lambda: self.place(*self.onscreen(*cfg["pos"])))

        self.rate = tk.DoubleVar(value=self.poll)
        self.size = tk.IntVar(value=round(self.scale * 100))
        self.scroll = tk.BooleanVar(value=cfg.get("scroll", True))
        self.year = tk.BooleanVar(value=cfg.get("year", True))
        self.win = None
        # scroll anywhere = volume; bind_all is app-wide, so only once
        r.bind_all("<Button-4>", lambda e: self.press("vol+"))
        r.bind_all("<Button-5>", lambda e: self.press("vol-"))
        r.bind_all("<MouseWheel>",
                   lambda e: self.press("vol+" if e.delta > 0 else "vol-"))
        self._build_ui()

    def _build_ui(self):
        """Everything sized by self.scale. Torn down and re-run on rescale."""
        s = self.scale
        W, H, ART, PAD = self.W, self.H, self.ART, self.PAD
        BG, ACCENT = self.bg, self.accent  # album-tinted; shadow the defaults
        body = self.body = tk.Frame(self.root, bg=BG)
        body.place(x=0, y=0, relwidth=1, relheight=1)

        px = lambda n: max(6, round(n * s))
        self.f_title = tkinter.font.Font(font=("TkDefaultFont", px(10), "bold"))
        self.f_sub = tkinter.font.Font(font=("TkDefaultFont", px(9)))
        f_ctl = tkinter.font.Font(font=("TkDefaultFont", px(11)))

        tx = PAD + ART + round(12 * s)
        tw = self.tw = W - tx - PAD
        bh = max(2, round(3 * s))
        # Fonts stop shrinking below ~6px, so at small scales a row is taller
        # than its share of H. Stack the rows on measured line heights and grow
        # the widget to fit, instead of letting the ×/⚙ row sit on the title.
        th, sh = self.f_title.metrics("linespace"), self.f_sub.metrics("linespace")
        ico = f_ctl.metrics("linespace")
        ty = round(3 * s) + ico                 # title, below the icon row
        ay = ty + th + round(4 * s)             # artist
        cy = ay + sh + round(5 * s)             # transport controls
        H = self.H = max(H, cy + ico + bh + round(8 * s))
        self.root.geometry(f"{W}x{H}")

        self.art = tk.Label(body, bg=self.surface, bd=0)
        self.art.place(x=PAD, y=(H - bh - ART) // 2, width=ART, height=ART)
        if self._art_img:
            self._art_ref = ImageTk.PhotoImage(
                self._art_img.resize((ART, ART), Image.LANCZOS))
            self.art.config(image=self._art_ref)

        # the frame clips the label, so an over-wide title can slide behind it
        clip = tk.Frame(body, bg=BG)
        clip.place(x=tx, y=ty, width=tw, height=th)
        self.lbl_title = tk.Label(clip, bg=BG, fg=DIM if self.msg else FG, bd=0,
                                  highlightthickness=0, pady=0,
                                  font=self.f_title, anchor="w")
        self.lbl_title.place(x=0, y=0)
        self.lbl_artist = tk.Label(body, bg=BG, fg=DIM, bd=0, pady=0,
                                   highlightthickness=0, font=self.f_sub,
                                   anchor="w")
        self.lbl_artist.place(x=tx, y=ay, width=tw)
        self._text()

        ctl = tk.Frame(body, bg=BG)
        ctl.place(x=tx - round(6 * s), y=cy)
        self.btns = {}
        for key, glyph in (("prev", "◀◀"), ("play", "▶"),
                           ("next", "▶▶")):
            b = tk.Label(ctl, text=glyph, bg=BG, fg=DIM, font=f_ctl, bd=0,
                         highlightthickness=0, pady=0, padx=round(8 * s),
                         cursor="hand2")
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self.press(k))
            b.bind("<Enter>", lambda e, w=b: w.config(fg=FG))
            b.bind("<Leave>", lambda e, w=b: w.config(fg=DIM))
            self.btns[key] = b

        for i, (glyph, cmd) in enumerate((("×", self.quit),
                                          ("⚙", self.settings))):
            icon = tk.Label(body, text=glyph, bg=BG, fg="#3a3a3a", bd=0,
                            highlightthickness=0, padx=0, pady=0, font=f_ctl,
                            cursor="hand2")
            icon.place(x=W - round(20 * s) * (i + 1), y=round(2 * s))
            icon.bind("<Button-1>", lambda e, c=cmd: c())
            icon.bind("<Enter>", lambda e, w=icon: w.config(fg=FG))
            icon.bind("<Leave>", lambda e, w=icon: w.config(fg="#3a3a3a"))

        self.lbl_time = tk.Label(body, bg=BG, fg=DIM, bd=0, pady=0,
                                 highlightthickness=0, font=self.f_sub,
                                 anchor="e")
        self.lbl_time.place(x=W - PAD, y=cy + (ico - sh) // 2, anchor="ne")

        self.bar = tk.Canvas(body, bg=self.surface, height=bh, bd=0,
                             highlightthickness=0, cursor="hand2")
        self.bar.place(x=0, y=H - bh, width=W)
        self.fill = self.bar.create_rectangle(0, 0, 0, bh, fill=ACCENT, width=0)
        self.bar.bind("<Button-1>", self.seek)
        self._bh = bh

        self.menu = tk.Menu(body, tearoff=0, bg=self.surface, fg=FG, bd=0,
                            activebackground=ACCENT, activeforeground="#000")
        self.menu.add_command(label="Settings", command=self.settings)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        for w in (body, self.art, clip, self.lbl_title, self.lbl_artist, ctl,
                  self.lbl_time):
            w.bind("<Button-1>", self.grab)
            w.bind("<B1-Motion>", self.drag)
            w.bind("<Button-3>",
                   lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.render_play()

    def _text(self):
        """Push self.info into the labels, then (re)start the marquee."""
        if self._job:
            self.root.after_cancel(self._job)
            self._job = None
        title, artist = self._shown = self.info
        if self.year.get() and self._year:
            artist = f"{artist} · {self._year}" if artist else self._year
        self.lbl_artist.config(text=fit(self.f_sub, artist, self.tw))
        over = self.f_title.measure(title) - self.tw
        if over > 0 and self.scroll.get():
            self.lbl_title.config(text=title)
            self._marquee(0, -1, HOLD, over)
        else:
            self.lbl_title.config(text=fit(self.f_title, title, self.tw))
            self.lbl_title.place(x=0)

    def _marquee(self, x, step, hold, over):
        self.lbl_title.place(x=x)
        self._job = self.root.after(
            SCROLL_MS,
            lambda a=marquee_step(x, step, hold, over): self._marquee(*a, over))

    def _modal(self, fn, *a, **kw):
        """Run a modal dialog with -topmost off, or the widget covers it."""
        self.root.attributes("-topmost", False)
        try:
            return fn(*a, **kw)
        finally:
            self.root.attributes("-topmost", True)

    def ask_cid(self):
        """Get a Client ID from the user. Called by Spotify from the poller
        thread, so hand the prompt to the main thread and wait for it."""
        if self._declined:
            return ""  # asked once and dismissed — don't reopen it every retry
        box = queue.Queue(1)
        self.q.put(("ask", box))
        cid = box.get()  # tick() answers on the main thread
        self._declined = not cid
        return cid

    def reconnect(self):
        """Forget the login so the next poll asks for a Client ID again.

        The only way out of a wrong Client ID or a revoked token that doesn't
        involve editing ~/.config/triolfm/config.json by hand.
        """
        for k in ("client_id", "refresh"):
            self.sp.cfg.pop(k, None)
        save_cfg(self.sp.cfg)
        self.sp.token, self.sp.exp = None, 0.0
        self._declined, self._shown_fatal = False, None
        self.msg = "connecting…"
        if self.win and self.win.winfo_exists():
            self.win.destroy()
        self.wake.set()

    def settings(self):
        if self.win and self.win.winfo_exists():
            self.win.lift()
            return
        BG, ACCENT = self.bg, self.accent
        w = self.win = tk.Toplevel(self.root)
        w.title("triolFM settings")
        w.configure(bg=BG)
        w.attributes("-topmost", True)
        for text, var, lo, hi, res, done in (
                ("Refresh rate (seconds)", self.rate, 1, 30, 1, self.set_rate),
                ("Widget size (%)", self.size, 50, 250, 5, self.set_scale)):
            tk.Label(w, text=text, bg=BG, fg=DIM, anchor="w").pack(
                fill="x", padx=12, pady=(10, 0))
            sc = tk.Scale(w, from_=lo, to=hi, resolution=res, variable=var,
                          orient="horizontal", length=240, bg=BG, fg=FG,
                          troughcolor=self.surface, highlightthickness=0, bd=0,
                          activebackground=ACCENT)
            sc.pack(fill="x", padx=12, pady=(0, 6))
            # commit on release, not on every pixel of the drag
            sc.bind("<ButtonRelease-1>", lambda e, f=done: f())
        for text, var in (("Scroll long titles", self.scroll),
                          ("Show release year", self.year)):
            tk.Checkbutton(w, text=text, variable=var, command=self.set_flags,
                           bg=BG, fg=DIM, activebackground=BG,
                           activeforeground=FG, selectcolor=self.surface,
                           highlightthickness=0, bd=0, anchor="w").pack(
                fill="x", padx=8, pady=(0, 4))
        tk.Button(w, text="Reconnect to Spotify…", command=self.reconnect,
                  bg=self.surface, fg=FG, activebackground=ACCENT,
                  activeforeground="#000", highlightthickness=0, bd=0,
                  relief="flat", cursor="hand2").pack(fill="x", padx=12,
                                                      pady=(6, 12))

    def onscreen(self, x, y):
        """Clamp a saved position onto the current screen.

        Monitors get unplugged and resolutions change. A widget restored onto
        a screen that no longer exists is unreachable: it has no taskbar entry
        and no tray icon to bring it back.
        """
        return (max(0, min(int(x), self.root.winfo_screenwidth() - self.W)),
                max(0, min(int(y), self.root.winfo_screenheight() - self.H)))

    def place(self, x, y, steps=8):
        """Restore the saved position.

        weston drops a single move request (and Tk suppresses a repeat of the
        last one), but tracks a run of distinct moves — so walk there like a
        drag. Abandoned if the user grabs the widget first.
        """
        cx, cy = self.root.winfo_x(), self.root.winfo_y()
        path = [(cx + (x - cx) * i // steps, cy + (y - cy) * i // steps)
                for i in range(1, steps + 1)]

        def step(i=0):
            if self.moved or i >= len(path):
                return
            self.root.geometry("+%d+%d" % path[i])
            self.root.after(40, lambda: step(i + 1))

        step()

    def grab(self, e):
        self.moved = True
        self._drag = (e.x_root - self.root.winfo_x(),
                      e.y_root - self.root.winfo_y())

    def drag(self, e):
        dx, dy = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def seek(self, e):
        if self.track:
            self.press("seek", int(max(0.0, min(1.0, e.x / self.W)) * self.dur))

    def set_rate(self):
        self.poll = self.rate.get()
        self.sp.cfg["poll"] = self.poll
        save_cfg(self.sp.cfg)
        self.wake.set()  # apply now instead of after the current sleep

    def set_flags(self):
        self.sp.cfg["scroll"] = self.scroll.get()
        self.sp.cfg["year"] = self.year.get()
        save_cfg(self.sp.cfg)
        self._text()

    def set_scale(self):
        self.scale = self.size.get() / 100
        self.sp.cfg["scale"] = self.scale
        save_cfg(self.sp.cfg)
        self._dims()
        self.root.geometry(f"{self.W}x{self.H}")
        self.body.destroy()  # settings window is a sibling, so it survives
        self._build_ui()

    def quit(self):
        self.alive = False  # stop tick before the window goes away
        self.sp.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
        save_cfg(self.sp.cfg)
        self.root.destroy()

    # -- network ----------------------------------------------------------

    def press(self, key, arg=None):
        self._declined = False  # user is back and wants it working: ask again
        if key == "play":  # optimistic flip, poll will confirm
            self.playing = not self.playing
            self.pos, self.stamp = self.now(), time.monotonic()
            self.render_play()
        elif key.startswith("vol"):
            self.volume = max(0, min(100, self.volume + (5 if key == "vol+" else -5)))
            # one PUT per burst of wheel notches, not one per notch
            if self._vol_job:
                self.root.after_cancel(self._vol_job)
            self._vol_job = self.root.after(200, self._flush_vol)
            return
        threading.Thread(target=self._do, args=(key, arg), daemon=True).start()

    def _flush_vol(self):
        self._vol_job = None
        threading.Thread(target=self._do, args=("vol", None), daemon=True).start()

    def _do(self, key, arg):
        try:
            if key == "play":
                self.sp.call("PUT", "/me/player/" + ("play" if self.playing else "pause"))
            elif key == "next":
                self.sp.call("POST", "/me/player/next")
            elif key == "prev":
                self.sp.call("POST", "/me/player/previous")
            elif key == "seek":
                self.sp.call("PUT", "/me/player/seek", position_ms=arg)
            elif key.startswith("vol"):
                self.sp.call("PUT", "/me/player/volume", volume_percent=self.volume)
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
            # a skip or seek changes what the widget shows; volume doesn't
            self.urgent = FAST_POLLS
        self.wake.set()

    def poller(self):
        fails = 0
        while True:
            wait = self.poll
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
            except Exception as e:  # incl. a failed re-auth: report, never die
                fails += 1
                self.q.put(("err", "offline" if isinstance(e, OSError)
                            else str(e)[:40] or type(e).__name__))
                wait = backoff(fails, self.poll)
            self.wake.wait(wait)  # actions and rate changes wake us early
            self.wake.clear()

    def fetch_art(self, url):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                img = Image.open(BytesIO(r.read()))
                img.load()
            self.q.put(("art", (url, img)))
        except (OSError, ValueError):
            pass

    # -- render -----------------------------------------------------------

    def now(self):
        return elapsed(self.pos, self.stamp, self.dur, self.playing,
                       time.monotonic())

    def render_play(self):
        self.btns["play"].config(text="❚❚" if self.playing else "▶")

    def recolor(self, rgb):
        """Fade the live widget tree to the current album's color."""
        target = theme(rgb)
        if target == self._target:
            return
        if self._fade:
            self.root.after_cancel(self._fade)
        self._target, start = target, (self.bg, self.surface, self.accent)

        def step(i=1):
            self._paint(*(mix(a, b, i / FADE) for a, b in zip(start, target)))
            self._fade = self.root.after(FADE_MS, step, i + 1) if i < FADE else None

        step()

    def _paint(self, bg, surface, accent):
        self.bg, self.surface, self.accent = bg, surface, accent
        self.root.config(bg=self.bg)

        def walk(w):
            for c in w.winfo_children():
                if c is not self.art and c is not self.bar:
                    try:
                        c.config(bg=self.bg)
                    except tk.TclError:
                        pass  # not every widget takes -bg (e.g. Menu entries)
                walk(c)

        walk(self.root)
        self.art.config(bg=self.surface)
        self.bar.config(bg=self.surface)
        self.bar.itemconfig(self.fill, fill=self.accent)
        self.menu.config(bg=self.surface, activebackground=self.accent)

    def apply(self, d):
        if not d or not d.get("item"):
            self.track, self.playing, self.msg = None, False, "nothing playing"
            return
        it = d["item"]
        self.playing = bool(d.get("is_playing"))
        self.dur = max(1, it.get("duration_ms", 1))
        self.pos = d.get("progress_ms") or 0
        self.stamp = time.monotonic()
        if not self._vol_job:  # don't clobber a scroll we haven't sent yet
            self.volume = ((d.get("device") or {}).get("volume_percent")
                           or self.volume)
        self.msg = None
        self.track = it.get("id") or it.get("uri") or it.get("name")  # local: no id
        alb = it.get("album") or {}
        # albums date the release; podcast episodes carry release_date directly
        self._year = (alb.get("release_date") or it.get("release_date") or "")[:4]
        self.info = (it.get("name", ""),
                     ", ".join(a["name"] for a in it.get("artists", [])))
        if self.info != self._shown:
            self.lbl_title.config(fg=FG)
            self._text()
        # tracks carry art on the album, podcast episodes carry it directly
        imgs = alb.get("images") or it.get("images") or []
        url = imgs[-1]["url"] if imgs else None  # smallest available
        if url and url != self.art_url:
            self.art_url = url
            threading.Thread(target=self.fetch_art, args=(url,), daemon=True).start()

    def tick(self):
        if not self.alive:
            return
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
                payload.put(self._modal(
                    tkinter.simpledialog.askstring,
                    "triolFM", "Spotify Client ID:", parent=self.root) or "")
            elif kind == "fatal":
                self.msg = "setup needed"
                if payload != self._shown_fatal:  # once, not every retry
                    self._shown_fatal = payload
                    self._modal(tkinter.messagebox.showerror, "triolFM",
                                payload, parent=self.root)
            elif kind == "art" and payload[0] == self.art_url:
                self._art_img = payload[1]
                self._art_ref = ImageTk.PhotoImage(
                    self._art_img.resize((self.ART, self.ART), Image.LANCZOS))
                self.art.config(image=self._art_ref)
                self.recolor(dominant(self._art_img))

        if self.msg:
            self.info, self._year = (self.msg, ""), ""
            if self.info != self._shown:
                self.lbl_title.config(fg=DIM)
                self._text()
            self.art.config(image="")
            self._art_ref = self._art_img = self.art_url = None
            self.recolor(None)
        self.render_play()
        self.lbl_time.config(
            text=f"{mmss(self.now())} / {mmss(self.dur)}" if self.track else "")
        frac = self.now() / self.dur if self.track else 0
        self.bar.coords(self.fill, 0, 0, self.W * frac, self._bh)
        self.root.after(250, self.tick)

    def run(self):
        # No Client ID prompt here: the poller asks for one when it needs one,
        # via ask_cid() -> tick(), so a re-auth after a revoked token gets the
        # same dialog as the first run instead of an input() with no terminal.
        threading.Thread(target=self.poller, daemon=True).start()
        self.root.after(100, self.tick)
        self.root.mainloop()


if __name__ == "__main__":
    if "--version" in sys.argv[1:]:
        print("triolFM " + __version__)
    else:
        Widget().run()
