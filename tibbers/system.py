#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Platform layer: locating League, identifying its processes, running the
injector, and where data lives.

The interface is the same on every OS; the implementation is picked here. The
two platforms differ in more than paths -- macOS needs root for the fopen hook
(`task_for_pid`), while Windows injects a DLL into the same-user game process
with no elevation at all -- so each lives in its own module rather than behind
a thicket of `if platform` branches.

  _system_macos.py    the original, unchanged
  _system_windows.py  the Windows port
"""

from __future__ import annotations

import sys

if sys.platform.startswith("win"):
    from ._system_windows import *  # noqa: F401,F403
    # `_DATA_DIRS` is the cache the test fixtures clear to redirect data_dir
    # into a temp home; re-exported (as the same object) so those fixtures
    # keep working through the dispatcher.
    from ._system_windows import _DATA_DIRS  # noqa: F401
else:
    # macOS is the default; a Linux run would land here and fail loudly at the
    # first install lookup, which is the honest outcome until there is a port.
    from ._system_macos import *  # noqa: F401,F403
    from ._system_macos import _DATA_DIRS  # noqa: F401
