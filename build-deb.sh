#!/bin/sh
# Build triolfm_<version>_all.deb. Needs only dpkg-deb, which ships with Debian
# and Ubuntu. Install the result with:
#
#   sudo apt install ./triolfm_1.0.0_all.deb
#
# apt pulls in python3-tk and python3-pil.imagetk for you, and removal is
# 'sudo apt remove triolfm'.
set -eu

VERSION=${VERSION:-1.0.0}
MAINTAINER=${MAINTAINER:-"triolFM <simonburgi09@gmail.com>"}

here=$(cd "$(dirname "$0")" && pwd)
stage="$here/build/triolfm_${VERSION}_all"
deb="$here/triolfm_${VERSION}_all.deb"

rm -rf "$stage"
mkdir -p "$stage/DEBIAN" \
         "$stage/usr/bin" \
         "$stage/usr/share/triolfm" \
         "$stage/usr/share/applications" \
         "$stage/usr/share/icons/hicolor/256x256/apps" \
         "$stage/usr/share/doc/triolfm"

install -m 644 "$here/triolfm.py" "$stage/usr/share/triolfm/triolfm.py"
install -m 644 "$here/icon.png" \
    "$stage/usr/share/icons/hicolor/256x256/apps/triolfm.png"

cat > "$stage/usr/bin/triolfm" <<'EOF'
#!/bin/sh
exec python3 /usr/share/triolfm/triolfm.py "$@"
EOF
chmod 755 "$stage/usr/bin/triolfm"

cat > "$stage/usr/share/applications/triolfm.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=triolFM
GenericName=Spotify Remote
Comment=Always-on-top Spotify controls, cover art and track info
Exec=/usr/bin/triolfm
Icon=triolfm
Terminal=false
StartupNotify=false
Categories=AudioVideo;Audio;Player;
EOF
chmod 644 "$stage/usr/share/applications/triolfm.desktop"

cat > "$stage/DEBIAN/control" <<EOF
Package: triolfm
Version: $VERSION
Section: sound
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-tk, python3-pil, python3-pil.imagetk
Maintainer: $MAINTAINER
Description: Always-on-top Spotify remote widget
 A small borderless widget that shows cover art, track info and a progress
 bar, and drives whatever device the Spotify app is already playing on. It
 does not play audio itself and does not replace the Spotify desktop app.
 .
 Controls the Spotify Web API, so playback control needs a Premium account.
 First run walks through a one-time OAuth login in the browser.
EOF

cat > "$stage/usr/share/doc/triolfm/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: triolFM

Files: *
Copyright: 2026 $MAINTAINER
License: none
 No licence has been chosen for this package. All rights reserved by the
 copyright holder. It is built for personal use and not for redistribution.
EOF
chmod 644 "$stage/usr/share/doc/triolfm/copyright"

cat > "$stage/usr/share/doc/triolfm/changelog.Debian" <<EOF
triolfm ($VERSION) unstable; urgency=low

  * Initial release.

 -- $MAINTAINER  $(date -R)
EOF
gzip -9n "$stage/usr/share/doc/triolfm/changelog.Debian"

dpkg-deb --root-owner-group --build "$stage" "$deb" >/dev/null
rm -rf "$here/build"
echo "Built $deb"
echo "Install with: sudo apt install $deb"
