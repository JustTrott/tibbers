#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows specifics: locating League, identifying its processes, and running the
injector.

Mirrors the public interface of ``_system_macos`` so the rest of the app never
knows which one it got. The differences that matter:

* **No elevation.** cslol's Win32 patcher injects ``cslol-dll.dll`` into the
  same-user game process (``OpenProcess`` / ``CreateRemoteThread``), which
  needs no admin rights -- unlike the macOS ``task_for_pid`` hook, which needs
  root. So there is no sudoers rule, no helper, and no password prompt here;
  ``runoverlay`` just runs as the user.
* **One architecture.** Windows League is x64, so there is a single
  ``mod-tools.exe`` and no Rosetta detection.
* **Keeping the patcher alive.** ``runoverlay`` still exits the moment its
  stdin reaches EOF, so a detached holder process keeps that pipe open and
  outlives this app -- the same idea as the macOS ``sleep | runoverlay``, done
  with a tiny detached Python holder here.

NOTE: developed and reasoned about on macOS; the Windows-only paths (injection,
detach, process discovery) are validated on a Windows machine.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

log = logging.getLogger("tibbers.system")

# Windows executable names. The game keeps the spaces in its name here, unlike
# the macOS Mach-O "LeagueofLegends".
GAME_PROCESS = "League of Legends.exe"
CLIENT_PROCESS = "LeagueClient.exe"
CLIENT_UX_PROCESS = "LeagueClientUx.exe"

#: Riot's default install location. An override for a non-standard drive is
#: read from the running client anyway; this is only the fallback.
INSTALL_ROOTS = (
    Path(r"C:\Riot Games\League of Legends"),
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
    "Riot Games" / "League of Legends",
)

HOLDER_MARK = "tibbers-patcher-holder"


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def is_client_dir(path: Path) -> bool:
    return (Path(path) / "LeagueClient.exe").is_file()


def is_game_dir(path: Path) -> bool:
    path = Path(path)
    return ((path / "League of Legends.exe").is_file()
            and (path / "DATA" / "FINAL").is_dir())


_INSTALL_LOCK = threading.Lock()
_INSTALL: Optional[Tuple[Optional[Path], Optional[Path]]] = None
_INSTALL_AT = 0.0
_INSTALL_MISS_TTL = 5.0


def find_install() -> Tuple[Optional[Path], Optional[Path]]:
    """Return ``(game_dir, client_dir)``; see the macOS module for the caching
    rationale -- it is asked for on the champ-select poll."""
    global _INSTALL, _INSTALL_AT
    with _INSTALL_LOCK:
        if _INSTALL is not None and (_INSTALL[0] is not None
                                     or time.time() - _INSTALL_AT
                                     < _INSTALL_MISS_TTL):
            return _INSTALL

    found = _look_for_install()
    with _INSTALL_LOCK:
        _INSTALL, _INSTALL_AT = found, time.time()
    return found


def _look_for_install() -> Tuple[Optional[Path], Optional[Path]]:
    for proc in find_processes(CLIENT_PROCESS, CLIENT_UX_PROCESS):
        try:
            exe = proc.exe()
        except (psutil.Error, OSError):
            continue
        if not exe:
            continue
        # Walk up from the running client to the install root that holds it.
        for parent in [Path(exe).parent, *Path(exe).parents]:
            if is_client_dir(parent):
                game = parent / "Game"
                if is_game_dir(game):
                    return game, parent
    for root in INSTALL_ROOTS:
        if is_client_dir(root) and is_game_dir(root / "Game"):
            return root / "Game", root
    return None, None


def lockfile_path() -> Optional[Path]:
    """The LCU lockfile, written into the client directory while it runs."""
    _game, client = find_install()
    candidates = list(INSTALL_ROOTS)
    if client is not None:
        candidates.insert(0, client)
    for root in candidates:
        lf = root / "lockfile"
        if lf.is_file():
            return lf
    return None


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def find_processes(*names: str) -> List[psutil.Process]:
    """Processes whose executable basename is any of *names*."""
    wanted = set(names)
    found = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = proc.info.get("exe")
            name = proc.info.get("name")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        # Match on the exe basename, falling back to the process name -- on
        # Windows psutil usually has both for same-user processes.
        base = os.path.basename(exe) if exe else name
        if base and base in wanted:
            found.append(proc)
    return found


def find_game() -> Optional[psutil.Process]:
    procs = find_processes(GAME_PROCESS)
    return procs[0] if procs else None


def game_pid() -> Optional[int]:
    proc = find_game()
    return proc.pid if proc is not None else None


# --- the patcher ------------------------------------------------------------
#
# Unlike macOS, the patcher runs as the user, so psutil can read its command
# line directly -- no `ps` shell-out needed. The scan still goes through a
# cached table because the champ-select watcher asks on every change.

_TABLE_CACHE: Tuple[float, List[Tuple[int, str]]] = (0.0, [])
_TABLE_TTL = 1.0
_TABLE_LOCK = threading.Lock()


def process_table(fresh: bool = False) -> List[Tuple[int, str]]:
    """``(pid, command line)`` for every process this user can see."""
    global _TABLE_CACHE
    if not fresh:
        with _TABLE_LOCK:
            age, rows = _TABLE_CACHE
            if time.time() - age < _TABLE_TTL:
                return rows

    rows = _read_process_table()
    with _TABLE_LOCK:
        _TABLE_CACHE = (time.time(), rows)
    return rows


