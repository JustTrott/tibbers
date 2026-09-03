#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Self-update from GitHub Releases.

The installed app is a frozen snapshot -- a `.app` on macOS, a PyInstaller
folder under `%LOCALAPPDATA%\\Programs\\Tibbers` on Windows -- published on the
repo's Releases (`Tibbers.zip` for macOS, `Tibbers-windows.zip` for Windows).
This checks the latest release, and downloads it and swaps it into place --
by itself once League is idle when the `auto_update` preference is on, or when
the user presses the button. The swap is done by a tiny detached script that
waits for this process to exit first, because a running app cannot overwrite
itself; the app quits right after launching it, and the script reopens the new
one.

The two halves are separate on purpose: `stage` downloads and unpacks, which
can be done any time and backed out of; `launch_swap` starts the script, after
which the install *will* be replaced. The app decides between them whether it
is still a good moment to quit.

Nothing here elevates. Both install locations are writable by the ordinary
user, and the patcher is detached, so a mid-game update leaves the skin on the
game and the next launch adopts it -- exactly what a normal restart does.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from . import __version__

log = logging.getLogger("tibbers.update")

REPO = "JustTrott/tibbers"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

#: How long the running app waits before asking GitHub again. Releases are
#: days apart, and the unauthenticated API allows sixty calls an hour per
#: address, which every other app on the machine shares.
CHECK_INTERVAL = 6 * 60 * 60
#: Opening Settings re-checks too, but not more often than this: the page
#: polls every two seconds and must not turn into a request per poll.
SETTINGS_RECHECK = 10 * 60
#: How often the loop looks whether a found update can be installed yet.
TICK = 30

#: Client phases in which nothing is going on that a restart would cut into.
#: Matchmaking is deliberately not here: a queue pop while the app is being
#: swapped would open champ select with no picker.
IDLE_PHASES = (None, "None", "Lobby")


def league_idle(phase: Optional[str], game_running: bool) -> bool:
    """Whether the app can quit and reopen now without anyone noticing.

    Champ select is where tibbers is in use, and a running game is being
    served by the patcher -- which survives the app (it is detached), but the
    picker and its build do not, so both wait. No client at all, or the client
    sitting in the lobby, is fine.
    """
    return phase in IDLE_PHASES and not game_running

_IS_WINDOWS = sys.platform.startswith("win")


def asset_name() -> str:
    """The release asset for this platform. The README's download button and
    the release job must use the same names."""
    return "Tibbers-windows.zip" if _IS_WINDOWS else "Tibbers.zip"


#: Back-compat alias; the macOS name, kept for any old caller.
ASSET = "Tibbers.zip"


def installed_app() -> Optional[Path]:
    """The installed app directory this code runs from, or None from source.

    Self-update only makes sense for the frozen build; a checkout updates
    itself with git, so this returns None there and every entry point no-ops.
    macOS: the `.app` bundle. Windows: the PyInstaller folder that holds
    `Tibbers.exe`, which is where the frozen `sys.executable` lives.
    """
    if _IS_WINDOWS:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return None
    for parent in Path(__file__).resolve().parents:
        if parent.name.endswith(".app"):
            return parent
    return None


def _version_tuple(text: str) -> tuple:
    """A comparable version, stopping at the first non-numeric part."""
    out = []
    for piece in str(text).lstrip("vV").split("."):
        if not piece.isdigit():
            break
        out.append(int(piece))
    return tuple(out)


