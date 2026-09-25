#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Every third-party module the app imports is installed where it is built.

PyInstaller bundles what the build environment has; a module imported inside
a function and absent there is simply left out, and the app only finds out at
the moment it reaches for it. That is how party sharing went out without
websocket-client: the lobby thread died on its first line and no error ever
reached the screen.
"""

from __future__ import annotations

import importlib
import sys
import unittest

#: Import name -> why the app needs it.
REQUIRED = {
    "psutil": "finding League's processes",
    "xxhash": "wad path hashes",
    "zstandard": "wad chunks",
    "websocket": "party sharing's relay connection",
}
WINDOWS = {
    "webview": "the picker window",
    "pystray": "the tray icon",
    "PIL": "the tray icon's image",
}


class Installed(unittest.TestCase):

    def test_the_modules_the_app_imports_are_installed(self):
        need = dict(REQUIRED)
        if sys.platform.startswith("win"):
            need.update(WINDOWS)
        for name, why in need.items():
            with self.subTest(name):
                try:
                    importlib.import_module(name)
                except ImportError:
                    self.fail(f"{name} is not installed; it is needed for {why}")


if __name__ == "__main__":
    unittest.main()
