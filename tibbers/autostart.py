#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Starting tibbers when you sign in.

The installer offers this as a task and writes a Startup shortcut; this is the
same switch, available afterwards, in Settings. So the answer to "is it on?"
has to come from the machine rather than from a preference: a preference would
say "on" for someone who unticked the box at install, and "off" for someone who
ticked it, and both would be wrong until they touched the switch.

  Windows   a value under HKCU\\...\\CurrentVersion\\Run, and/or the installer's
            shortcut in the Startup folder. Either one counts as on. Turning it
            on writes the Run value and drops a leftover shortcut, so exactly
            one of the two survives and the app cannot be launched twice.
  macOS     a LaunchAgent in ~/Library/LaunchAgents. launchd reads the folder
            at login, so writing the file is the whole of it.

Only for a frozen build. A checkout is started by whoever is working on it.
"""

from __future__ import annotations

import logging
import os
import plistlib
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("tibbers.autostart")

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

#: The name under both the Run key and the Startup shortcut. The installer
#: writes the shortcut as "{#MyAppName}.lnk" -- keep the two in step.
APP_NAME = "Tibbers"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: The LaunchAgent's label, and its filename. Reverse-DNS, as launchd wants.
AGENT_LABEL = "lol.tibbers.autostart"

#: Started this way rather than plainly: the app comes up in the tray without
#: taking the foreground, which is the whole point of starting at sign-in.
QUIET = "--quiet"


def executable() -> Optional[Path]:
    """The binary to start, or None when running from a checkout."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def supported() -> bool:
    """Whether this build can put itself in the sign-in list at all.

    Not from a portable copy: writing to the registry is the one thing a
    portable app is not supposed to do, and the entry would outlive a folder
    that can be deleted or unplugged at any moment.
    """
    if os.environ.get("TIBBERS_PORTABLE"):
        return False
    return executable() is not None and (IS_WINDOWS or IS_MACOS)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _startup_shortcut() -> Optional[Path]:
    """The installer's Startup-folder shortcut, whether or not it is there."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / f"{APP_NAME}.lnk")


def _run_value() -> Optional[str]:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, APP_NAME)
        return str(value)
    except OSError:
        return None


def _windows_enabled() -> bool:
    if _run_value() is not None:
        return True
    shortcut = _startup_shortcut()
    return bool(shortcut and shortcut.is_file())


def _windows_enable(exe: Path) -> None:
    import winreg
    command = f'"{exe}" {QUIET}'
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
    # One mechanism, not two: a shortcut left from the installer would start a
    # second copy, which the instance mutex would turn into a puzzling
    # "tibbers is already running" at every sign-in.
    _remove_shortcut()


def _remove_shortcut() -> None:
    shortcut = _startup_shortcut()
    if shortcut is None:
        return
    try:
        shortcut.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("could not remove the Startup shortcut: %s", exc)


def _windows_disable() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except OSError:
        pass                       # not there, which is the wanted end state
    _remove_shortcut()


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _macos_enable(exe: Path) -> None:
    agent = _agent_path()
    agent.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [str(exe), QUIET],
        "RunAtLoad": True,
        # Started once at login and not kept alive: quitting tibbers from the
        # menu bar has to mean quitting it.
        "KeepAlive": False,
    }
    tmp = agent.with_suffix(".plist.part")
    with open(tmp, "wb") as out:
        plistlib.dump(payload, out)
    tmp.replace(agent)


def _macos_disable() -> None:
    try:
        _agent_path().unlink(missing_ok=True)
    except OSError as exc:
        log.debug("could not remove the LaunchAgent: %s", exc)


# ---------------------------------------------------------------------------
# What the app calls
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Whether signing in starts tibbers, read from the machine."""
    if not supported():
        return False
    if IS_WINDOWS:
        return _windows_enabled()
    return _agent_path().is_file()


def set_enabled(on: bool) -> bool:
    """Turn it on or off. Returns what it is afterwards, read back.

    Read back rather than assumed: this writes outside the app's own
    directory, where a policy or a locked-down profile can refuse.
    """
    exe = executable()
    if exe is None:
        return False
    try:
        if IS_WINDOWS:
            _windows_enable(exe) if on else _windows_disable()
        elif IS_MACOS:
            _macos_enable(exe) if on else _macos_disable()
    except OSError as exc:
        log.warning("could not %s start at sign-in: %s",
                    "enable" if on else "disable", exc)
    return enabled()
