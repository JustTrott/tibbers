#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Desktop shell: the picker and settings windows, and the background presence
that keeps tibbers watching champ select without holding a Dock slot.

Both platforms drive pywebview for the windows themselves; they differ in the
background presence -- a macOS menu-bar item (AppKit ``NSStatusItem``) versus a
Windows system-tray icon (``pystray``) -- and in the small activation dance
macOS needs and Windows does not. Each lives in its own module.

  _shell_macos.py    the original, unchanged
  _shell_windows.py  the Windows port (pywebview windows + a pystray tray)
"""

from __future__ import annotations

import sys

if sys.platform.startswith("win"):
    from ._shell_windows import *  # noqa: F401,F403
else:
    from ._shell_macos import *  # noqa: F401,F403
