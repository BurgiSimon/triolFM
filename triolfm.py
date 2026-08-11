#!/usr/bin/env python3
"""triolFM — always-on-top Spotify remote widget.

Does not play audio. Talks to the Spotify Web API and drives whatever device
your Spotify app is already playing on.
"""

import base64
import ctypes
import hashlib
import http.server
import json
import os
import queue
import secrets
import threading
import time
import tkinter as tk
import tkinter.font
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from io import BytesIO

from PIL import Image, ImageDraw, ImageTk

CFG_PATH = os.path.expanduser("~/.config/triolfm/config.json")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-private user-read-playback-state "
          "user-modify-playback-state user-library-read user-library-modify "
          "playlist-modify-private")
POLL = 3.0            # seconds between /me/player reads; overridable in config
SCROLL_MS, HOLD = 60, 12   # marquee: 1px per 60ms, ~0.7s pause at each end

W, H, ART, PAD, RADIUS = 372, 216, 56, 14, 16  # at scale 1.0; settings scales these

BG, FG, DIM, ACCENT = "#121212", "#ffffff", "#8a8a8a", "#1db954"
CARD, RAIL, ORANGE, INK = "#1e1e1e", "#b3b3b3", "#ff6a00", "#000000"

GLYPH = {"like": "♥", "add": "✚", "queue": "≡", "shuffle": "⇄", "prev": "◀◀",
         "next": "▶▶", "repeat": "↻", "lyrics": "♫", "playlist": "▤",
         "devices": "▭", "expand": "⇲"}


# ---------------------------------------------------------------- auth / api

