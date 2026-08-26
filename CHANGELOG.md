# Changelog

All notable changes to triolFM. Versions follow [semver](https://semver.org); the
number lives in `__version__` in `spotify_backend.py`, and `build-deb.sh` and
`setup.py` read it from there.

## 2.0.0 — 2026-08-26

The Tk widget is gone. triolFM is now a Dynamic Island.

### Added

- **Dynamic Island frontend** (PySide6). A black pill flush against the top edge of the
  primary screen, in three states: idle pill with cover and spectrum, a two-second peek
  on a track change, and the full transport view while the pointer is on it. Cover,
  title, artist, release year, play/pause, prev, next, a draggable progress bar and
  scroll-to-set volume.
- **System tray icon** carrying the same right-click menu as the island — refresh rate,
  release year, reconnect, quit. Desktops without a tray (WSLg has none) simply never
  show it; the island's own right-click still works.
- **Close cross** in the open island, so quitting needs no menu.
- **Poll burst after a control press** — play, skip and seek poll immediately, then
  twice more half a second apart, because Spotify keeps reporting the previous track for
  a moment after a skip. Capped per press, so holding skip cannot run away with the rate
  limit. Volume changes skip the burst.
- `docs/triolfm-flow.html`, a swimlane of one poll cycle.

### Changed

- Qt is pinned to the `xcb` backend on Linux: Wayland offers no reliable always-on-top,
  no input mask and no absolute placement, all three of which the island needs. Override
  with `QT_QPA_PLATFORM`.
- The window advertises `_NET_WM_WINDOW_TYPE_DOCK` so window managers place it where it
  asks instead of relocating it — under WSLg, weston otherwise drops it at 32,32 and
  ignores every move afterwards.
- The island is no longer draggable. Like the real thing it is pinned to the top centre.
- Dependencies: PySide6 replaces Tk. Pillow is still there for cover art.

### Fixed

- The island collapses when the pointer leaves it sideways, not only downwards.
- The cover art no longer pokes out of the pill mid-collapse.
- The mask no longer leaves a stale band as the island collapses.
- Under WSLg the Windows pointer stays over the island instead of falling through it.

## 1.0.0 — 2026-08-18

First public release: a Tk widget with live track info, cover art, transport controls,
UI tinted from the dominant album-art colour, PKCE login against the Spotify Web API,
exponential backoff on errors, and a `.deb` / `install.sh` / `.app` install story.
