#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS specifics: locating League, identifying its processes, and running the
one command that needs root.

Distilled from the Rose macOS port's platform_compat package, keeping only
what this tool actually uses. Nothing here modifies the League installation --
that is the whole point of this design, since Riot's client verifies its own
files and repairs anything that changed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

log = logging.getLogger("tibbers.system")

# Mach-O executable names. "LeagueofLegends" has no spaces and is exactly 15
# characters -- right at the macOS proc_name truncation limit, which is why
# processes are matched on their executable path rather than their name.
GAME_PROCESS = "LeagueofLegends"
CLIENT_PROCESS = "LeagueClient"
CLIENT_UX_PROCESS = "LeagueClientUx"

#: The fopen hook goes through `task_for_pid`, and the kernel hands a foreign
#: task port to root alone -- so every injection here is elevated, with or
#: without the passwordless helper. Windows sets this False.
INJECTION_NEEDS_ROOT = True

#: A no-op on macOS (see the Windows module, where it suppresses the console
#: window a child of the windowed app would otherwise pop up). Passed to
#: `subprocess` on both platforms so the call sites stay identical.
CREATE_NO_WINDOW = 0

#: No instance mutex on macOS: LaunchServices activates the running app when
#: it is opened again, so a second copy never starts.
INSTANCE_MUTEX = ""


def claim_instance() -> bool:
    return True

INSTALL_ROOTS = (
    Path("/Applications/League of Legends.app/Contents/LoL"),
    Path.home() / "Applications/League of Legends.app/Contents/LoL",
)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def is_client_dir(path: Path) -> bool:
    return (Path(path) / "LeagueClient.app").is_dir()


def is_game_dir(path: Path) -> bool:
    path = Path(path)
    return (path / "LeagueofLegends.app").is_dir() and (path / "DATA" / "FINAL").is_dir()


#: Where League was found, and when. An install does not move under a running
#: app, so a hit is kept for good; a miss is re-checked shortly, because
#: League being started while this app is up is the ordinary case.
_INSTALL_LOCK = threading.Lock()
_INSTALL: Optional[Tuple[Optional[Path], Optional[Path]]] = None
_INSTALL_AT = 0.0
_INSTALL_MISS_TTL = 5.0


def find_install() -> Tuple[Optional[Path], Optional[Path]]:
    """Return ``(game_dir, client_dir)``.

    Prefers a running client (authoritative for non-standard installs), then
    falls back to the standard locations.

    Cached, because the answer is asked for on the champ-select poll -- via
    `lockfile_path`, several times a second -- and finding it walks every
    process on the machine. Uncached that scan ran continuously for as long as
    League was closed, which is most of the time this app is open.
    """
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
    # One sweep for both executable names: process_iter is the expensive part
    # and it answered the same question twice.
    for proc in find_processes(CLIENT_PROCESS, CLIENT_UX_PROCESS):
        try:
            exe = proc.exe()
        except (psutil.Error, OSError):
            continue
        if not exe:
            continue
        for parent in Path(exe).parents:
            if parent.name.endswith(".app") and is_client_dir(parent.parent):
                client = parent.parent
                game = client / "Game"
                if is_game_dir(game):
                    return game, client

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
    """Processes whose executable basename is any of *names*.

    Several names in one call because the sweep, not the comparison, is what
    this costs: `process_iter` reads an executable path per process and that
    is denied for most of them.
    """
    wanted = set(names)
    found = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = proc.info.get("exe")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if exe and os.path.basename(exe) in wanted:
            found.append(proc)
    return found


def find_game() -> Optional[psutil.Process]:
    procs = find_processes(GAME_PROCESS)
    return procs[0] if procs else None


def game_pid() -> Optional[int]:
    proc = find_game()
    return proc.pid if proc is not None else None


# --- the patcher, which runs as root ---------------------------------------
#
# psutil cannot see it. `proc_pidpath` on a root-owned process is denied to an
# ordinary user, so process_iter reports no exe (and therefore no match on the
# executable name) and cmdline() raises AccessDenied. The patcher was
# consequently invisible to every psutil-based check in this file -- which is
# why `Injector.is_running()` only ever noticed patchers it had spawned itself.
#
# `ps -axo command=` reads KERN_PROCARGS2, which IS readable across users, so
# the command line is available even though the executable path is not. That is
# what makes it possible to find -- and adopt -- a patcher left behind by an
# earlier run of the app.

#: Written into the holder shell's own command line so it can be found again.
HOLDER_MARK = "tibbers-patcher-holder"


_TABLE_CACHE: Tuple[float, List[Tuple[int, str]]] = (0.0, [])
_TABLE_TTL = 1.0
_TABLE_LOCK = threading.Lock()