def _read_process_table() -> List[Tuple[int, str]]:
    rows = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline")
            pid = proc.info.get("pid")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if pid is not None and cmd:
            rows.append((int(pid), " ".join(cmd)))
    return rows


def runoverlay_pids(overlay: Optional[Path] = None,
                    fresh: bool = False) -> List[int]:
    """Live ``mod-tools runoverlay`` processes, optionally only for *overlay*."""
    want = str(overlay) if overlay is not None else None
    found = []
    for pid, command in process_table(fresh=fresh):
        if "mod-tools" not in command or " runoverlay " not in f" {command} ":
            continue
        # The holder's own command line carries the whole runoverlay command
        # too, so it would otherwise be counted as a second patcher.
        if HOLDER_MARK in command:
            continue
        if want is not None and want not in command:
            continue
        found.append(pid)
    return found


def holder_pids(overlay: Optional[Path] = None,
                fresh: bool = True) -> List[int]:
    """The detached holders keeping the patcher's stdin open."""
    want = str(overlay) if overlay is not None else None
    found = []
    for pid, command in process_table(fresh=fresh):
        if HOLDER_MARK not in command:
            continue
        if want is not None and want not in command:
            continue
        found.append(pid)
    return found


# ---------------------------------------------------------------------------
# Architecture (trivial on Windows -- always x64)
# ---------------------------------------------------------------------------

def is_translated(pid: int) -> Optional[bool]:
    return False


def select_modtools(tools_dir: Path, pid: Optional[int] = None) -> Path:
    """The single Windows mod-tools build. ``cslol-dll.dll`` must sit beside
    it -- ``fetch_modtools`` installs the pair together."""
    tools_dir = Path(tools_dir)
    exe = tools_dir / "mod-tools.exe"
    if not exe.exists():
        raise FileNotFoundError(f"mod-tools.exe is missing at {exe}")
    return exe


def modtools_arch(modtools: Path) -> str:
    return "x86_64"


# ---------------------------------------------------------------------------
# Injection (no elevation on Windows)
# ---------------------------------------------------------------------------

def runoverlay_command(modtools: Path, overlay: Path, config: Path,
                       game_dir: Path) -> List[str]:
    return [
        str(modtools), "runoverlay", str(overlay), str(config),
        f"--game:{game_dir}", "--opts:configless",
    ]


def manual_command(modtools: Path, overlay: Path, config: Path,
                   game_dir: Path) -> str:
    """The equivalent command a user could run by hand -- no elevation."""
    return subprocess.list2cmdline(
        runoverlay_command(modtools, overlay, config, game_dir))


# runoverlay exits the instant its stdin reaches EOF, so a detached holder
# keeps the pipe open and outlives this app. The holder carries HOLDER_MARK
# and the overlay path in its own argv so it can be found and adopted later,
# exactly like the macOS holder shell.
_HOLDER_SCRIPT = (
    "import subprocess,sys,time\n"
    "log=open(sys.argv[3],'ab')\n"
    "p=subprocess.Popen(sys.argv[4:],stdin=subprocess.PIPE,"
    "stdout=log,stderr=log)\n"
    "try:\n"
    "    time.sleep(10**9)\n"
    "finally:\n"
    "    p.terminate()\n"
)

# Windows process-creation flags for a fully detached, windowless holder.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def spawn_runoverlay_detached(modtools: Path, overlay: Path, config: Path,
                              game_dir: Path, log_path: Path,
                              detached: bool = True):
    """Start runoverlay as the user, detached, with its output captured.

    A holder Python process owns runoverlay's stdin and sleeps forever, so the
    patcher keeps waiting for the game across a restart of this app. Nothing
    about the injection command changes; only who holds the pipe.
    """
    import sys

    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    command = runoverlay_command(modtools, overlay, config, game_dir)
    holder_argv = [sys.executable, "-c", _HOLDER_SCRIPT,
                   HOLDER_MARK, str(overlay), str(log_path), *command]

    flags = _CREATE_NO_WINDOW
    if detached:
        flags |= _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP

    try:
        subprocess.Popen(
            holder_argv,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError(f"could not start the patcher holder: {exc}") from exc
    # The caller confirms success from the patcher log, as on macOS.
    return None


def _terminate(pids: List[int]) -> int:
    killed = 0
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def kill_runoverlay() -> None:
    """Stop the patcher. No elevation: it runs as the user on Windows."""
    _terminate(runoverlay_pids(fresh=True))
    _terminate(holder_pids(fresh=True))


def kill_holders(overlay: Optional[Path] = None) -> int:
    """Reap the stdin holders for *overlay*; killing one drops its runoverlay
    child with it (the holder terminates the child on the way out, and a closed
    pipe would end it anyway)."""
    return _terminate(holder_pids(overlay))


# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------

_DATA_DIRS: Dict[str, Path] = {}
_DATA_LOCK = threading.Lock()


def data_dir() -> Path:
    """Where the library, the built overlay and preferences live.

    ``%LOCALAPPDATA%\\tibbers`` by default, overridable with TIBBERS_HOME so a
    second instance never touches the real one.
    """
    override = os.environ.get("TIBBERS_HOME") or ""
    with _DATA_LOCK:
        made = _DATA_DIRS.get(override)
    if made is not None:
        return made

    if override:
        d = Path(override).expanduser()
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        d = Path(base) / "tibbers"
    d.mkdir(parents=True, exist_ok=True)
    with _DATA_LOCK:
        _DATA_DIRS[override] = d
    return d
