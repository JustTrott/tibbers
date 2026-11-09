#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
What a bug report needs to say about the machine, in one place.

A launch writes it to the log as a header, so every session in tibbers.log
opens with the facts that used to take a round of questions each: which
tibbers, where League was found and how, which backend and which patcher
files, and whether anything is elevated. The same lines go into the summary
of a diagnostics archive, taken at the moment it is made.

Every probe answers "unknown" rather than raising: this runs on the way up,
and a machine that cannot answer one question must still start.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

log = logging.getLogger("tibbers.diagnostics")

IS_WINDOWS = sys.platform.startswith("win")

#: Riot's two folder names, which is what a RUNASADMIN layer is matched by --
#: the layer is keyed by the exe's full path.
RIOT_FOLDERS = ("league of legends", "riot client", "riot games")


def _probe(fn: Callable[[], str]) -> str:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc})"


# -- small facts --------------------------------------------------------------

def build_kind() -> str:
    """How this copy runs: installed, portable, an unpacked build or source."""
    if os.environ.get("TIBBERS_PORTABLE"):
        return "portable"
    if not getattr(sys, "frozen", False):
        commit = _git_commit()
        return f"source checkout {commit}" if commit else "source checkout"
    from . import update
    return "installed" if update.installed_app() is not None else "unpacked build"


def _git_commit() -> Optional[str]:
    """The checkout's commit, with a + when it has local changes."""
    import subprocess
    root = Path(__file__).resolve().parents[1]
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    head = run("rev-parse", "--short", "HEAD")
    if head.returncode:
        return None
    dirty = run("status", "--porcelain", "--untracked-files=no").stdout.strip()
    return head.stdout.strip() + ("+" if dirty else "")


def utc_offset() -> str:
    """The local clock's offset, so log times line up with LTK's UTC ones."""
    raw = time.strftime("%z") or "+0000"
    return f"UTC{raw[:3]}:{raw[3:]}"


def short_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


# -- Windows probes ------------------------------------------------------------

