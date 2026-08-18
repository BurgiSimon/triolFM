#!/bin/sh
# Build dist/triolFM.app. macOS only — py2app makes bundles, nothing else does.
#
# Needs a Python with Tk: the python.org installer ships it, Homebrew needs
# 'brew install python-tk'. The result is unsigned, so the first launch has to
# be right-click -> Open (see the README).
set -eu

here=$(cd "$(dirname "$0")" && pwd)
cd "$here"

[ "$(uname)" = "Darwin" ] || {
    echo "macOS only. On Linux use ./install.sh or ./build-deb.sh."
    exit 1
}
python3 -c 'import tkinter' 2>/dev/null || {
    echo "This Python has no tkinter. Install python.org's build, or:"
    echo "  brew install python-tk"
    exit 1
}

# .icns from the single 256px PNG we ship. macOS wants every size present or
# the Dock silently falls back to a generic icon.
rm -rf icon.iconset icon.icns
mkdir icon.iconset
for s in 16 32 128 256 512; do
    sips -z "$s" "$s" icon.png --out "icon.iconset/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2))
    sips -z "$d" "$d" icon.png --out "icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns icon.iconset -o icon.icns
rm -rf icon.iconset

python3 -m pip install --quiet --upgrade py2app pillow
rm -rf build dist
python3 setup.py py2app

echo
echo "Built dist/triolFM.app — drag it into /Applications."
echo "First launch: right-click the app -> Open -> Open. It is unsigned, so a"
echo "plain double-click gets refused by Gatekeeper."
