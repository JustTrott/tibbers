#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provisioning the Windows injection tools at runtime.

The packaged app does not carry the patcher binaries. cslol's `cslol-dll.dll`
is an unlicensed vendored blob and LTK's patcher is signed by its own
publisher -- bundling either would mean redistributing something that is not
ours to redistribute (and, for LTK, stripping their signature). Fetching them
onto the user's own machine at first run sidesteps all of that: the same two
pairs `scripts/fetch_modtools.ps1` installs for a source checkout, but done in
Python so the frozen app can do it with no PowerShell script on disk.

  mod-tools.exe + cslol-dll.dll         from cslol-manager  -> mkoverlay only
  ltk_patcher_host.exe + ltk_patcher_dll.dll  from LTK Manager  -> injection

Both are fetched into the data directory (`%LOCALAPPDATA%\\tibbers\\tools`),
which is writable, unlike the install directory under Program Files.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

from . import system

log = logging.getLogger("tibbers.wintools")

CSLOL_LATEST = ("https://api.github.com/repos/LeagueToolkit/"
                "cslol-manager/releases/latest")
LTK_LATEST = ("https://api.github.com/repos/LeagueToolkit/"
              "ltk-manager/releases/latest")

CSLOL_ASSET = "cslol-manager-windows.exe"

#: The four files a working Windows install needs, and which pair each is in.
CSLOL_FILES = ("mod-tools.exe", "cslol-dll.dll")
LTK_FILES = ("ltk_patcher_host.exe", "ltk_patcher_dll.dll")


def tools_dir() -> Path:
    """Where the fetched Windows tools live: writable, beside the data dir."""
    d = system.data_dir() / "tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def have_tools(where: Optional[Path] = None) -> bool:
    """True when both pairs are already present in *where*."""
    where = Path(where) if where is not None else tools_dir()
    return all((where / name).exists()
               for name in (*CSLOL_FILES, *LTK_FILES))


#: A progress sink takes a message and an optional 0-100 percent. The app maps
#: it onto a bar; the CLI and the log just print the message.
Progress = Callable[[str, Optional[int]], None]


def _report(progress: Optional[Progress], message: str,
            percent: Optional[int] = None) -> None:
    if progress:
        progress(message, percent)
    else:
        log.info(message)


def _latest_asset(api_url: str, match: Callable[[dict], bool]) -> dict:
    req = urllib.request.Request(api_url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "tibbers-wintools",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    for asset in data.get("assets") or []:
        if match(asset):
            return asset
    raise RuntimeError(f"no matching asset in {api_url}")


def _download(url: str, dest: Path, progress: Optional[Progress] = None,
              label: str = "downloading") -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "tibbers-wintools"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = -1
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total and progress is not None:
                pct = int(done * 100 / total)
                # Report on whole-percent changes only, so a slow link does not
                # flood the state with near-identical updates.
                if pct != last:
                    last = pct
                    _report(progress, f"{label} {done // 1048576}/"
                            f"{total // 1048576} MB", pct)


def _find(root: Path, name: str) -> Optional[Path]:
    for found in root.rglob(name):
        return found
    return None


def _install_pair(source_dir: Path, names, into: Path) -> None:
    """Copy a load-time pair together; the second must sit beside the first."""
    for name in names:
        src = source_dir / name
        if not src.exists():
            raise RuntimeError(f"{name} was not found beside its pair in "
                               f"{source_dir}")
    for name in names:
        shutil.copy2(source_dir / name, into / name)


def _fetch_cslol(into: Path, progress) -> None:
    asset = _latest_asset(CSLOL_LATEST, lambda a: a.get("name") == CSLOL_ASSET)
    tmp = Path(tempfile.mkdtemp(prefix="tibbers-cslol-"))
    try:
        sfx = tmp / CSLOL_ASSET
        _download(asset["browser_download_url"], sfx,
                  progress, "downloading overlay builder")
        extract = tmp / "x"
        extract.mkdir()
        _report(progress, "extracting overlay builder...")
        # The Windows release is a 7-Zip console self-extractor; -o/-y unpack
        # it without a GUI. CREATE_NO_WINDOW keeps the windowed app from popping
        # a console for it (see system.CREATE_NO_WINDOW).
        subprocess.run([str(sfx), f"-o{extract}", "-y"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, creationflags=system.CREATE_NO_WINDOW)
        modtools = _find(extract, "mod-tools.exe")
        if modtools is None:
            raise RuntimeError("mod-tools.exe not found after extraction")
        _install_pair(modtools.parent, CSLOL_FILES, into)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch_ltk(into: Path, progress) -> None:
    asset = _latest_asset(
        LTK_LATEST, lambda a: str(a.get("name", "")).lower().endswith(".msi"))
    tmp = Path(tempfile.mkdtemp(prefix="tibbers-ltk-"))
    try:
        msi = tmp / asset["name"]
        _download(asset["browser_download_url"], msi,
                  progress, "downloading injection patcher")
        extract = tmp / "x"
        _report(progress, "extracting injection patcher...")
        # Administrative install: unpacks the payload with NO install -- no
        # service, no registry, no Vanguard interaction. Windowless as above.
        subprocess.run(["msiexec.exe", "/a", str(msi), "/qn",
                        f"TARGETDIR={extract}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, creationflags=system.CREATE_NO_WINDOW)
        host = _find(extract, "ltk_patcher_host.exe")
        if host is None:
            raise RuntimeError("ltk_patcher_host.exe not found after extraction")
        _install_pair(host.parent, LTK_FILES, into)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure(where: Optional[Path] = None,
           progress: Optional[Progress] = None,
           force: bool = False) -> Path:
    """Make both tool pairs present in *where*, fetching what is missing.

    Returns the tools directory. Raises on a failed fetch; the caller decides
    whether that is fatal (it is, for injection) or merely deferred.
    """
    where = Path(where) if where is not None else tools_dir()
    where.mkdir(parents=True, exist_ok=True)

    if force or not all((where / n).exists() for n in CSLOL_FILES):
        _fetch_cslol(where, progress)
    if force or not all((where / n).exists() for n in LTK_FILES):
        _fetch_ltk(where, progress)

    _report(progress, "injection tools ready")
    return where


def missing(where: Optional[Path] = None) -> List[str]:
    """Which of the four files are not yet present."""
    where = Path(where) if where is not None else tools_dir()
    return [n for n in (*CSLOL_FILES, *LTK_FILES) if not (where / n).exists()]
