#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Which language the windows are drawn in.

The pages carry their own dictionaries (`static/index.html`,
`static/settings.html`); this only decides the code they are handed. The
`language` setting is "system", or one of `SUPPORTED`; "system" follows the
OS display language and falls back to English, so a Russian Windows gets
Russian without anyone opening Settings, and everyone else gets English.

Status lines and the log stay English: they are the record of what the app
did, read back in bug reports, and a translated log is one nobody can grep.
"""

from __future__ import annotations

import locale
import os
import sys
from typing import Optional

SUPPORTED = ("en", "ru")
DEFAULT = "en"


def _windows_ui_language() -> Optional[str]:
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:  # noqa: BLE001
        return None
    # The primary language is the low ten bits; 0x19 is Russian.
    primary = langid & 0x3FF
    return {0x09: "en", 0x19: "ru"}.get(primary)


def _posix_language() -> Optional[str]:
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return value[:2].lower()
    try:
        code = locale.getlocale()[0]
    except Exception:  # noqa: BLE001
        return None
    return code[:2].lower() if code else None


def system_language() -> str:
    """The OS display language, as one of `SUPPORTED`, else English."""
    if sys.platform.startswith("win"):
        code = _windows_ui_language()
    else:
        code = _posix_language()
    return code if code in SUPPORTED else DEFAULT


def resolve(choice: Optional[str]) -> str:
    """The language a page should draw in, for the stored *choice*."""
    if choice in SUPPORTED:
        return choice
    return system_language()