def process_table(fresh: bool = False) -> List[Tuple[int, str]]:
    """``(pid, command line)`` for every process, including other users'.

    Cached for a second. The champ-select watcher asks whether the patcher is
    alive on every change it reports, and forking `ps` several times a second
    for an answer that cannot meaningfully change that fast is waste.
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
    try:
        proc = subprocess.run(["/bin/ps", "-axo", "pid=,command="],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in proc.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and command.strip():
            rows.append((int(pid), command.strip()))
    return rows


def runoverlay_pids(overlay: Optional[Path] = None,
                    fresh: bool = False) -> List[int]:
    """Live ``mod-tools runoverlay`` processes, optionally only for *overlay*.

    Scoping by overlay matters: a dev instance with its own TIBBERS_HOME must
    never conclude that the real instance's patcher is its own, and must never
    stop it.
    """
    want = str(overlay) if overlay is not None else None
    found = []
    for pid, command in process_table(fresh=fresh):
        if "mod-tools" not in command or " runoverlay " not in f" {command} ":
            continue
        # The holder shell's own command line contains the whole pipeline,
        # patcher included, so it matches this scan as well and would be
        # counted as a second patcher.
        if HOLDER_MARK in command:
            continue
        if want is not None and want not in command:
            continue
        found.append(pid)
    return found


def holder_pids(overlay: Optional[Path] = None,
                fresh: bool = True) -> List[int]:
    """The detached shells holding the patcher's stdin open.

    runoverlay exits(0) the moment stdin reaches EOF, so something has to keep
    the write end open for as long as the patcher should live. That something
    used to be the app itself, which is exactly why restarting the app dropped
    the skin.
    """
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
# Architecture
# ---------------------------------------------------------------------------

_PROC_PIDTBSDINFO = 3
_PROC_FLAG_TRANSLATED = 0x400


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def is_translated(pid: int) -> Optional[bool]:
    """Whether *pid* runs under Rosetta 2 (i.e. executes x86_64 code)."""
    try:
        libproc = ctypes.CDLL(
            ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        )
        info = _ProcBsdInfo()
        written = libproc.proc_pidinfo(
            ctypes.c_int(pid), _PROC_PIDTBSDINFO, ctypes.c_uint64(0),
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if written != ctypes.sizeof(info):
            return None
        return bool(info.pbi_flags & _PROC_FLAG_TRANSLATED)
    except (OSError, AttributeError, ValueError):
        return None


def select_modtools(tools_dir: Path, pid: Optional[int] = None) -> Path:
    """Pick the mod-tools build matching the game process's architecture.

    cslol's patcher writes architecture-specific shellcode, so an arm64 build
    cannot patch a translated x86_64 game or vice versa. League ships universal
    binaries, so this has to be measured rather than assumed.
    """
    tools_dir = Path(tools_dir)
    native = tools_dir / "mod-tools"
    intel = tools_dir / "mod-tools-x86_64"

    if pid is None:
        proc = find_game()
        pid = proc.pid if proc is not None else None

    if pid is not None and is_translated(pid) is True:
        if intel.exists():
            return intel
        raise FileNotFoundError(
            f"game process {pid} runs under Rosetta (x86_64) but the Intel "
            f"mod-tools build is missing at {intel}"
        )
    return native


# ---------------------------------------------------------------------------
# Privileged execution
# ---------------------------------------------------------------------------
#
# `mod-tools runoverlay` hooks fopen in the live game via task_for_pid(), which
# the kernel grants only to root. Only that one command is elevated; the rest
# of this tool runs as the user.

def runoverlay_command(modtools: Path, overlay: Path, config: Path,
                       game_dir: Path) -> List[str]:
    return [
        str(modtools), "runoverlay", str(overlay), str(config),
        f"--game:{game_dir}", "--opts:configless",
    ]


def manual_command(modtools: Path, overlay: Path, config: Path,
                   game_dir: Path) -> str:
    """The equivalent command a user could run by hand.

    Logged before every elevation so a privileged action is never opaque.
    """
    return "sudo " + " ".join(
        shlex.quote(a)
        for a in runoverlay_command(modtools, overlay, config, game_dir)
    )


def _applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def modtools_arch(modtools: Path) -> str:
    """'x86_64' or 'arm64' for the selected build."""
    return "x86_64" if str(modtools).endswith("x86_64") else "arm64"


def spawn_runoverlay_detached(modtools: Path, overlay: Path, config: Path,
                              game_dir: Path, log_path: Path,
                              detached: bool = True):
    """Start runoverlay as root, detached, with its output captured.

    The patcher runs until stopped -- it has its own loop waiting for the game
    -- so `do shell script` cannot be allowed to block on it. Backgrounding
    inside the shell command lets osascript return as soon as authorization is
    granted, and redirecting to *log_path* keeps the patcher's status messages
    ("Found League", "Patching", ...) readable, which is the only way to tell
    whether it actually worked.

    *detached* decides whether the patcher outlives this process. It does not
    change one byte of what the patcher does to the game: same binary, same
    arguments, same hook. Only who holds its stdin open changes.
    """
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    # Passwordless path, when the user has installed the helper -- asked
    # about the build that is actually going to be run, so a helper without
    # the Intel copy falls back to prompting instead of exec'ing a wrapper
    # that will refuse. Nothing about the command changes either way.
    from . import privileged
    arch = modtools_arch(modtools)
    if privileged.available(arch):
        return privileged.start_runoverlay(
            arch, overlay, config, game_dir, log_path, detached=detached)

    inner = " ".join(
        shlex.quote(a)
        for a in runoverlay_command(modtools, overlay, config, game_dir)
    )
    # runoverlay exits as soon as stdin reaches EOF, so a plain `nohup ... &`
    # dies instantly and silently. A sleep on the other end of a pipe keeps
    # stdin open without ever sending the newline that means "stop".
    held = (f": {HOLDER_MARK} {shlex.quote(str(overlay))}; "
            f"/bin/sleep 2147483647 | {inner}")
    backgrounded = (
        f"nohup /bin/sh -c {shlex.quote(held)} "
        f"> {shlex.quote(str(log_path))} 2>&1 &"
    )

    proc = subprocess.run(
        [
            "osascript", "-e",
            f"do shell script {_applescript_string(backgrounded)} "
            f"with administrator privileges",
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "User canceled" in err or "-128" in err:
            raise PermissionError("authorization cancelled")
        raise RuntimeError(err or "osascript failed")
    return None


# ---------------------------------------------------------------------------
# Reading the patcher log
# ---------------------------------------------------------------------------

#: What cslol's `runoverlay` prints; see patcher.hpp STATUS_MSG.
PATCHER_READY = "Waiting for league match to start"
PATCHER_FOUND = "Found League"
PATCHER_PATCHING = "Patching"
PATCHER_EXITED = "League exited"


def parse_patcher_log(text: str) -> dict:
    """Distil cslol's `runoverlay` output into the fields the app watches."""
    error = None
    for line in text.splitlines():
        if re.search(r"error|failed|exception|throw", line, re.I):
            if "Waiting" not in line:
                error = line.strip()
    return {
        "watching": PATCHER_READY in text,
        "found": PATCHER_FOUND in text,
        "patched": PATCHER_PATCHING in text,
        "exited": PATCHER_EXITED in text,
        "error": error,
        "tail": "\n".join(text.splitlines()[-6:]),
    }