def file_version(path: Path) -> Optional[str]:
    """The exe's own version resource, e.g. ``1.33.0``; None without one."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    version = ctypes.WinDLL("version")
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    info = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(info), ctypes.byref(length)):
        return None
    # VS_FIXEDFILEINFO: signature, struct version, then the file version as
    # two DWORDs, most significant first.
    words = ctypes.cast(info, ctypes.POINTER(wintypes.DWORD * 4)).contents
    ms, ls = words[2], words[3]
    parts = [ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF]
    while len(parts) > 3 and parts[-1] == 0:
        parts.pop()
    return ".".join(str(p) for p in parts)


#: WinVerifyTrust's answers worth naming; anything else is printed as a code.
_TRUST_ERRORS = {
    0x800B0100: "unsigned",
    0x80096010: "signature does not match the file",
    0x800B0101: "certificate expired",
    0x800B0109: "untrusted root",
    0x800B0111: "explicitly distrusted",
}


def signature(path: Path) -> str:
    """Whether Windows accepts *path*'s Authenticode signature.

    The one fact that decided whether a hook installed at all: an unsigned
    or altered patcher DLL is refused before it reaches the game.
    """
    if not IS_WINDOWS:
        return "n/a"
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.c_void_p)]

    class TRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(FILE_INFO)), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    # WINTRUST_ACTION_GENERIC_VERIFY_V2
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                  (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = FILE_INFO(ctypes.sizeof(FILE_INFO), str(path), None, None)
    data = TRUST_DATA()
    data.cbStruct = ctypes.sizeof(TRUST_DATA)
    data.dwUIChoice = 2            # WTD_UI_NONE
    data.fdwRevocationChecks = 0   # WTD_REVOKE_NONE: no network on the way up
    data.dwUnionChoice = 1         # WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = 1         # WTD_STATEACTION_VERIFY
    data.dwProvFlags = 0x1000      # WTD_CACHE_ONLY_URL_RETRIEVAL

    wintrust = ctypes.WinDLL("wintrust")
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    result = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data)) & 0xFFFFFFFF
    data.dwStateAction = 2         # WTD_STATEACTION_CLOSE
    wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    if result == 0:
        return "signed"
    return _TRUST_ERRORS.get(result, f"not trusted (0x{result:08X})")


def elevated() -> bool:
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def uac_enabled() -> Optional[bool]:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as key:
        value, _ = winreg.QueryValueEx(key, "EnableLUA")
    return bool(value)


def runasadmin_layers() -> List[str]:
    """Riot exes Windows is told to run as administrator, in either hive.

    LTK refuses to patch while any of these is set (PATCHER LEAGUE_ELEVATED),
    whether or not League happens to be running elevated right now.
    """
    import winreg
    found = []
    for hive, label in ((winreg.HKEY_CURRENT_USER, "HKCU"),
                        (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows NT\CurrentVersion"
                                       r"\AppCompatFlags\Layers")
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if ("RUNASADMIN" in str(value).upper()
                        and any(f in name.lower() for f in RIOT_FOLDERS)):
                    found.append(f"{label} {name}")
    return found


# -- the header ----------------------------------------------------------------

def _file_line(label: str, path: Optional[Path], extra: str = "") -> str:
    if path is None or not Path(path).is_file():
        return f"{label} missing" + (f" ({path})" if path else "")
    parts = [short_hash(path), signature(path)]
    if extra:
        parts.append(extra)
    return f"{label} {', '.join(parts)}"


def patcher_lines(manager_backend: bool, tools_dir: Optional[Path]) -> List[str]:
    """The patcher pair the next game will be patched with."""
    if not IS_WINDOWS:
        return [f"Patcher:  mod-tools in {tools_dir}"]
    from . import wintools
    if manager_backend:
        from . import ltk_injector
        manager = ltk_injector.manager_path()
        lines = [f"Backend:  LTK Manager {_probe(lambda: file_version(manager) or '?')} "
                 f"({manager.parent})"]
        host = manager.parent / wintools.LTK_HOST
        staged = wintools.staged_ltk_dll()
        if staged is None:
            dll_line = "dll not downloaded yet (LTK Manager has not been opened)"
        else:
            directory, release = staged
            dll_line = _file_line("dll", directory / wintools.LTK_DLL,
                                  str(release.get("version") or ""))
        lines.append(f"Patcher:  {_file_line('host', host)}; {dll_line}")
        return lines
    record = _probe(lambda: str(wintools._read_record(tools_dir).get("ltk_release") or ""))
    return ["Backend:  direct (tibbers runs LTK's patcher itself)",
            f"Patcher:  {_file_line('host', tools_dir / wintools.LTK_HOST)}; "
            f"{_file_line('dll', tools_dir / wintools.LTK_DLL, record)}"]


def rights_line() -> str:
    if not IS_WINDOWS:
        return "Rights:   n/a"
    parts = [_probe(lambda: "tibbers elevated" if elevated() else "tibbers not elevated"),
             _probe(lambda: {True: "UAC on", False: "UAC OFF", None: "UAC ?"}[uac_enabled()])]
    layers = _probe(lambda: ", ".join(runasadmin_layers()) or "none")
    parts.append(f"RUNASADMIN on Riot: {layers}")
    return "Rights:   " + ", ".join(parts)


def session_lines(*, client_dir: Optional[Path], data_dir: Path,
                  tools_dir: Optional[Path], manager_backend: bool,
                  install_source: Optional[str] = None) -> List[str]:
    """The header: one fact per line, each prefixed so it greps by name."""
    from . import __version__
    lines = [f"===== tibbers {__version__} ({_probe(build_kind)}), "
             f"{time.strftime('%Y-%m-%d %H:%M')} {utc_offset()}, "
             f"{platform.platform()}",
             f"League:   {client_dir} (found by {install_source or 'unknown'})"]
    lines += _probe_lines(lambda: patcher_lines(manager_backend, tools_dir), "Patcher:  ")
    lines.append(rights_line())
    lines.append(f"Data:     {data_dir}")
    return lines


def _probe_lines(fn: Callable[[], List[str]], label: str) -> List[str]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return [f"{label}unknown ({exc})"]


def log_session(lines: Iterable[str]) -> None:
    for line in lines:
        log.info("%s", line)


# -- the archive ---------------------------------------------------------------
#
# What used to be "send me these four logs from these five folders": one zip,
# made by a button, landing on the desktop. Files are taken whole up to a cap
# -- the end of a log is the part a report is about -- and scrubbed of the
# user's name and of the bridge's and the client's secrets on the way in,
# because it is going to be posted somewhere people can read it.

#: The most of any one file the archive takes; past it, the end is kept.
FILE_CAP = 2_000_000

#: The bridge's own logs: C:\Users\Public when its launcher could write
#: there, else the temp folder (native/ltf/LeagueToolFlop-inj.cpp).
BRIDGE_LOGS = ("ltk_launcher.log", "ltk_bridge.log", "ltk_api.log")


def report_files(data_dir: Path, tools_dir: Optional[Path]) -> List[Tuple[str, Path]]:
    """Every file a report may need, as (name in the archive, path)."""
    files = [(f"tibbers/{name}", data_dir / name)
             for name in ("tibbers.log", "tibbers.log.1", "tibbers.log.2", "tibbers.log.3")]
    files.append(("tibbers/runoverlay.log", data_dir / "work" / "runoverlay.log"))
    files.append(("tibbers/preferences.json", data_dir / "preferences.json"))
    if tools_dir is not None:
        files.append(("tibbers/installed.json", Path(tools_dir) / "installed.json"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        logs = Path(appdata) / "dev.leaguetoolkit.manager" / "logs"
        try:
            newest = sorted(logs.glob("ltk-manager.*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)[:2]
        except OSError:
            newest = []
        files += [(f"ltk-manager/{p.name}", p) for p in newest]
    for env, label in (("PUBLIC", "bridge-public"), ("TEMP", "bridge-temp")):
        base = os.environ.get(env)
        if base:
            files += [(f"{label}/{name}", Path(base) / name) for name in BRIDGE_LOGS]
    return files


def redactor(user: Optional[str] = None, secrets: Iterable[str] = ()) -> Callable[[str], str]:
    """A function that takes the user's name and the known secrets out of text.

    The name is replaced where it is a profile folder -- in plain paths and in
    JSON-escaped ones -- which is where logs carry it. The secrets are the
    bridge token and anything shaped like the client's credentials.
    """
    rules: List[Tuple[re.Pattern, str]] = []
    if user:
        rules.append((re.compile(r"(?i)(Users(?:\\\\|\\|/))" + re.escape(user)
                                 + r"(?=\\|/|\"|'|\s|$)"), r"\1<user>"))
    for secret in secrets:
        if secret and len(secret) >= 8:
            rules.append((re.compile(re.escape(secret)), "<token>"))
    rules += [
        (re.compile(r"(?i)(riot:)[A-Za-z0-9_\-]{8,}"), r"\1<password>"),
        (re.compile(r"(?i)(Basic\s+)[A-Za-z0-9+/=]{12,}"), r"\1<auth>"),
        (re.compile(r"(?i)(\btoken[\"'\s:=]+)[A-Za-z0-9_\-]{16,}"), r"\1<token>"),
    ]

    def scrub(text: str) -> str:
        for pattern, replacement in rules:
            text = pattern.sub(replacement, text)
        return text
    return scrub


def _read_capped(path: Path) -> str:
    with open(path, "rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - FILE_CAP))
        data = stream.read()
    text = data.decode("utf-8", errors="replace")
    if size > FILE_CAP:
        text = f"[first {size - FILE_CAP} bytes left out]\n" + text
    return text


def desktop_dir() -> Path:
    """The user's desktop, wherever it was moved to (OneDrive, often)."""
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                                r"\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            path = Path(os.path.expandvars(value))
            if path.is_dir():
                return path
        except OSError:
            pass
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def collect(summary: List[str], data_dir: Path, tools_dir: Optional[Path],
            dest_dir: Optional[Path] = None, secrets: Iterable[str] = ()) -> Path:
    """Write the archive and return its path.

    *summary* is the header taken now plus whatever the app knows about the
    moment; the list of files found and not found is added to it, so a reply
    can tell "no LTK log" from "forgot to attach it".
    """
    dest_dir = Path(dest_dir) if dest_dir is not None else desktop_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"tibbers-diagnostics-{time.strftime('%Y-%m-%d-%H%M%S')}.zip"
    scrub = redactor(os.environ.get("USERNAME") or os.environ.get("USER"), secrets)

    included, missing = [], []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, source in report_files(data_dir, tools_dir):
            try:
                text = _read_capped(source)
            except FileNotFoundError:
                missing.append(name)
                continue
            except OSError as exc:
                missing.append(f"{name} ({exc})")
                continue
            archive.writestr(name, scrub(text))
            included.append(name)
        lines = list(summary) + ["", "Included: " + (", ".join(included) or "nothing"),
                                 "Not found: " + (", ".join(missing) or "nothing"),
                                 "The user's name in paths is <user>; tokens and "
                                 "passwords are <token>, <password> and <auth>."]
        archive.writestr("summary.txt", scrub("\n".join(lines)) + "\n")
    return path


def reveal(path: Path) -> None:
    """Show *path* selected in its folder, ready to drag into a chat."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if IS_WINDOWS:
        subprocess.Popen(["explorer", f"/select,{path}"], creationflags=flags)
    else:
        subprocess.Popen(["open", "-R", str(path)], creationflags=flags)