def latest_release() -> dict:
    """The newest release: its tag, notes, and the Tibbers.zip download URL."""
    req = urllib.request.Request(LATEST_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "tibbers-updater",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    tag = data.get("tag_name") or ""
    want = asset_name()
    url = None
    for asset in data.get("assets") or []:
        if asset.get("name") == want:
            url = asset.get("browser_download_url")
            break
    return {"tag": tag, "version": tag.lstrip("vV"), "url": url,
            "name": data.get("name") or tag, "notes": data.get("body") or ""}


def check(current: str = __version__) -> dict:
    """Whether a newer build exists. Network errors are swallowed into it.

    Returns a dict the settings page renders directly: `available`, `version`,
    `current`, `url`, `notes`, and on failure an `error`.
    """
    try:
        rel = latest_release()
    except Exception as exc:  # noqa: BLE001 -- offline is a normal outcome here
        log.debug("update check failed: %s", exc)
        return {"available": False, "current": current, "error": str(exc)}
    if not rel["url"]:
        return {"available": False, "current": current,
                "error": f"the latest release has no {asset_name()}"}
    available = _version_tuple(rel["version"]) > _version_tuple(current)
    return {"available": available, "version": rel["version"],
            "current": current, "url": rel["url"], "notes": rel["notes"],
            "name": rel["name"]}


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "tibbers-updater"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _stage(url: str) -> Path:
    """Download and unpack the release, returning the new Tibbers.app path.

    `ditto` is used to unzip rather than `zipfile` so the bundle's symlinks and
    executable bits survive -- the same reason it is used to zip on release.
    """
    tmp = Path(tempfile.mkdtemp(prefix="tibbers-update-"))
    archive = tmp / ASSET
    _download(url, archive)
    subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(tmp)],
                   check=True)
    app = tmp / "Tibbers.app"
    if not app.exists():
        found = list(tmp.glob("*.app"))
        if not found:
            raise RuntimeError(f"no .app inside {ASSET}")
        app = found[0]
    return app


def _swap_script(new_app: Path, target: Path) -> Path:
    """A detached script: wait for us to exit, replace the app, reopen it."""
    body = f"""#!/bin/bash
PID="$1"
# Wait (up to ~40s) for the running app to exit before touching its bundle.
for _ in $(seq 1 200); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done
rm -rf {shlex.quote(str(target))}
/usr/bin/ditto {shlex.quote(str(new_app))} {shlex.quote(str(target))}
rm -rf {shlex.quote(str(new_app.parent))}
# -g: reopen in the background, without stealing focus from a game.
/usr/bin/open -g {shlex.quote(str(target))}
"""
    fd, path = tempfile.mkstemp(prefix="tibbers-swap-", suffix=".sh")
    with os.fdopen(fd, "w") as handle:
        handle.write(body)
    return Path(path)


# --- Windows -----------------------------------------------------------------
#
# The frozen build is a PyInstaller folder, not a bundle, so the zip is plain
# and `zipfile` unpacks it (no symlinks or exec bits to preserve). The swap is
# a detached .bat because it must outlive this process and needs no dependency
# the machine might lack: it waits for our PID to exit, mirrors the new folder
# over the old with robocopy, and relaunches quietly. `--quiet` comes up in the
# tray without stealing focus from a game, matching the macOS `open -g`.

def _stage_windows(url: str) -> Path:
    import zipfile

    tmp = Path(tempfile.mkdtemp(prefix="tibbers-update-"))
    archive = tmp / asset_name()
    _download(url, archive)
    extract = tmp / "unpacked"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(extract)
    exe = next(extract.rglob("Tibbers.exe"), None)
    if exe is None:
        raise RuntimeError(f"no Tibbers.exe inside {asset_name()}")
    return exe.parent