def kill_runoverlay() -> None:
    """Stop the root-owned patcher. Needs elevation to signal a root process."""
    from . import privileged
    # Stopping execs no mod-tools build -- the wrapper pkills by name -- so
    # this must not be refused over a build it will never reach for.
    if privileged.available(privileged.NO_BUILD):
        privileged.stop_runoverlay()
        return

    # The prompting path runs the holder as root too, so it has to go the same
    # way; the helper path's holder belongs to the user and is reaped in
    # kill_holders() without any elevation at all.
    subprocess.run(
        [
            "osascript", "-e",
            "do shell script \"/usr/bin/pkill -f 'mod-tools.*runoverlay'; "
            f"/usr/bin/pkill -f '{HOLDER_MARK}'; true\" "
            "with administrator privileges",
        ],
        capture_output=True, text=True, timeout=60,
    )


def kill_holders(overlay: Optional[Path] = None) -> int:
    """Reap the stdin holders for *overlay*. They run as the user, not root.

    Killing the process *group* rather than the shell: the shell's own child
    is the `sleep` that actually holds the pipe, and signalling only the shell
    would leave that sleep running forever. The holder is started with
    start_new_session(), so its group contains nothing else.
    """
    killed = 0
    mine = os.getpgrp()
    for pid in holder_pids(overlay):
        try:
            group = os.getpgid(pid)
        except OSError:
            continue
        if group == mine:
            # Never signal our own group: that would take the app with it.
            continue
        try:
            os.killpg(group, signal.SIGTERM)
            killed += 1
        except OSError as exc:  # noqa: PERF203
            log.debug("could not stop patcher holder %d: %s", pid, exc)
    return killed


#: One created directory per home, keyed by the override so a test that
#: changes TIBBERS_HOME still gets its own.
_DATA_DIRS: Dict[str, Path] = {}
_DATA_LOCK = threading.Lock()


def data_dir() -> Path:
    """Where the library, the built overlay and the preferences live.

    Overridable with TIBBERS_HOME so a second instance can be run without
    touching the real one. Every instance builds its overlay into the same
    place, so a test run started while a game is live would rebuild the
    directory the running patcher is serving from, under a different
    champion's mods.

    The directory is created once rather than on every call. Nearly every path
    in the app is derived from here -- the art proxy asks per image, the
    library asks per skin file -- so the `mkdir` was running thousands of times
    for a directory that exists after the first.
    """
    override = os.environ.get("TIBBERS_HOME") or ""
    with _DATA_LOCK:
        made = _DATA_DIRS.get(override)
    if made is not None:
        return made

    d = Path(override).expanduser() if override else (
        Path.home() / "Library" / "Application Support" / "tibbers")
    d.mkdir(parents=True, exist_ok=True)
    with _DATA_LOCK:
        _DATA_DIRS[override] = d
    return d
