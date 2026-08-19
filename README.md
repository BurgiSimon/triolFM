# triolFM

A Dynamic Island for Spotify. A black pill sits flush against the top edge of your
screen: idle it shows the cover and a spectrum, a track change makes it peek, and
hovering springs it open into full transport controls. It does **not** play audio and
does not replace the Spotify desktop app — it drives whatever device your Spotify app is
already playing on.

Two files: `triolfm.py` draws the island (PySide6), `spotify_backend.py` talks to the
Web API (stdlib plus Pillow for cover art).

## Install

Pick **one** route. On Linux the `.deb` is the easy one: it pulls in the Python bits for
you and uninstalls cleanly.

### Debian / Ubuntu / Mint / Pop!_OS — the `.deb`

Download `triolfm_<version>_all.deb` from the
[Releases page](https://github.com/BurgiSimon/triolFM/releases) and either double-click
it in your file manager, or:

```sh
sudo apt install ./triolfm_1.0.0_all.deb
```

apt resolves PySide6 and Pillow itself. triolFM then appears in your
applications menu. Remove it with `sudo apt remove triolfm`.

To build the `.deb` yourself: `./build-deb.sh` (needs only `dpkg-deb`, which Debian and
Ubuntu already have).

### Any other Linux — `install.sh`

```sh
sudo apt install python3-pyside6.qtwidgets python3-pil   # or your distro's names
./install.sh              # install or upgrade
./install.sh --uninstall  # remove
```

No sudo for the install itself. Everything lands under `~/.local`, where XDG expects
user apps:

| Path | What |
|---|---|
| `~/.local/bin/triolfm` | launcher — run `triolfm` from any directory |
| `~/.local/share/triolfm/` | the app itself (copied, so the repo can move) |
| `~/.local/share/applications/triolfm.desktop` | menu entry |
| `~/.local/share/icons/hicolor/256x256/apps/triolfm.png` | icon |

Under WSL the installer also asks for sudo once, to symlink the desktop entry and icon
into `/usr/share/applications` and `/usr/share/icons` — see
[Windows Start Menu shortcut](#windows-start-menu-shortcut) for why. Say no and the app
still installs, it just won't appear in Windows search.

Re-run `./install.sh` after editing the sources to push changes to the installed copy.

**`~/.local/bin` sits ahead of `/usr/bin` on PATH**, so a leftover user install shadows a
packaged one. Run `./install.sh --uninstall` before installing the `.deb`.

### macOS — `.app` bundle

```sh
./build-app.sh    # builds dist/triolFM.app
```

Drag `dist/triolFM.app` into `/Applications`. The script installs `py2app`, `pillow`
and `PySide6` itself. On a MacBook with a real notch the island sits right under it
rather than on it — triolFM draws its own pill at the top centre of the primary
screen.

The bundle is **unsigned**, so the first launch must be **right-click the app → Open →
Open**. A plain double-click gets refused by Gatekeeper with "cannot be opened because
the developer cannot be verified". After that first time it opens normally.

*(This bundle is built by py2app but has not been tested on macOS by the author —
if it misbehaves, please open an issue.)*

### Windows

There is no native Windows build. Run it inside
[WSL](https://learn.microsoft.com/windows/wsl/install) with the `.deb` or `install.sh`
above — WSLg draws the island on the Windows desktop and puts a shortcut in the Start
Menu. Or run `python3 triolfm.py` directly on Windows Python (`pip install pillow
PySide6`).

## Connect to Spotify

Spotify only lets an app talk to your account if that app is registered with them, and
they make each person register their own. It is a five-minute, one-time thing.

1. Go to <https://developer.spotify.com/dashboard> and log in with your normal Spotify
   account.
2. Click **Create app**. Name and description can be anything.
3. In **Redirect URIs**, paste this **exactly**, then click **Add**:

   ```
   http://127.0.0.1:8888/callback
   ```

   It must be `127.0.0.1`, not `localhost` — Spotify rejects `localhost`.
4. Tick **Web API**, agree to the terms, and save.
5. Open the app you just made, click **Settings**, and copy the **Client ID** (the long
   string of letters and numbers). Not the Client Secret — triolFM never needs it.
6. Start triolFM. It asks for the Client ID: paste it in and press OK.
7. Your browser opens once, asks you to allow triolFM, and says "Authorized. You can
   close this tab."

That's it. triolFM stores the login in `~/.config/triolfm/config.json` (readable only by
you, mode 600) and starts straight into the widget every time after that. It never sees
your Spotify password, and the Client ID is not a secret — it's public by design.

If you prefer, set the `SPOTIFY_CLIENT_ID` environment variable instead of pasting it.

### When it doesn't work

triolFM shows a dialog explaining what went wrong. The usual causes:

| What you see | What to do |
|---|---|
| "Spotify never sent the login back" | The redirect URI is wrong. It must be exactly `http://127.0.0.1:8888/callback`, with `127.0.0.1`, no trailing slash. |
| "Spotify rejected the login" | The Client ID has a typo, or you pasted the Client *Secret*. |
| "triolFM needs port 8888" | Another program (or a second copy of triolFM) is using it. Close it and try again. |
| "premium required" | Play/pause/skip need Spotify Premium. Track info and cover art still work. |
| "no active device" | Start playing something in the Spotify app first — triolFM is a remote, it has nothing to control on its own. |
| nothing appears at the top of the screen | A `pip`-installed PySide6 also needs `sudo apt install libxcb-cursor0`; the distro package pulls it in itself. |

To start over — wrong Client ID, or you revoked triolFM's access on Spotify's side —
right-click **→ Reconnect to Spotify**. That forgets the saved login and asks again. No need
to delete any files.

## Controls

The island has three states. It idles as a small pill, peeks for a couple of seconds
when the track changes, and opens fully while the pointer is on it.

| Action | How |
|---|---|
| Open | move the pointer onto the pill |
| Play / pause, prev, next | the three glyphs, once open |
| Seek | drag the progress bar |
| Volume | scroll anywhere on the island — the bar shows the level for a moment |
| Settings, quit | right-click the island, or right-click the tray icon |

triolFM also puts an icon in the system tray, carrying the same right-click menu. Not
every desktop has a tray — WSLg has none at all, and there the icon simply never
appears; the island's own right-click is then the way out.

The island is not draggable: like the real thing it is pinned to the top centre of the
primary screen. Refresh rate and the release-year toggle are remembered in the config
file.

## Settings

Right-click the island:

- **Refresh rate** — 1, 3, 5, 10 or 30s between `/me/player` polls. Default 3s ≈ 20
  req/min, comfortably under Spotify's rolling limit. On errors the poll backs off
  exponentially (capped at 60s, or whatever `Retry-After` asks for) and resumes the
  moment you touch a control. The progress bar is interpolated locally between polls, so
  even 30s still gives a smoothly moving bar.

  Pressing play, skip or seek polls immediately and then twice more half a second apart,
  whatever the rate is set to — Spotify keeps reporting the previous track for a moment
  after a skip, so the first read back is often stale. Without the follow-ups a 30s rate
  would leave the wrong title on screen for 30s. The burst is capped per press, not
  accumulated, so holding down skip can't run away with the rate limit. Volume changes
  skip it, since they don't change what the island shows.
- **Show release year** — on by default. Appends the album's release year to the artist
  line (`Some Artist · 1998`).
- **Reconnect to Spotify…** — forgets the saved Client ID and login token, then asks
  again on the next poll.

## Notes

- **Spotify Premium is required** for play/pause/skip/seek/volume — a Web API
  restriction, not this app. Free accounts still get live track info and cover art.
- The title line shows `nothing playing`, `no active device`, `premium required`,
  `setup needed` or `offline` when something is up. Open the island to read it.
- The spectrum and the progress fill tint themselves to the dominant color of the
  current cover, crossfading over ~0.35s.
- The spectrum is decorative. The Web API exposes no audio levels, so the bars are a
  smoothed random walk gated on the play state, not the actual signal.
- On Linux triolFM forces Qt's `xcb` backend: Wayland offers no reliable always-on-top,
  no input mask and no absolute placement, all three of which the island needs. Set
  `QT_QPA_PLATFORM` yourself to override.
- `triolfm --version` prints the version.

### Windows Start Menu shortcut

Nothing here writes a `.lnk`. WSLg does it, and it does it by itself once the desktop
entry sits somewhere it looks:

1. `weston` (rdprail-shell) runs in the WSLg system distro, enters this distro's mount
   namespace, and starts `app_list_monitor_thread`.
2. That thread scans and `inotify`-watches exactly four directories:
   `/usr/share/applications`, `/usr/local/share/applications`,
   `/var/lib/snapd/desktop/applications`, `/var/lib/flatpak/exports/share/applications`.
   Ones that don't exist at weston start are skipped and never looked at again.
   **`~/.local/share/applications` is not in the list** — that is the whole reason
   `install.sh` needs the one sudo symlink.
3. Entries with `Terminal=true` or `NoDisplay=true` are dropped. `Icon=` is resolved as
   an icon-theme name (`/usr/share/icons/hicolor/<size>/apps/<name>.png`), falling back
   to `/usr/share/icons/wsl/linux.png` — hence the icon symlink too.
4. The surviving list goes over a custom RDP virtual channel to `WSLDVCPlugin`, hosted
   in the `mstsc` client on the Windows side.
5. The plugin writes
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs\<Distro>\<Name> (<Distro>).lnk`,
   target `C:\Program Files\WSL\wslg.exe`, arguments
   `-d <Distro> --cd "~" -- <Exec>`, and the PNG converted to
   `%LOCALAPPDATA%\Temp\WSLDVCPlugin\<Distro>\<Name>.ico`.

Because step 2 is `inotify` and not a one-shot scan, the shortcut shows up a second
after `install.sh` drops the symlink — no `wsl --shutdown`. The `.deb` route installs
into `/usr/share` anyway, so it needs nothing extra.

Weston logs every decision it makes about a desktop file, so if the shortcut doesn't
appear, the reason is in there:

```sh
grep -i 'app list\|desktop file' /mnt/wslg/weston.log
```

### WSLg specifics

Frameless windows under WSLg are their own adventure. weston's window manager decorates
every `_NET_WM_WINDOW_TYPE`, and it never forwards override-redirect X11 windows to the
Windows desktop at all. Qt's `FramelessWindowHint` goes through `_MOTIF_WM_HINTS`, which
weston does honour, so the island draws undecorated there — which is also why triolFM
pins Qt to the `xcb` backend rather than letting it pick Wayland.

## Development

```sh
python3 test_triolfm.py    # no network needed; the Island tests need PySide6
```

Without PySide6 the backend tests still run and the Island tests skip themselves, which
is how CI runs them on Python 3.9–3.14 (3.9 being the floor the `.deb` declares) with a
real Pillow installed. The `.deb` is built on every push.

The version lives in one place, `__version__` in `spotify_backend.py`; `build-deb.sh`
and `setup.py` both read it from there.

## License

MIT — see [LICENSE](LICENSE).
