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

The injection itself is byte-for-byte what cslol-manager and Rose run: the same
``mod-tools.exe``, the same ``runoverlay`` arguments, the same ``cslol-dll.dll``
hook. Only the timing differs -- this pre-arms during champ select and lets
cslol's own poll catch the game, where Rose suspends the game at launch instead
(see the injector docstring for why suspending is not done here).

Validated on Windows 11 against a real install: discovery, mkoverlay, the
detached patcher, adoption across a restart, and shutdown. The one path still
unproven is the hook landing in a live game, which needs a game to land in.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
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

#: cslol's Win32 patcher injects into a process owned by the same user, which
#: needs no rights the user does not already have. Nothing here elevates, so
#: there is no helper, no prompt, and no "authorization cancelled" outcome.
INJECTION_NEEDS_ROOT = False

#: Pass to every subprocess the app runs. The packaged app is windowed (it has
#: no console of its own), so a child started without this makes Windows
#: allocate a fresh console window for it -- a black box that flashes up during
#: champ select (mkoverlay), a guide fetch (curl) or the first-run download.
#: On macOS the same constant is 0 and does nothing.
CREATE_NO_WINDOW = 0x08000000

# --- the two patchers ------------------------------------------------------
#
# Windows splits the work cslol's mod-tools does on macOS across two binaries,
# for a reason that decides the whole design: cslol's Windows injection DLL is
# a dead end (an expiry kill-switch, and Vanguard names it incompatible), while
# its *overlay builder* -- pure local file munging that never touches the game
# -- still works and is open. So the overlay is still built with cslol's
# `mod-tools mkoverlay`, and the injection is handed to LTK's patcher, the
# maintained cslol successor whose hook Vanguard accepts. See WINDOWS.md.
#
#   mod-tools.exe + cslol-dll.dll   -> mkoverlay only (build_overlay); no game
#   ltk_patcher_host.exe + dll      -> the injection (runoverlay); hooks the game

LTK_HOST = "ltk_patcher_host.exe"
LTK_DLL = "ltk_patcher_dll.dll"

#: LTK hook flag OPT_OUT_AH_V1. LTK verifies its overlay and, for a base-skin
#: swap, finds skin0 pointing at another skin's mesh/audio -- which is exactly
#: what tibbers does on purpose. This downgrades that check from a fatal error
#: to a warning so the overlay is served. It is LTK's own quality gate, not the
#: game's anti-cheat (the DLL clears that separately, before this runs).
LTK_OPT_OUT_AH_V1 = 4

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
#
# The command line is the expensive field on Windows, and by a margin that
# decides how the app feels. macOS gets every argv in one `ps -axo command=`
# dump; Windows has no such call, so psutil opens each process and reads its
# PEB across the process boundary. Measured on a 217-process desktop:
#
#     process_iter(["pid", "cmdline"])       2075 ms
#     process_iter(["pid", "name"])             1 ms
#
# Two seconds, on the path the picker polls. So the name -- which comes free
# with the process listing -- is used to throw away 98% of the machine first,
# and argv is read only for the handful of processes that could possibly be a
# patcher or a holder. Same answer, ~1700x cheaper.

#: The only executables whose command line is ever worth reading: cslol's
#: patcher, and whatever is holding its stdin (a Python from source, the app
#: itself when frozen). `sys.executable` covers the venv and the frozen build;
#: the literals cover a holder left behind by the *other* one.
_CMDLINE_CANDIDATES = frozenset({
    "mod-tools.exe",
    LTK_HOST,
    "python.exe", "pythonw.exe",
    "Tibbers.exe", "tibbers.exe",
    os.path.basename(sys.executable) if sys.executable else "python.exe",
})

_TABLE_CACHE: Tuple[float, List[Tuple[int, str]]] = (0.0, [])
_TABLE_TTL = 1.0
_TABLE_LOCK = threading.Lock()


def process_table(fresh: bool = False) -> List[Tuple[int, str]]:
    """``(pid, command line)`` for the processes that could be ours.

    Not every process on the machine, unlike the macOS namesake: see the note
    above for why reading argv is rationed here. Everything that consumes this
    is looking for `mod-tools` or a holder, so the difference is invisible to
    callers -- but do not reach for this to find something else.
    """
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
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info.get("name") not in _CMDLINE_CANDIDATES:
                continue
            pid = proc.info.get("pid")
            # Read argv only now, one process at a time -- this is the call
            # that costs, and it is why the name is checked first.
            cmd = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess,
                OSError):
            continue
        if pid is not None and cmd:
            rows.append((int(pid), " ".join(cmd)))
    return rows