def _swap_script_windows(new_dir: Path, target: Path,
                         log_path: Optional[Path] = None) -> Path:
    """The .bat that replaces the install once this process has exited.

    Two things it must not do, both learnt the hard way. It must not retry
    forever: robocopy's default is a million retries thirty seconds apart,
    and Tibbers.exe is locked by any running copy -- the patcher holder
    included, which *is* Tibbers.exe -- so a swap started with one alive hung
    for good. And it must not leave a half-replaced install: the exe is
    copied first and alone, and only when that worked is the rest mirrored
    over it. Nor does it open a second copy when one is already running.
    Everything it does goes to *log_path*, since nobody is watching.
    """
    log = str(log_path) if log_path else "nul"
    q = lambda path: f'"{path}"'  # noqa: E731
    ro = "/R:30 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP"
    stamp = "echo [%date% %time%] update:"
    body = "\r\n".join([
        "@echo off",
        'set "PID=%~1"',
        f'set "LOG={log}"',
        f'{stamp} waiting for pid %PID% to exit >> "%LOG%"',
        ":wait",
        'tasklist /fi "PID eq %PID%" | find "%PID%" >nul '
        "&& ( ping -n 2 127.0.0.1 >nul & goto wait )",
        f'robocopy {q(new_dir)} {q(target)} Tibbers.exe {ro} >> "%LOG%" 2>&1',
        "if errorlevel 8 (",
        f'  {stamp} Tibbers.exe is in use -- the install was left as it was '
        '>> "%LOG%"',
        "  goto done",
        ")",
        f'robocopy {q(new_dir)} {q(target)} /MIR {ro} >> "%LOG%" 2>&1',
        "if errorlevel 8 (",
        f'  {stamp} copying the new build failed >> "%LOG%"',
        "  goto done",
        ")",
        f'{stamp} installed >> "%LOG%"',
        'tasklist /fi "imagename eq Tibbers.exe" | find /i "Tibbers.exe" >nul '
        "&& (",
        f'  {stamp} tibbers is already open, not reopening it >> "%LOG%"',
        f') || start "" {q(target / "Tibbers.exe")} --quiet',
        ":done",
        f"rmdir /s /q {q(new_dir.parent)} >nul 2>&1",
        "",
    ])
    fd, path = tempfile.mkstemp(prefix="tibbers-swap-", suffix=".bat")
    with os.fdopen(fd, "w", newline="") as handle:
        handle.write(body)
    return Path(path)


# CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP -- and not DETACHED_PROCESS.
# Windows ignores CREATE_NO_WINDOW next to DETACHED_PROCESS, and a cmd with no
# console at all hands every tasklist, find and robocopy it runs a console
# window of its own: the empty terminal that sat over the desktop during the
# update. A child of a windowed app outlives its parent without being detached.
_SWAP_FLAGS = 0x08000000 | 0x00000200


def stage(url: str) -> Path:
    """Download and unpack the new build into a temporary directory.

    Nothing is touched yet: the result can sit there until it is a good
    moment to swap, or be thrown away with `discard`.
    """
    return _stage_windows(url) if _IS_WINDOWS else _stage(url)


def discard(staged: Path) -> None:
    """Drop a staged build that will not be installed after all."""
    shutil.rmtree(staged.parent, ignore_errors=True)


def launch_swap(staged: Path, target: Optional[Path] = None,
                log_path: Optional[Path] = None) -> None:
    """Start the detached swap of a staged build over the install.

    The caller must quit the app immediately after this returns, so the swap
    script -- already waiting on this PID -- can replace the install and reopen
    it. On Windows nothing else may be running from the install either: the
    patcher holder is Tibbers.exe, and a running image cannot be overwritten,
    so the caller stops the patcher first. What the script did is written to
    *log_path* there.
    """
    target = target or installed_app()
    if target is None:
        raise RuntimeError("not running from an installed build")

    if _IS_WINDOWS:
        script = _swap_script_windows(staged, target, log_path)
        subprocess.Popen(["cmd", "/c", str(script), str(os.getpid())],
                         creationflags=_SWAP_FLAGS, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    script = _swap_script(staged, target)
    subprocess.Popen(["/bin/bash", str(script), str(os.getpid())],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply(url: str, target: Optional[Path] = None) -> None:
    """Download the new build and launch the swap, in one go.

    The button's path: the user asked, so there is no waiting for a good
    moment. The caller quits right after, as for `launch_swap`.
    """
    launch_swap(stage(url), target)
