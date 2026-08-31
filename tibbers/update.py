#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Self-update from GitHub Releases.

The installed app is a self-contained `.app` published as `Tibbers.zip` on the
repo's Releases. This checks the latest release, and -- when the user asks --
downloads it and swaps it into place. The swap is done by a tiny detached
script that waits for this process to exit first, because a running bundle
cannot overwrite itself; the app quits right after launching it, and the script
reopens the new one.

Nothing here elevates. `/Applications` is writable by an admin user (the same
way `build_app.sh --install` copies into it without sudo), and the patcher is
detached, so a mid-game update leaves the skin on the game and the next launch
adopts it -- exactly what a normal restart does.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from . import __version__

log = logging.getLogger("tibbers.update")

REPO = "JustTrott/tibbers"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
#: The asset name the release must carry, and the one the README's download
#: button points at (releases/latest/download/Tibbers.zip).
ASSET = "Tibbers.zip"


def installed_app() -> Optional[Path]:
    """The `.app` this code is running from, or None when run from source.

    Self-update only makes sense for the installed bundle; a checkout updates
    itself with git, so this returns None there and every entry point no-ops.
    """
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
    url = None
    for asset in data.get("assets") or []:
        if asset.get("name") == ASSET:
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
                "error": f"the latest release has no {ASSET}"}
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


def apply(url: str, target: Optional[Path] = None) -> None:
    """Download the new build and launch the detached swap.

    The caller must quit the app immediately after this returns, so the swap
    script -- which is already waiting on this PID -- can replace the bundle
    and reopen it.
    """
    target = target or installed_app()
    if target is None:
        raise RuntimeError("not running from an installed .app")
    new_app = _stage(url)
    script = _swap_script(new_app, target)
    subprocess.Popen(["/bin/bash", str(script), str(os.getpid())],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
