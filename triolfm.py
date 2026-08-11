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

from PIL import Image, ImageTk

CFG_PATH = os.path.expanduser("~/.config/triolfm/config.json")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state"
POLL = 3.0            # seconds between /me/player reads; overridable in config
SCROLL_MS, HOLD = 60, 12   # marquee: 1px per 60ms, ~0.7s pause at each end

W, H, ART, PAD = 340, 104, 76, 8  # at scale 1.0; settings scales these

BG, FG, DIM, ACCENT = "#121212", "#ffffff", "#8a8a8a", "#1db954"


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
    save_cfg(cfg)
    return tok["access_token"], time.time() + tok["expires_in"]


class Spotify:
    def __init__(self):
        self.cfg = load_cfg()
        self.token, self.exp = None, 0.0

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
        self.token, self.exp = login(self.cfg)
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
        self.art_url = None
        self.playing = False
        self.pos = 0               # ms at self.stamp
        self.dur = 1
        self.stamp = time.monotonic()
        self.volume = 50
        self.poll = float(self.sp.cfg.get("poll", POLL))
        self.msg = "connecting…"
        self.alive = True
        self._art_ref = None
        self._art_img = None       # last PIL image, re-resized on rescale
        self.info = ("", "")       # raw title/artist, re-ellipsized on rescale
        self._year = ""
        self._shown = None         # self.info currently in the labels
        self._job = None           # pending marquee callback
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

    def _build_ui(self):
        """Everything sized by self.scale. Torn down and re-run on rescale."""
        s = self.scale
        W, H, ART, PAD = self.W, self.H, self.ART, self.PAD
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

        self.art = tk.Label(body, bg="#1e1e1e", bd=0)
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

        self.bar = tk.Canvas(body, bg="#2a2a2a", height=bh, bd=0,
                             highlightthickness=0, cursor="hand2")
        self.bar.place(x=0, y=H - bh, width=W)
        self.fill = self.bar.create_rectangle(0, 0, 0, bh, fill=ACCENT, width=0)
        self.bar.bind("<Button-1>", self.seek)
        self._bh = bh

        self.menu = tk.Menu(body, tearoff=0, bg="#1e1e1e", fg=FG, bd=0,
                            activebackground=ACCENT, activeforeground="#000")
        self.menu.add_command(label="Settings", command=self.settings)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        for w in (body, self.art, clip, self.lbl_title, self.lbl_artist, ctl):
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
        if key == "play":  # optimistic flip, poll will confirm
            self.playing = not self.playing
            self.pos, self.stamp = self.now(), time.monotonic()
            self.render_play()
        elif key.startswith("vol"):
            self.volume = max(0, min(100, self.volume + (5 if key == "vol+" else -5)))
        threading.Thread(target=self._do, args=(key, arg), daemon=True).start()

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
        except urllib.error.HTTPError as e:
            self.q.put(("err", "premium required" if e.code == 403
                        else "no active device" if e.code == 404
                        else f"http {e.code}"))
        except OSError:
            self.q.put(("err", "offline"))
        self.wake.set()

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

    # -- render -----------------------------------------------------------

    def now(self):
        return elapsed(self.pos, self.stamp, self.dur, self.playing,
                       time.monotonic())

    def render_play(self):
        self.btns["play"].config(text="❚❚" if self.playing else "▶")

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
            elif kind == "art" and payload[0] == self.art_url:
                self._art_img = payload[1]
                self._art_ref = ImageTk.PhotoImage(
                    self._art_img.resize((self.ART, self.ART), Image.LANCZOS))
                self.art.config(image=self._art_ref)

        if self.msg:
            self.info, self._year = (self.msg, ""), ""
            if self.info != self._shown:
                self.lbl_title.config(fg=DIM)
                self._text()
            self.art.config(image="")
            self._art_ref = self._art_img = self.art_url = None
        self.render_play()
        frac = self.now() / self.dur if self.track else 0
        self.bar.coords(self.fill, 0, 0, self.W * frac, self._bh)
        self.root.after(250, self.tick)

    def run(self):
        threading.Thread(target=self.poller, daemon=True).start()
        self.root.after(100, self.tick)
        self.root.mainloop()


if __name__ == "__main__":
    Widget().run()