def runoverlay_pids(overlay: Optional[Path] = None,
                    fresh: bool = False) -> List[int]:
    """Live LTK patcher-host processes, optionally only those for *overlay*.

    The host is driven over stdin, so its own command line is a bare
    ``ltk_patcher_host.exe`` with the overlay nowhere in it -- unlike cslol's
    ``runoverlay <overlay>``. Scoping to an overlay therefore goes through the
    *holder*, which does carry the overlay: a host counts for *overlay* when
    its parent is one of that overlay's holders. This is what keeps a dev
    instance's patcher from being mistaken for the real one (rule 2).
    """
    hosts = []
    for pid, command in process_table(fresh=fresh):
        if LTK_HOST not in command:
            continue
        # The holder's argv names the host too (it spawned it); that row is the
        # holder, not the patcher, and holder_pids owns it.
        if HOLDER_MARK in command:
            continue
        hosts.append(pid)

    if overlay is None:
        return hosts

    holders = set(holder_pids(overlay, fresh=fresh))
    if not holders:
        return []
    scoped = []
    for pid in hosts:
        try:
            if psutil.Process(pid).ppid() in holders:
                scoped.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return scoped


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
    """The single Windows mod-tools build.

    ``cslol-dll.dll`` must sit beside it: the exe imports it at load time, so
    without it Windows fails the process at the loader -- no output, no exit
    code worth reading, nothing in the patcher log. Checking here turns that
    into something the user can act on. ``fetch_modtools.ps1`` installs the
    pair together.
    """
    tools_dir = Path(tools_dir)
    exe = tools_dir / "mod-tools.exe"
    if not exe.exists():
        raise FileNotFoundError(f"mod-tools.exe is missing at {exe}")
    dll = tools_dir / "cslol-dll.dll"
    if not dll.exists():
        raise FileNotFoundError(
            f"cslol-dll.dll is missing at {dll}. mod-tools.exe imports it at "
            f"load time and cannot start without it -- re-run "
            f"scripts\\fetch_modtools.ps1 to install both.")
    return exe


def modtools_arch(modtools: Path) -> str:
    return "x86_64"


def select_patcher(tools_dir: Path) -> Path:
    """LTK's injection host, ``ltk_patcher_host.exe``.

    ``ltk_patcher_dll.dll`` -- the hook itself -- must sit beside it; the host
    loads it to inject. ``fetch_modtools.ps1`` installs the pair. This is the
    binary the game is hooked with, distinct from the ``mod-tools.exe`` that
    only builds the overlay.
    """
    tools_dir = Path(tools_dir)
    host = tools_dir / LTK_HOST
    if not host.exists():
        raise FileNotFoundError(
            f"{LTK_HOST} is missing at {host} -- re-run "
            f"scripts\\fetch_modtools.ps1 to install the LTK patcher.")
    dll = tools_dir / LTK_DLL
    if not dll.exists():
        raise FileNotFoundError(
            f"{LTK_DLL} is missing at {dll}. {LTK_HOST} loads it to inject and "
            f"cannot work without it -- re-run scripts\\fetch_modtools.ps1.")
    return host


# ---------------------------------------------------------------------------
# Injection (no elevation on Windows)
# ---------------------------------------------------------------------------

def _overlay_prefix(overlay: Path) -> str:
    """LTK's ``config prefix`` wants the overlay root with a trailing separator."""
    prefix = str(overlay)
    return prefix if prefix.endswith(("\\", "/")) else prefix + "\\"


def patcher_protocol(overlay: Path) -> List[str]:
    """The lines fed to the LTK host over stdin to arm it for *overlay*.

    Configure logging, set OPT_OUT_AH_V1 so the base-skin overlay is served,
    point it at the overlay, then start scanning for the game. The host derives
    the game itself, so there is no game path here. It stops when its stdin
    reaches EOF, which is what the holder's open pipe prevents.
    """
    return [
        "config loglevel 1",
        f"config flags {LTK_OPT_OUT_AH_V1}",
        f"config prefix {_overlay_prefix(overlay)}",
        "start scan",
    ]


def runoverlay_command(patcher: Path, overlay: Path, config: Path,
                       game_dir: Path) -> List[str]:
    """The human-runnable equivalent of what the app drives over the protocol.

    LTK's ``runoverlay`` compat subcommand takes an overlay and scans for the
    game exactly as the app does -- but it cannot set OPT_OUT_AH_V1, so it is
    only a reference for a person reading the log, not what the app runs. The
    app spawns the bare host and drives `patcher_protocol` over stdin instead.
    """
    return [str(patcher), "runoverlay", str(overlay), f"--game:{game_dir}"]


def manual_command(modtools: Path, overlay: Path, config: Path,
                   game_dir: Path) -> str:
    """The equivalent command a user could run by hand -- no elevation."""
    try:
        patcher = select_patcher(Path(modtools).parent)
    except FileNotFoundError:
        patcher = Path(modtools).parent / LTK_HOST
    return subprocess.list2cmdline(
        runoverlay_command(patcher, overlay, config, game_dir))