def load_cfg():
    try:
        with open(CFG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cfg(cfg):
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    with open(CFG_PATH, "w") as f:
        json.dump(cfg, f)
    os.chmod(CFG_PATH, 0o600)


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

    srv = http.server.HTTPServer(("127.0.0.1", 8888), H)
    webbrowser.open(url)
    print("If no browser opened, visit:\n" + url)
    while not got:
        srv.handle_request()  # favicon etc. leave `got` empty, so we loop
    srv.server_close()
    if got.get("state") != state:
        raise SystemExit("OAuth state mismatch — aborting.")
    if "code" not in got:
        raise SystemExit("Auth failed: " + got.get("error", "unknown"))
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


def login(cfg):
    """PKCE authorization-code flow. Returns (access_token, expiry_epoch)."""
    cid = (cfg.get("client_id") or os.environ.get("SPOTIFY_CLIENT_ID")
           or input("Spotify Client ID: ").strip())
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES, "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    })
    code = _catch_code(url, state)
    tok = _post_token({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": cid, "code_verifier": verifier,
    })
    cfg["client_id"] = cid
    cfg["refresh"] = tok["refresh_token"]
    cfg["scopes"] = SCOPES
    save_cfg(cfg)
    return tok["access_token"], time.time() + tok["expires_in"]


class Spotify:
    def __init__(self):
        self.cfg = load_cfg()
        self.token, self.exp = None, 0.0

    def _tok(self):
        if self.token and time.time() < self.exp - 30:
            return self.token
        # a token minted before the scope list grew can't like or add tracks,
        # and Spotify won't widen it on refresh — re-authorize instead
        if self.cfg.get("refresh") and self.cfg.get("scopes") == SCOPES:
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
        self.token, self.exp = login(self.cfg)
        return self.token

    def call(self, method, path, body=None, **params):
        url = "https://api.spotify.com/v1" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = b"" if method in ("PUT", "POST") else None
        headers = {"Authorization": "Bearer " + self._tok()}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
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


def corner_rows(w, h, r):
    """Rectangles covering a w×h rounded rect: the straight middle plus one
    row per corner scanline. Fed to the X11 SHAPE extension, which takes
    rectangles, not curves."""
    r = max(0, min(r, w // 2, h // 2))
    rows = [(0, r, w, h - 2 * r)]
    for y in range(r):
        dx = r - round((r * r - (r - y - 0.5) ** 2) ** 0.5)
        rows += [(dx, y, w - 2 * dx, 1), (dx, h - 1 - y, w - 2 * dx, 1)]
    return [t for t in rows if t[2] > 0 and t[3] > 0]


class XRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


def round_window(root, w, h, r):
    """Clip the window to a rounded rect (X11 SHAPE). No-op where unavailable —
    square corners are cosmetic, not fatal."""
    if root.tk.call("tk", "windowingsystem") != "x11":
        return
    try:
        x, xext = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXext.so.6")
    except OSError:
        return
    void, ulong = ctypes.c_void_p, ctypes.c_ulong
    x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = void, [ctypes.c_char_p]
    x.XFlush.argtypes = x.XCloseDisplay.argtypes = [void]
    xext.XShapeCombineRectangles.argtypes = [
        void, ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(XRect), ctypes.c_int, ctypes.c_int, ctypes.c_int]
    dpy = x.XOpenDisplay(None)
    if not dpy:
        return
    rows = corner_rows(w, h, r)
    arr = (XRect * len(rows))(*(XRect(*map(int, t)) for t in rows))
    # kind=ShapeBounding, op=ShapeSet, ordering=Unsorted
    xext.XShapeCombineRectangles(dpy, root.winfo_id(), 0, 0, 0, arr, len(rows),
                                 0, 0)
    x.XFlush(dpy)
    x.XCloseDisplay(dpy)


def round_img(img, size, r):
    """Square, resized, rounded-corner cover art on the widget background."""
    small = img.convert("RGB").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1),
                                          min(r, size // 2), fill=255)
    out = Image.new("RGB", (size, size), BG)
    out.paste(small, (0, 0), mask)
    return out


def capsule(cv, x, y, w, h, color, tag):
    """Rounded-end bar on a canvas — used for both sliders."""
    if w <= 0:
        return
    if w <= h:  # too short for the caps: a shrinking dot
        cv.create_oval(x, y, x + w, y + h, fill=color, width=0, tags=tag)
        return
    cv.create_oval(x, y, x + h, y + h, fill=color, width=0, tags=tag)
    cv.create_oval(x + w - h, y, x + w, y + h, fill=color, width=0, tags=tag)
    cv.create_rectangle(x + h / 2, y, x + w - h / 2, y + h, fill=color, width=0,
                        tags=tag)


def elapsed(pos, stamp, dur, playing, t):
    """Interpolated playback position (ms) at monotonic time `t`."""
    return min(dur, pos + (t - stamp) * 1000 if playing else pos)


def fmt(ms):
    """Playback position as m:ss."""
    s = max(0, int(ms) // 1000)
    return f"{s // 60}:{s % 60:02d}"


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
        self.sp = Spotify()
        self.q = queue.Queue()
        self.wake = threading.Event()
        self.track = None          # current track id
        self.uri = None
        self.kind = "track"        # track | episode — decides the library path
        self.art_url = None
        self.playing = False
        self.pos = 0               # ms at self.stamp
        self.dur = 1
        self.stamp = time.monotonic()
        self.volume = 50
        self.liked = False
        self.shuffle = False
        self.repeat = "off"        # off | context | track
        self.links = {}            # web URLs for the browser shortcuts
        self.scrub = None          # fraction being dragged, or None
        self.poll = float(self.sp.cfg.get("poll", POLL))
        self.msg = "connecting…"
        self.alive = True
        self._added = 0.0          # monotonic deadline for the ✚ confirmation
        self._art_ref = None
        self._art_img = None       # last PIL image, re-resized on rescale
        self.info = ("", "")       # raw title/artist, re-ellipsized on rescale
        self._year = ""
        self._shown = None         # self.info currently in the labels
        self._job = None           # pending marquee callback
        self._expanded = False
        self.qwin = None           # queue popup
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
        r.configure(bg=BG)
        r.geometry(f"{self.W}x{self.H}")
        frameless(r)  # after geometry: the remap shouldn't move the widget
        r.attributes("-topmost", True)
        # No saved position: let the WM place us. Blind guesses like +80+80 can
        # land off the usable area on multi-monitor setups and get clamped.
        self.moved = False
        if cfg.get("pos"):
            r.after(2000, lambda: self.place(*cfg["pos"]))

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

    def _icon(self, parent, key, fnt, pad, cmd=None, rest=FG):
        b = tk.Label(parent, text=GLYPH[key], bg=BG, fg=rest, font=fnt, bd=0,
                     highlightthickness=0, pady=0, padx=pad, cursor="hand2")
        b.bind("<Button-1>", lambda e: (cmd or self.press)(key))
        b.bind("<Enter>", lambda e: b.config(fg=self._rest(key, hover=True)))
        b.bind("<Leave>", lambda e: b.config(fg=self._rest(key)))
        self.btns[key] = b
        return b

    def _rest(self, key, hover=False):
        """Resting colour of a toggle icon — its state, not the pointer."""
        if key == "like":
            return ACCENT if self.liked else (FG if hover else DIM)
        if key == "shuffle":
            return ACCENT if self.shuffle else (FG if hover else DIM)
        if key == "repeat":
            return ACCENT if self.repeat != "off" else (FG if hover else DIM)
        if key == "add" and time.monotonic() < self._added:
            return ACCENT
        return FG if hover else DIM

    def _build_ui(self):
        """Everything sized by self.scale. Torn down and re-run on rescale."""
        s = self.scale
        W, ART, PAD = self.W, self.ART, self.PAD
        body = self.body = tk.Frame(self.root, bg=BG)
        body.place(x=0, y=0, relwidth=1, relheight=1)
        self.btns = {}

        px = lambda n: max(6, round(n * s))
        self.f_title = tkinter.font.Font(font=("TkDefaultFont", px(11), "bold"))
        self.f_sub = tkinter.font.Font(font=("TkDefaultFont", px(9)))
        f_ico = tkinter.font.Font(font=("TkDefaultFont", px(12)))
        f_time = tkinter.font.Font(font=("TkDefaultFont", px(8)))

        # Fonts stop shrinking below ~6px, so at small scales a row is taller
        # than its share of H. Stack the three zones on measured line heights
        # and grow the widget to fit rather than letting rows collide.
        th, sh = self.f_title.metrics("linespace"), self.f_sub.metrics("linespace")
        ih, tlh = f_ico.metrics("linespace"), f_time.metrics("linespace")
        rail_h = max(3, round(6 * s))
        disc = max(round(42 * s), ih + round(10 * s))     # play button diameter
        ty = PAD + max(0, (ART - th - round(4 * s) - sh) // 2)   # title
        ay = ty + th + round(4 * s)                              # artist
        cy = PAD + max(ART, th + sh) + round(16 * s)             # transport row
        sy = cy + disc + round(16 * s)                           # scrubber row
        uy = sy + max(tlh, rail_h) + round(14 * s)               # utility row
        H = self.H = uy + max(ih, rail_h) + PAD
        self.root.geometry(f"{W}x{H}")

        # -- header: art, metadata, quick actions -------------------------
        self.art = tk.Label(body, bg=CARD, bd=0, highlightthickness=0)
        self.art.place(x=PAD, y=PAD, width=ART, height=ART)
        if self._art_img:
            self._show_art()

        acts = tk.Frame(body, bg=BG)
        for key in ("like", "add", "queue"):
            self._icon(acts, key, f_ico, round(5 * s),
                       rest=self._rest(key)).pack(side="left")
        acts.place(x=W - PAD, y=PAD + ART // 2, anchor="e")
        acts.update_idletasks()

        tx = PAD + ART + round(12 * s)
        tw = self.tw = max(round(20 * s), W - tx - PAD - acts.winfo_reqwidth()
                           - round(8 * s))
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

        # -- centre: transport --------------------------------------------
        ctl = tk.Frame(body, bg=BG)
        self._icon(ctl, "shuffle", f_ico, round(7 * s),
                   rest=self._rest("shuffle")).pack(side="left")
        self._icon(ctl, "prev", f_ico, round(7 * s)).pack(side="left")
        self.disc = disc
        self.play_c = tk.Canvas(ctl, width=disc, height=disc, bg=BG, bd=0,
                                highlightthickness=0, cursor="hand2")
        self.play_c.create_oval(0, 0, disc - 1, disc - 1, fill=FG, width=0)
        self.play_c.pack(side="left", padx=round(9 * s))
        self.play_c.bind("<Button-1>", lambda e: self.press("play"))
        self._icon(ctl, "next", f_ico, round(7 * s)).pack(side="left")
        self._icon(ctl, "repeat", f_ico, round(7 * s),
                   rest=self._rest("repeat")).pack(side="left")
        ctl.place(relx=0.5, y=cy, anchor="n")

        # -- lower: scrubber ----------------------------------------------
        wt = f_time.measure("00:00")
        self.lbl_t0 = tk.Label(body, text="0:00", bg=BG, fg=DIM, font=f_time,
                               bd=0, highlightthickness=0, pady=0, anchor="w")
        self.lbl_t0.place(x=PAD, y=sy + (max(tlh, rail_h) - tlh) // 2, width=wt)
        self.lbl_t1 = tk.Label(body, text="0:00", bg=BG, fg=DIM, font=f_time,
                               bd=0, highlightthickness=0, pady=0, anchor="e")
        self.lbl_t1.place(x=W - PAD - wt, y=sy + (max(tlh, rail_h) - tlh) // 2,
                          width=wt)
        gap = round(8 * s)
        rw = max(round(30 * s), W - 2 * (PAD + wt + gap))
        self.rail = tk.Canvas(body, width=rw, height=rail_h, bg=BG, bd=0,
                              highlightthickness=0, cursor="hand2")
        self.rail.place(x=PAD + wt + gap, y=sy + (max(tlh, rail_h) - rail_h) // 2)
        capsule(self.rail, 0, 0, rw, rail_h, RAIL, "track")
        self.rail_geo = (0, 0, rw, rail_h)
        self.rail.bind("<Button-1>", self.scrub_to)
        self.rail.bind("<B1-Motion>", self.scrub_to)
        self.rail.bind("<ButtonRelease-1>", self.scrub_done)

        # -- lower: utility toolbar ---------------------------------------
        left = tk.Frame(body, bg=BG)
        for key in ("lyrics", "playlist"):
            self._icon(left, key, f_ico, round(6 * s), cmd=self.open_url).pack(
                side="left")
        left.place(x=PAD - round(6 * s), y=uy)

        right = tk.Frame(body, bg=BG)
        self._icon(right, "devices", f_ico, round(6 * s)).pack(side="left")
        vw, vh = round(52 * s), max(ih, rail_h)
        self.vol = tk.Canvas(right, width=vw, height=vh, bg=BG, bd=0,
                             highlightthickness=0, cursor="hand2")
        self.vol.pack(side="left", padx=round(4 * s))
        vi, cyy = max(6, round(9 * s)), vh / 2      # speaker glyph, drawn: no
        self.vol.create_polygon(                    # dependable emoji for it
            0, cyy - vi * .26, vi * .45, cyy - vi * .26, vi, cyy - vi * .55,
            vi, cyy + vi * .55, vi * .45, cyy + vi * .26, 0, cyy + vi * .26,
            fill=DIM, width=0)
        self.vol.create_arc(vi * 1.15, cyy - vi * .6, vi * 2.1, cyy + vi * .6,
                            start=-55, extent=110, style="arc", outline=DIM)
        bx = round(vi * 2.5)
        self.vol_geo = (bx, (vh - rail_h) / 2, vw - bx, rail_h)
        capsule(self.vol, *self.vol_geo, RAIL, "track")
        self.vol.bind("<Button-1>", self.vol_to)
        self.vol.bind("<B1-Motion>", self.vol_to)
        self.vol.bind("<ButtonRelease-1>", self.vol_done)
        self._icon(right, "expand", f_ico, round(6 * s),
                   cmd=lambda k: self.toggle_expand()).pack(side="left")
        right.place(x=W - PAD + round(6 * s), y=uy, anchor="ne")

        self.menu = tk.Menu(body, tearoff=0, bg=CARD, fg=FG, bd=0,
                            activebackground=ACCENT, activeforeground=INK)
        self.menu.add_command(label="Settings", command=self.settings)
        self.menu.add_command(label="Expand / shrink", command=self.toggle_expand)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        for w in (body, self.art, clip, self.lbl_title, self.lbl_artist, ctl,
                  acts, left, right, self.lbl_t0, self.lbl_t1):
            w.bind("<Button-1>", self.grab, add="+")
            w.bind("<B1-Motion>", self.drag, add="+")
            w.bind("<Button-3>",
                   lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.render_play()
        self.root.update_idletasks()
        round_window(self.root, W, H, max(4, round(RADIUS * s)))

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

    def _show_art(self):
        self._art_ref = ImageTk.PhotoImage(
            round_img(self._art_img, self.ART, round(12 * self.scale)))
        self.art.config(image=self._art_ref)

    def settings(self):
        if self.win and self.win.winfo_exists():
            self.win.lift()
            return
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
                          troughcolor="#2a2a2a", highlightthickness=0, bd=0,
                          activebackground=ACCENT)
            sc.pack(fill="x", padx=12, pady=(0, 6))
            # commit on release, not on every pixel of the drag
            sc.bind("<ButtonRelease-1>", lambda e, f=done: f())
        for text, var in (("Scroll long titles", self.scroll),
                          ("Show release year", self.year)):
            tk.Checkbutton(w, text=text, variable=var, command=self.set_flags,
                           bg=BG, fg=DIM, activebackground=BG,
                           activeforeground=FG, selectcolor="#2a2a2a",
                           highlightthickness=0, bd=0, anchor="w").pack(
                fill="x", padx=8, pady=(0, 4))

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

    # -- sliders ----------------------------------------------------------

    def scrub_to(self, e):
        """Track the pointer locally; one seek on release beats one per pixel."""
        if self.track:
            self.scrub = max(0.0, min(1.0, e.x / self.rail_geo[2]))

    def scrub_done(self, e):
        if self.scrub is not None:
            frac, self.scrub = self.scrub, None
            self.pos, self.stamp = frac * self.dur, time.monotonic()
            self.press("seek", int(frac * self.dur))

    def vol_to(self, e):
        x, _, w, _ = self.vol_geo
        self.volume = round(max(0.0, min(1.0, (e.x - x) / w)) * 100)

    def vol_done(self, e):
        self.press("volset", self.volume)

    # -- actions ----------------------------------------------------------

    def open_url(self, key):
        """Lyrics and the playing context both live in the browser: the Web API
        exposes neither."""
        title, artist = self.info
        if key == "lyrics" and title and not self.msg:
            webbrowser.open("https://genius.com/search?q="
                            + urllib.parse.quote(f"{artist} {title}"))
        elif key == "playlist":
            url = self.links.get("context") or self.links.get("track")
            if url:
                webbrowser.open(url)

    def toggle_expand(self):
        base = float(self.sp.cfg.get("scale", 1.0))
        self._expanded = not self._expanded
        self.size.set(round((min(2.5, base * 1.6) if self._expanded else base)
                            * 100))
        self.set_scale(save=False)

    def show_queue(self, d):
        if self.qwin and self.qwin.winfo_exists():
            self.qwin.destroy()
        items = ((d or {}).get("queue") or [])[:8]
        w = self.qwin = tk.Toplevel(self.root)
        w.title("triolFM queue")
        w.configure(bg=CARD)
        w.attributes("-topmost", True)
        w.geometry(f"+{self.root.winfo_x()}+{self.root.winfo_y() + self.H + 8}")
        tk.Label(w, text="Up next", bg=CARD, fg=DIM, anchor="w",
                 font=("TkDefaultFont", 9, "bold")).pack(fill="x", padx=12,
                                                         pady=(10, 4))
        for it in items or [None]:
            line = "queue is empty" if not it else "{}  —  {}".format(
                it.get("name", ""),
                ", ".join(a["name"] for a in it.get("artists", [])) or "…")
            tk.Label(w, text=line[:60], bg=CARD, fg=FG if it else DIM,
                     anchor="w").pack(fill="x", padx=12, pady=1)
        tk.Label(w, text="click to close", bg=CARD, fg=DIM, anchor="w").pack(
            fill="x", padx=12, pady=(6, 8))
        w.bind("<Button-1>", lambda e: w.destroy())
        w.bind("<Escape>", lambda e: w.destroy())

    def show_devices(self, devs):
        m = tk.Menu(self.root, tearoff=0, bg=CARD, fg=FG, bd=0,
                    activebackground=ACCENT, activeforeground=INK)
        if not devs:
            m.add_command(label="no devices", state="disabled")
        for d in devs:
            m.add_command(label=("● " if d.get("is_active") else "   ")
                          + d.get("name", "?"),
                          command=lambda i=d["id"]: self.press("device", i))
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

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

    def set_scale(self, save=True):
        self.scale = self.size.get() / 100
        if save:  # the expand toggle is transient; the slider is the real size
            self._expanded = False
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
        if key in ("lyrics", "playlist"):
            return self.open_url(key)
        if key == "play":  # optimistic flip, poll will confirm
            self.playing = not self.playing
            self.pos, self.stamp = self.now(), time.monotonic()
            self.render_play()
        elif key == "shuffle":
            self.shuffle = not self.shuffle
        elif key == "repeat":
            self.repeat = {"off": "context", "context": "track",
                           "track": "off"}[self.repeat]
        elif key == "like":
            if not self.track or not self.savable():
                return self.q.put(("err", "local file"))
            self.liked = not self.liked
        elif key == "add":
            if not self.savable():
                return self.q.put(("err", "local file"))
        elif key.startswith("vol") and key != "volset":
            self.volume = max(0, min(100, self.volume + (5 if key == "vol+" else -5)))
        self.render_toggles()
        threading.Thread(target=self._do, args=(key, arg), daemon=True).start()

    def savable(self):
        """Local files have no id and can't be liked or put in a playlist."""
        return bool(self.uri) and not self.uri.startswith("spotify:local")

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
            elif key == "shuffle":
                self.sp.call("PUT", "/me/player/shuffle",
                             state="true" if self.shuffle else "false")
            elif key == "repeat":
                self.sp.call("PUT", "/me/player/repeat", state=self.repeat)
            elif key == "like":
                self.sp.call("PUT" if self.liked else "DELETE",
                             f"/me/{self.kind}s", ids=self.track)
            elif key == "add":
                self.add_to_playlist(self.uri)
            elif key == "queue":
                self.q.put(("queue", self.sp.call("GET", "/me/player/queue")))
            elif key == "devices":
                self.q.put(("devices", (self.sp.call(
                    "GET", "/me/player/devices") or {}).get("devices") or []))
            elif key == "device":
                self.sp.call("PUT", "/me/player", body={"device_ids": [arg]})
            elif key == "volset":
                self.sp.call("PUT", "/me/player/volume", volume_percent=arg)
            elif key.startswith("vol"):
                self.sp.call("PUT", "/me/player/volume", volume_percent=self.volume)
        except urllib.error.HTTPError as e:
            self.q.put(("err", "premium required" if e.code == 403
                        else "no active device" if e.code == 404
                        else f"http {e.code}"))
        except OSError:
            self.q.put(("err", "offline"))
        self.wake.set()

    def add_to_playlist(self, uri, retry=True):
        """Append to a private "triolFM" playlist, created on first use."""
        pid = self.sp.cfg.get("playlist")
        if not pid:
            me = self.sp.call("GET", "/me")["id"]
            pid = self.sp.call("POST", f"/users/{me}/playlists", body={
                "name": "triolFM", "public": False,
                "description": "Saved from the triolFM widget"})["id"]
            self.sp.cfg["playlist"] = pid
            save_cfg(self.sp.cfg)
        try:
            self.sp.call("POST", f"/playlists/{pid}/tracks", uris=uri)
        except urllib.error.HTTPError as e:
            if e.code != 404 or not retry:
                raise
            self.sp.cfg.pop("playlist", None)  # deleted upstream — remake it
            save_cfg(self.sp.cfg)
            return self.add_to_playlist(uri, retry=False)
        self.q.put(("added", None))

    def poller(self):
        while True:
            try:
                self.q.put(("state", self.sp.call("GET", "/me/player")))
            except urllib.error.HTTPError as e:
                self.q.put(("err", f"http {e.code}"))
            except OSError:
                self.q.put(("err", "offline"))
            self.wake.wait(self.poll)  # actions and rate changes wake us early
            self.wake.clear()

    def fetch_art(self, url):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                img = Image.open(BytesIO(r.read()))
                img.load()
            self.q.put(("art", (url, img)))
        except (OSError, ValueError):
            pass

    def fetch_liked(self, tid, kind):
        try:
            r = self.sp.call("GET", f"/me/{kind}s/contains", ids=tid)
            self.q.put(("liked", (tid, bool(r and r[0]))))
        except (urllib.error.HTTPError, OSError):
            pass

    # -- render -----------------------------------------------------------

    def now(self):
        return elapsed(self.pos, self.stamp, self.dur, self.playing,
                       time.monotonic())

    def render_play(self):
        """White disc with a black play triangle / pause bars."""
        d = self.disc
        self.play_c.delete("g")
        m = d * 0.3
        if self.playing:
            bw = max(2, round(d * 0.085))
            for dx in (-d * 0.105, d * 0.105):
                cx = d / 2 + dx
                self.play_c.create_rectangle(cx - bw / 2, m, cx + bw / 2, d - m,
                                             fill=INK, width=0, tags="g")
        else:
            self.play_c.create_polygon(d * .39, m, d * .39, d - m, d * .73, d / 2,
                                       fill=INK, width=0, tags="g")

    def render_toggles(self):
        for key in ("like", "shuffle", "repeat", "add"):
            self.btns[key].config(fg=self._rest(key))
        self.btns["repeat"].config(
            text=GLYPH["repeat"] + ("¹" if self.repeat == "track" else ""))

    def apply(self, d):
        if not d or not d.get("item"):
            self.track, self.playing, self.msg = None, False, "nothing playing"
            return
        it = d["item"]
        self.playing = bool(d.get("is_playing"))
        self.dur = max(1, it.get("duration_ms", 1))
        self.pos = d.get("progress_ms") or 0
        self.stamp = time.monotonic()
        self.volume = (d.get("device") or {}).get("volume_percent") or self.volume
        self.shuffle = bool(d.get("shuffle_state"))
        self.repeat = d.get("repeat_state") or "off"
        self.msg = None
        was = self.track
        self.track = it.get("id") or it.get("uri") or it.get("name")  # local: no id
        self.uri = it.get("uri")
        self.kind = "episode" if it.get("type") == "episode" else "track"
        self.links = {
            "track": (it.get("external_urls") or {}).get("spotify"),
            "context": ((d.get("context") or {}).get("external_urls")
                        or {}).get("spotify")}
        alb = it.get("album") or {}
        # albums date the release; podcast episodes carry release_date directly
        self._year = (alb.get("release_date") or it.get("release_date") or "")[:4]
        self.info = (it.get("name", ""),
                     ", ".join(a["name"] for a in it.get("artists", [])))
        if self.info != self._shown:
            self.lbl_title.config(fg=FG)
            self._text()
        if self.track != was:
            self.liked = False
            if it.get("id"):  # local files aren't in the library
                threading.Thread(target=self.fetch_liked,
                                 args=(it["id"], self.kind), daemon=True).start()
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
            elif kind == "queue":
                self.show_queue(payload)
            elif kind == "devices":
                self.show_devices(payload)
            elif kind == "added":
                self._added = time.monotonic() + 1.5
            elif kind == "liked" and payload[0] == self.track:
                self.liked = payload[1]
            elif kind == "art" and payload[0] == self.art_url:
                self._art_img = payload[1]
                self._show_art()

        if self.msg:
            self.info, self._year = (self.msg, ""), ""
            if self.info != self._shown:
                self.lbl_title.config(fg=DIM)
                self._text()
            self.art.config(image="")
            self._art_ref = self._art_img = self.art_url = None
        self.render_play()
        self.render_toggles()
        pos = self.scrub * self.dur if self.scrub is not None else self.now()
        self.lbl_t0.config(text=fmt(pos if self.track else 0))
        self.lbl_t1.config(text=fmt(self.dur if self.track else 0))
        self.rail.delete("f")
        if self.track:
            x, y, w, h = self.rail_geo
            capsule(self.rail, x, y, round(w * pos / self.dur), h, ORANGE, "f")
        self.vol.delete("f")
        x, y, w, h = self.vol_geo
        capsule(self.vol, x, y, round(w * self.volume / 100), h, FG, "f")
        self.root.after(250, self.tick)

    def run(self):
        threading.Thread(target=self.poller, daemon=True).start()
        self.root.after(100, self.tick)
        self.root.mainloop()


if __name__ == "__main__":
    Widget().run()
