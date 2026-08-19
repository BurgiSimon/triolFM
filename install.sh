#!/bin/sh
# Install triolFM for the current user. No sudo: everything lands under
# ~/.local, which is already on PATH and is where XDG expects user apps.
#
# Under WSL, WSLg's app-list monitor only watches /usr/share/applications (plus
# the snap and flatpak dirs) — never ~/.local/share/applications. So a Windows
# Start Menu shortcut needs one symlink there, which is the only sudo step and
# is skipped if sudo is unavailable. WSLg picks the symlink up over inotify
# within a second and writes the .lnk itself; no 'wsl --shutdown' needed.
#
#   ./install.sh              install (or upgrade in place)
#   ./install.sh --uninstall  remove everything it installed
set -eu

here=$(cd "$(dirname "$0")" && pwd)
libdir="$HOME/.local/share/triolfm"
bin="$HOME/.local/bin/triolfm"
desktop="$HOME/.local/share/applications/triolfm.desktop"
icon="$HOME/.local/share/icons/hicolor/256x256/apps/triolfm.png"
sysdesktop="/usr/share/applications/triolfm.desktop"
sysicon="/usr/share/icons/hicolor/256x256/apps/triolfm.png"

# Symlink the user's entry into the system dirs WSLg watches. Only ever touches
# symlinks, so a .deb install (real files at the same paths) is left alone.
wslg_link() {
    [ -n "${WSL_DISTRO_NAME:-}" ] || return 0
    command -v sudo >/dev/null || { echo "No sudo — skipping Start Menu entry."; return 0; }
    for f in "$sysdesktop" "$sysicon"; do
        if [ -e "$f" ] && [ ! -L "$f" ]; then
            echo "$f exists and is not our symlink (.deb install?) — skipping Start Menu entry."
            return 0
        fi
    done
    if [ "${1:-}" = "--remove" ]; then
        sudo rm -f "$sysdesktop" "$sysicon" || echo "sudo failed — $sysdesktop left behind."
        return 0
    fi
    echo "Linking the desktop entry into /usr/share for the Windows Start Menu (sudo)..."
    sudo mkdir -p "$(dirname "$sysicon")" \
        && sudo ln -sfn "$icon" "$sysicon" \
        && sudo ln -sfn "$desktop" "$sysdesktop" \
        || echo "sudo failed — no Start Menu entry. Re-run install.sh to retry."
}

if [ "${1:-}" = "--uninstall" ]; then
    wslg_link --remove
    rm -rf "$libdir" "$bin" "$desktop" "$icon"
    update-desktop-database "$(dirname "$desktop")" 2>/dev/null || true
    echo "Removed triolFM. Config left at ~/.config/triolfm/ — delete it yourself."
    exit 0
fi

python3 -c 'import PySide6.QtWidgets, PIL.Image' 2>/dev/null || {
    echo "Missing GUI deps. Run:"
    echo "  sudo apt install python3-pyside6.qtwidgets python3-pil"
    exit 1
}

mkdir -p "$libdir" "$(dirname "$bin")" "$(dirname "$desktop")" "$(dirname "$icon")"
cp "$here/triolfm.py" "$here/spotify_backend.py" "$libdir/"
cp "$here/icon.png" "$icon"

cat > "$bin" <<EOF
#!/bin/sh
exec python3 "$libdir/triolfm.py" "\$@"
EOF
chmod +x "$bin"

cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=triolFM
GenericName=Spotify Remote
Comment=Always-on-top Spotify controls, cover art and track info
Exec=$bin
Icon=triolfm
Terminal=false
StartupNotify=false
Categories=AudioVideo;Audio;Player;
EOF

update-desktop-database "$(dirname "$desktop")" 2>/dev/null || true
wslg_link

echo "Installed. Run 'triolfm' from any directory."
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "WARNING: $HOME/.local/bin is not on your PATH — add it to your shell rc." ;;
esac
[ -n "${WSL_DISTRO_NAME:-}" ] && echo "Windows Start Menu: search for 'triolFM ($WSL_DISTRO_NAME)'."
exit 0
