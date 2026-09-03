#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Self-update from GitHub Releases.

The installed app is a frozen snapshot -- a `.app` on macOS, a PyInstaller
folder under `%LOCALAPPDATA%\\Programs\\Tibbers` on Windows -- published on
the repo's Releases. This checks the latest release, verifies and downloads
its asset, and installs it -- by itself once League is idle when the
`auto_update` preference is on, or when the user presses the button.

How the install is replaced differs by platform, on purpose:

* macOS: `Tibbers.zip` is unpacked and a tiny detached shell script waits for
  this process to exit, swaps the bundle and reopens it. A bundle is a plain
  directory and nothing else runs from it, so this is safe and simple.
* Windows: the asset is the Inno Setup installer itself, run silently. A
  running image cannot be overwritten on Windows and the patcher holder is
  Tibbers.exe, so a hand-rolled copy has to get file locks, retries, partial
  copies, relaunch and logging right -- and did not. The installer already
  does all of that (Restart Manager for in-use files, a log, exit codes) and
  is the same artefact a first install uses, so there is one path to test.

The two halves are separate on purpose: `stage` downloads (and unpacks), which
can be done any time and backed out of; `launch_swap` starts the replacement,
after which the install *will* change. The app decides between them whether
it is still a good moment to quit.

Nothing here elevates. Both install locations are writable by the ordinary
user. On macOS the patcher is detached from the app, so a mid-game update
leaves the skin on the game; on Windows the app stops the patcher first, and
therefore only updates once the game is over.
"""

from __future__ import annotations

import hashlib
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
    """The release asset for this platform: what the app downloads to update.

    On Windows that is the installer -- the very file the README's download
    button points at, so an update and a first install are the same thing.
    The build script and the release must use these exact names.
    """
    return "Tibbers-windows-setup.exe" if _IS_WINDOWS else "Tibbers.zip"


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
    url = digest = None
    for asset in data.get("assets") or []:
        if asset.get("name") == want:
            url = asset.get("browser_download_url")
            # GitHub publishes "sha256:<hex>" per asset; the download is
            # checked against it so a truncated or tampered file is never run.
            digest = asset.get("digest")
            break
    return {"tag": tag, "version": tag.lstrip("vV"), "url": url,
            "digest": digest, "name": data.get("name") or tag,
            "notes": data.get("body") or ""}


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
            "current": current, "url": rel["url"], "digest": rel["digest"],
            "notes": rel["notes"], "name": rel["name"]}


def _download(url: str, dest: Path, digest: Optional[str] = None) -> None:
    """Fetch *url* to *dest* and, when the release published a digest, refuse
    a file that does not match it."""
    req = urllib.request.Request(url, headers={"User-Agent": "tibbers-updater"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)
    verify_digest(dest, digest)


def verify_digest(path: Path, digest: Optional[str]) -> None:
    """Raise unless *path* hashes to *digest* ("sha256:<hex>", as GitHub's
    release API reports it). No digest means nothing to check against."""
    if not digest:
        log.debug("no digest published for %s; not verified", path.name)
        return
    algo, _, want = digest.partition(":")
    if algo != "sha256" or not want:
        log.debug("unrecognised digest %r; not verified", digest)
        return
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest().lower() != want.lower():
        raise RuntimeError(f"{path.name} does not match the release's checksum")


def _stage(url: str, digest: Optional[str] = None) -> Path:
    """Download and unpack the release, returning the new Tibbers.app path.

    `ditto` is used to unzip rather than `zipfile` so the bundle's symlinks and
    executable bits survive -- the same reason it is used to zip on release.
    """
    tmp = Path(tempfile.mkdtemp(prefix="tibbers-update-"))
    archive = tmp / ASSET
    _download(url, archive, digest)
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
# The asset is the Inno Setup installer, and "swapping" is running it silently.
# Everything a hand-rolled copy got wrong is the installer's job: it waits for
# the app's instance mutex to go (see scripts/tibbers.iss), asks the Restart
# Manager to close anything still holding a file, replaces the files, writes a
# log, exits with a documented code, and reopens the app `--quiet` because we
# ask it to with /RELAUNCH=1. No cmd, no robocopy, no console anywhere: the
# installer is a windowed program and /VERYSILENT shows nothing at all.

#: Setup switches for an unattended update. /NOCANCEL because nobody is there
#: to answer; /NORESTART because nothing here needs a reboot and a silent
#: one would be a disaster; the CLOSEAPPLICATIONS pair so a stale process
#: holding a file is closed rather than failing the install; /RELAUNCH is our
#: own parameter, read by the [Run] section of the .iss.
INSTALLER_SWITCHES = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                      "/NOCANCEL", "/CLOSEAPPLICATIONS",
                      "/FORCECLOSEAPPLICATIONS", "/RELAUNCH=1")


def _stage_windows(url: str, digest: Optional[str] = None) -> Path:
    """Download the installer into its own temporary directory."""
    tmp = Path(tempfile.mkdtemp(prefix="tibbers-update-"))
    setup = tmp / asset_name()
    _download(url, setup, digest)
    return setup


def installer_command(setup: Path, log_path: Optional[Path] = None) -> str:
    """The command line that runs a downloaded installer unattended.

    A string rather than a list: Inno reads `/LOG="path"` with the quotes
    around the value, which `subprocess`'s list quoting would not produce.
    """
    parts = [f'"{setup}"', *INSTALLER_SWITCHES]
    if log_path is not None:
        parts.append(f'/LOG="{log_path}"')
    return " ".join(parts)


def stage(url: str, digest: Optional[str] = None) -> Path:
    """Download (and on macOS unpack) the new build into a temporary directory.

    Nothing is touched yet: the result can sit there until it is a good
    moment to swap, or be thrown away with `discard`. *digest* is the
    release's published checksum; the download is refused if it differs.
    """
    return _stage_windows(url, digest) if _IS_WINDOWS else _stage(url, digest)


def discard(staged: Path) -> None:
    """Drop a staged build that will not be installed after all."""
    shutil.rmtree(staged.parent, ignore_errors=True)


def launch_swap(staged: Path, target: Optional[Path] = None,
                log_path: Optional[Path] = None) -> None:
    """Start the replacement of the install by a staged build.

    The caller must quit the app immediately after this returns: on macOS the
    swap script is waiting on this PID; on Windows the installer waits for
    the instance mutex to be released. Nothing else may be running from the
    Windows install either -- the patcher holder is Tibbers.exe, and a running
    image cannot be overwritten -- so the caller stops the patcher first. The
    installer's own log goes to *log_path* there.
    """
    target = target or installed_app()
    if target is None:
        raise RuntimeError("not running from an installed build")

    if _IS_WINDOWS:
        # A windowed program: no console is involved at any point. The flag
        # is passed anyway so every spawn in the package reads the same.
        subprocess.Popen(installer_command(staged, log_path),
                         close_fds=True, creationflags=0x08000000)
        return

    script = _swap_script(staged, target)
    subprocess.Popen(["/bin/bash", str(script), str(os.getpid())],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply(url: str, target: Optional[Path] = None,
          digest: Optional[str] = None) -> None:
    """Download the new build and launch the swap, in one go.

    The caller quits right after, as for `launch_swap`.
    """
    launch_swap(stage(url, digest), target)
