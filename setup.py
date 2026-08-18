"""macOS .app bundle for triolFM.

Run ./build-app.sh rather than this file directly — it builds icon.icns first
and installs py2app. Linux and Windows don't use this; they have install.sh and
build-deb.sh.
"""
import os
import re

from setuptools import setup

VERSION = re.search(r'^__version__ = "(.*)"',
                    open("triolfm.py").read(), re.M).group(1)

OPTIONS = {
    "packages": ["PIL"],
    "plist": {
        "CFBundleName": "triolFM",
        "CFBundleDisplayName": "triolFM",
        "CFBundleIdentifier": "com.burgisimon.triolfm",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHumanReadableCopyright": "Copyright (c) 2026 Simon Burgi, MIT",
        "LSMinimumSystemVersion": "11.0",
    },
}
if os.path.exists("icon.icns"):  # build-app.sh makes it; without it, generic
    OPTIONS["iconfile"] = "icon.icns"

setup(
    name="triolFM",
    version=VERSION,
    app=["triolfm.py"],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
    install_requires=["pillow"],
)