# The LTK host stops the instant its stdin reaches EOF, so a detached holder
# keeps the pipe open and outlives this app. The holder writes the arming
# protocol into that pipe first, then holds it. It carries HOLDER_MARK, the
# overlay and the log in its own argv so it can be found and adopted later --
# the overlay lives here rather than in the host's bare argv.
#
# argv layout, both forms (after the interpreter/flag):
#   HOLDER_MARK  overlay  log_path  host_exe  <protocol line> <protocol line>...
_HOLDER_SCRIPT = (
    "import subprocess,sys,time\n"
    "log=open(sys.argv[3],'ab')\n"
    "p=subprocess.Popen([sys.argv[4]],stdin=subprocess.PIPE,"
    "stdout=log,stderr=log)\n"
    "for line in sys.argv[5:]:\n"
    "    p.stdin.write((line+'\\n').encode());p.stdin.flush()\n"
    "try:\n"
    "    time.sleep(10**9)\n"
    "finally:\n"
    "    p.stdin.close();p.terminate()\n"
)

#: The frozen build has no interpreter to hand `-c` to -- `sys.executable` is
#: Tibbers.exe, which would relaunch the whole app instead of holding a pipe.
#: So the packaged app re-runs *itself* with this flag and becomes the holder;
#: `hold_patcher` below is the body, and main.py dispatches to it before it
#: does anything else. From source the `-c` form is used unchanged.
HOLDER_FLAG = "--hold-patcher"


def holder_argv(overlay: Path, log_path: Path, host: Path,
                protocol: List[str]) -> List[str]:
    """The command line for a detached holder, frozen or from source.

    Either form carries HOLDER_MARK, the overlay, the log, the host binary and
    the protocol lines in its own argv -- which is what ``holder_pids`` matches
    on, so discovery and adoption do not care which one started it.
    """
    tail = [HOLDER_MARK, str(overlay), str(log_path), str(host), *protocol]
    if getattr(sys, "frozen", False):
        return [sys.executable, HOLDER_FLAG, *tail]
    return [sys.executable, "-c", _HOLDER_SCRIPT, *tail]


def hold_patcher(argv: List[str]) -> int:
    """Be the holder: start the LTK host, arm it, then hold its stdin open.

    *argv* is everything after ``HOLDER_FLAG``:
    ``[HOLDER_MARK, overlay, log_path, host_exe, *protocol]`` -- the same tail
    the ``-c`` script reads from its own ``sys.argv`` (one slot later, since
    that argv still carries ``-c``), so the two forms stay in step.
    """
    if len(argv) < 4:
        return 2
    log_path, host, protocol = argv[2], argv[3], argv[4:]
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen([host], stdin=subprocess.PIPE,
                                stdout=log_file, stderr=log_file)
        try:
            for line in protocol:
                proc.stdin.write((line + "\n").encode())
                proc.stdin.flush()
            while True:
                time.sleep(3600)
        except BaseException:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            proc.terminate()
            raise
    return 0  # pragma: no cover -- the loop above does not fall through

# Windows process-creation flags for a fully detached, windowless holder.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def spawn_runoverlay_detached(modtools: Path, overlay: Path, config: Path,
                              game_dir: Path, log_path: Path,
                              detached: bool = True):
    """Start the LTK patcher as the user, detached, with its output captured.

    A holder process owns the host's stdin and sleeps forever, so the patcher
    keeps scanning for the game across a restart of this app. *modtools* is the
    cslol mkoverlay binary the shared injector hands in; the LTK host is
    resolved from beside it (``fetch_modtools`` installs both into ``tools``).
    """
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    host = select_patcher(Path(modtools).parent)
    protocol = patcher_protocol(Path(overlay))
    argv = holder_argv(Path(overlay), Path(log_path), host, protocol)

    flags = _CREATE_NO_WINDOW
    if detached:
        flags |= _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP

    try:
        subprocess.Popen(
            argv,
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
    """Reap the stdin holders for *overlay*; killing one drops its patcher-host
    child with it (the holder closes stdin and terminates the child on the way
    out, and a closed pipe would end it anyway)."""
    return _terminate(holder_pids(overlay))


# ---------------------------------------------------------------------------
# Reading the patcher log
# ---------------------------------------------------------------------------

def parse_patcher_log(text: str) -> dict:
    """Distil LTK's host output into the fields the app watches.

    LTK's line protocol prints ``status <t> <state> <msg>`` and per-level DLL
    records. The states of interest: ``scanning for game`` (armed and waiting),
    ``game found`` (the host caught the game), then the DLL's own
    ``overlay verified`` / ``redirected wad`` (the skin is being served). Only
    ``ERROR`` lines are failures; ``WARN`` -- including the ``(opted out)``
    base-skin note that OPT_OUT_AH_V1 produces -- is expected and ignored.
    """
    error = None
    for line in text.splitlines():
        if re.search(r"\bERROR\b", line) or line.startswith("error "):
            if "opted out" not in line:
                error = line.strip()
    return {
        "watching": "scanning for game" in text or "injecting" in text,
        "found": "game found" in text,
        "patched": "overlay verified" in text or "redirected wad" in text,
        "exited": "dll detached" in text or " exited " in text,
        "error": error,
        "tail": "\n".join(text.splitlines()[-6:]),
    }


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
