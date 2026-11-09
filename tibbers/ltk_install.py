#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Installing LTK Manager from inside tibbers, when it is missing.

Skins are applied through LTK Manager, and an auto-update of tibbers runs its
installer silently, which never offers LTK (a silent update must not open
another program's setup by itself). So the app notices LTK is missing and
offers a button: it downloads LTK's official setup from LTK's latest release,
checks Windows accepts its signature, and runs it -- LTK's own wizard and its
own administrator prompt, in front of the user who asked for it.

Downloaded, never bundled: the patcher inside LTK Manager is under LTK's own
licence, which lets it travel with LTK's signature only in LTK's release
artifact.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("tibbers.ltk_install")

RELEASES_API = "https://api.github.com/repos/LeagueToolkit/ltk-manager/releases/latest"
#: LTK publishes its setup under a version-free name too, so this still finds
#: the newest one when the API is refusing us (a shared connection can be over
#: GitHub's unauthenticated limit for an hour at a time).
STABLE_SETUP = ("https://github.com/LeagueToolkit/ltk-manager/releases/latest/"
                "download/LTK.Manager_x64-setup.exe")
#: Only LTK's own releases are ever downloaded and run.
ALLOWED = "https://github.com/LeagueToolkit/ltk-manager/releases/"
USER_AGENT = "tibbers-ltk-install"

#: How long the setup may take once its window is up, and how long after it
#: exits LTK may take to appear (an installer that re-launches itself elevated
#: exits before the files land).
SETUP_TIMEOUT = 30 * 60
SETTLE_TIMEOUT = 60

Progress = Callable[[str, Optional[int]], None]


class InstallError(RuntimeError):
    """Something a user can act on, worded for them."""


def installed() -> bool:
    """True when LTK Manager is where tibbers looks for it."""
    from . import ltk_injector
    try:
        ltk_injector.manager_path()
    except FileNotFoundError:
        return False
    return True


def setup_url() -> str:
    """The newest official x64 setup's download link."""
    try:
        req = urllib.request.Request(RELEASES_API, headers={
            "Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.load(resp)
        for asset in release.get("assets") or []:
            url = str(asset.get("browser_download_url") or "")
            if url.endswith("_x64-setup.exe") and url.startswith(ALLOWED):
                return url
    except (OSError, ValueError) as exc:
        log.info("could not ask GitHub for LTK's latest release (%s); "
                 "using its stable link", exc)
    return STABLE_SETUP


def download(url: str, dest: Path, progress: Progress) -> Path:
    if not url.startswith(ALLOWED):
        raise InstallError(f"refusing to download from {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        for chunk in iter(lambda: resp.read(1 << 16), b""):
            out.write(chunk)
            done += len(chunk)
            progress("downloading", int(done * 100 / total) if total else None)
    return dest


def verify(path: Path) -> None:
    """Run nothing Windows does not accept as signed."""
    from . import diagnostics
    verdict = diagnostics.signature(path)
    if verdict != "signed":
        raise InstallError(f"LTK Manager's setup failed its signature check ({verdict})")


def run_setup(path: Path) -> int:
    """Start the setup through the shell and wait for it to exit.

    Through the shell rather than as a child: LTK installs per machine, so its
    setup asks for administrator rights, and only ShellExecute shows that
    prompt -- CreateProcess from this unelevated app is refused outright.
    """
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040          # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "open"
    info.lpFile = str(path)
    info.nShow = 1                   # SW_SHOWNORMAL: it is the user's wizard
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:            # ERROR_CANCELLED: the prompt was declined
            raise InstallError("the administrator prompt was declined")
        raise InstallError(f"LTK Manager's setup could not be started (error {error})")
    kernel32 = ctypes.WinDLL("kernel32")
    try:
        kernel32.WaitForSingleObject(info.hProcess, SETUP_TIMEOUT * 1000)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def wait_installed(timeout: float = SETTLE_TIMEOUT, poll: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if installed():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def install(work_dir: Path, progress: Progress) -> None:
    """Download, check and run LTK's setup; raise InstallError if LTK is not
    installed at the end of it."""
    work_dir.mkdir(parents=True, exist_ok=True)
    progress("resolving", None)
    url = setup_url()
    setup = work_dir / "ltk-manager-setup.exe"
    log.info("downloading LTK Manager's setup from %s", url)
    download(url, setup, progress)
    verify(setup)
    progress("installing", None)
    code = run_setup(setup)
    log.info("LTK Manager's setup exited with code %d", code)
    try:
        setup.unlink()
    except OSError:
        pass
    if not wait_installed():
        raise InstallError("LTK Manager's setup closed before it finished installing")
