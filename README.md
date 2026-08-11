# triolFM

A tiny always-on-top Spotify remote. It does **not** play audio and does not replace the
Spotify desktop app — it drives whatever device your Spotify app is already playing on.
A borderless, rounded 372×204 player card in three zones: cover art + metadata + quick
actions on top, transport in the middle, scrubber and utility toolbar below.

One file, ~1000 lines, stdlib only except Pillow (cover art decoding and rounding).

## Install

```sh
sudo apt install python3-tk python3-pil python3-pil.imagetk   # Debian/Ubuntu
# macOS: brew install python-tk && pip install pillow
# Windows: tkinter ships with python.org installers; pip install pillow
```

## Setup

1. Create an app at <https://developer.spotify.com/dashboard>.
2. Add the redirect URI **exactly**: `http://127.0.0.1:8888/callback` (`127.0.0.1`, not
   `localhost` — Spotify rejects `localhost`). Tick **Web API**.
3. Copy the Client ID.

```sh
python3 triolfm.py
```

First run asks for the Client ID (or reads `$SPOTIFY_CLIENT_ID`), opens your browser
once, and stores the refresh token in `~/.config/triolfm/config.json` (mode 600). After
that it starts straight into the widget.

## Install as a real app

```sh
./install.sh              # install or upgrade
./install.sh --uninstall  # remove
```

No sudo. Everything lands under `~/.local`, where XDG expects user apps:

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

Re-run `./install.sh` after editing `triolfm.py` to push changes to the installed copy.

### Or install it as a .deb

```sh
./build-deb.sh
sudo apt install ./triolfm_1.0.0_all.deb
```

System-wide install to `/usr/bin/triolfm`, `/usr/share/triolfm/`, plus the desktop entry
and icon. apt resolves `python3-tk` and `python3-pil.imagetk` itself, so the manual
`apt install` step above is unnecessary on this route. Remove with
`sudo apt remove triolfm`.

`build-deb.sh` needs only `dpkg-deb`, which ships with Debian and Ubuntu. Override
`VERSION` or `MAINTAINER` by environment variable.

**Pick one route.** `~/.local/bin` sits ahead of `/usr/bin` on PATH, so a leftover
user install shadows the packaged one. Run `./install.sh --uninstall` before installing
the .deb.

## Controls

| Action | How |
|---|---|
| Play / pause | the white disc |
| Prev, next | ◀◀ / ▶▶ |
| Shuffle | ⇄ — green when on |
| Repeat | ↻ — off, whole context, then ↻¹ for one track |
| Like / unlike | ♥ — green when the track is in your library |
| Add to a playlist | ✚ — appends to a private playlist called *triolFM*, created on first use |
| Show the queue | ≡ — the next 8 items, click to close |
| Seek | drag the orange scrubber; the seek fires on release |
| Volume | drag the white level bar, or scroll anywhere on the widget |
| Lyrics | ♫ — opens a Genius search for the track (the Web API has no lyrics) |
| Open the playlist / album | ▤ — opens the playing context in your browser |
| Switch device | ▭ — lists Spotify Connect devices, click one to move playback |
| Expand / shrink | ⇲ — 1.6× the saved size and back |
| Move | drag anywhere outside the sliders |
| Settings, expand, quit | right-click anywhere |

Window position, refresh rate and size are remembered in the config file. There is no
title bar and no × — right-click is the way out.

## Settings

⚙ opens two sliders (applied on release) and two toggles:

- **Refresh rate** — 1–30s between `/me/player` polls. Default 3s ≈ 20 req/min,
  comfortably under Spotify's rolling limit. The progress bar is interpolated locally
  between polls, so even 30s still gives a smoothly moving bar — only track changes lag.
- **Widget size** — 50–250% of the 372×204 default. Fonts, art and spacing all scale;
  the widget is rebuilt in place, no restart.
- **Scroll long titles** — on by default. A title too wide for the widget slides
  back and forth instead of being cut off with an ellipsis. Off restores the ellipsis.
- **Show release year** — on by default. Appends the album's release year to the
  artist line (`Some Artist · 1998`).

## Notes

- **Spotify Premium is required** for play/pause/skip/seek/volume/shuffle/repeat and
  device switching — a Web API restriction, not this app. Free accounts still get live
  track info, cover art, likes, playlist adds and the queue view.
- Liking and playlist adds need library and playlist scopes. Upgrading from an older
  version re-runs the browser authorization once, because Spotify won't widen the scopes
  of an existing refresh token.
- Local files can't be liked or added to a playlist (they have no Spotify id) — the
  widget says `local file` instead.
- Rounded window corners use the X11 SHAPE extension; where it's unavailable the corners
  are simply square.
- Status line shows `nothing playing`, `no active device`, `premium required` or
  `offline` when something is up.

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

Getting a borderless always-on-top widget under WSLg took three tries, documented here
so nobody repeats them:

- `overrideredirect(True)` — the usual frameless trick — makes the app **invisible**.
  WSLg never forwards override-redirect X11 windows to the Windows desktop.
- Every `_NET_WM_WINDOW_TYPE` (dock, splash, notification, …) is still decorated by
  weston's window manager.
- What works: `_MOTIF_WM_HINTS` with decorations off, set via `ctypes` against
  `libX11` while the window is withdrawn. See `_x11_undecorate()`.
- weston also ignores lone window-move requests, so restoring the saved position walks
  there in several steps like a drag. See `place()`.

On Windows and macOS the code takes the plain `overrideredirect` path.

## Test

```sh
python3 test_triolfm.py
```
