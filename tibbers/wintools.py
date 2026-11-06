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

import base64
import hashlib
import json
import logging
import lzma
import os
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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
LTK_HOST, LTK_DLL = LTK_FILES

#: LTK Manager's Tauri identifier, which names its app data directory under
#: %APPDATA%. Its settings.json there may move the data directory elsewhere.
LTK_IDENTIFIER = "dev.leaguetoolkit.manager"

#: Since LTK Manager's ADR-0074 (2026-10-31) its setup carries the host and no
#: DLL. The manager downloads the DLL from its own delivery service and stages
#: it under `<data dir>/patcher/<sha256>/`, next to `delivery.json`, which
#: caches the signed manifest of each channel. A build without LTK's
#: attestation is meant to read exactly these staged DLLs, so that is what
#: tibbers does: it never talks to the delivery service itself.
LTK_PATCHER_DIR = "patcher"
LTK_DELIVERY_STATE = "delivery.json"
LTK_CHANNELS = ("stable", "beta")

#: A local override, in the data directory, for where the LTK pair comes from:
#: ``{"host": "<ltk_patcher_host.exe>", "dll": "<ltk_patcher_dll.dll>"}``.
#: With it, tibbers copies exactly those two files -- a fork's own host, a DLL
#: built or patched by hand -- and never reads LTK Manager's staged copy or its
#: releases. Whenever either source file changes, the pair is copied again.
#: Relative paths are taken from the directory the file sits in.
LTK_SOURCE = "ltk_source.json"


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


#: What was fetched, and which release it came from. Written beside the
#: binaries themselves so a copied or restored tools directory carries its own
#: provenance rather than trusting a preference elsewhere.
RECORD = "installed.json"

#: LTK's patcher DLL carries an expiry. Past it the DLL still attaches to the
#: game and then refuses to do anything, logging this and redirecting no wad
#: at all -- the game opens, the skin simply is not there. cslol's Windows DLL
#: has the same kind of kill-switch; it is why that one is not used to inject.
#: A tools directory that has served an expired patcher is stale whatever its
#: recorded tag says, so the line is worth reading directly.
END_OF_LIFE = "end of life reached"


def _read_record(where: Path) -> dict:
    try:
        data = json.loads((Path(where) / RECORD).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(where: Path, **fields) -> None:
    """Note which release a pair came from, merging into what is already there.

    Best effort: the binaries are what matter, and a tools directory whose
    record failed to write is merely treated as stale next time, which costs a
    re-fetch rather than a broken install.
    """
    data = _read_record(where)
    data.update(fields)
    data["at"] = time.time()
    try:
        (Path(where) / RECORD).write_text(json.dumps(data))
    except OSError as exc:  # noqa: BLE001
        log.debug("could not write %s: %s", RECORD, exc)


def expired(log_path: Path) -> bool:
    """True when the patcher's own log says its DLL is past its expiry.

    Read rather than inferred: the tag comparison needs the network, and this
    does not -- so a machine that cannot reach GitHub still knows the patcher
    it has is finished.
    """
    try:
        text = Path(log_path).read_text(errors="replace")
    except OSError:
        return False
    return END_OF_LIFE in text


def ltk_outdated(where: Optional[Path] = None) -> bool:
    """True when LTK Manager has staged a different DLL than the one we copied.

    The DLL no longer ships in LTK's releases, so a release tag says nothing
    about it; what LTK Manager last downloaded is the latest patcher this
    machine can have. An install whose record names no DLL digest -- every
    install from before the DLL moved -- counts as outdated whenever LTK has
    staged one. With nothing staged there is nothing better to switch to, so
    the pair in place stays (and its expiry still shows up in its log).
    """
    where = Path(where) if where is not None else tools_dir()
    if not all((where / name).exists() for name in LTK_FILES):
        return True
    source = ltk_source()
    if source is not None:
        # A missing source is a fork mid-rebuild: keep the pair in place.
        if not all(src.exists() for src in source):
            log.warning("%s names a file that does not exist; keeping the "
                        "patcher in place", LTK_SOURCE)
            return False
        return any(_sha256(src) != _sha256(where / name)
                   for src, name in zip(source, LTK_FILES))
    staged = staged_ltk_dll()
    if staged is None:
        return False
    return staged[1].get("digest") != _read_record(where).get("ltk_dll")


def _report(progress: Optional[Progress], message: str,
            percent: Optional[int] = None) -> None:
    if progress:
        progress(message, percent)
    else:
        log.info(message)


def _latest_release(api_url: str) -> dict:
    req = urllib.request.Request(api_url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "tibbers-wintools",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _pick_asset(release: dict, match: Callable[[dict], bool],
                api_url: str) -> dict:
    for asset in release.get("assets") or []:
        if match(asset):
            return asset
    raise RuntimeError(f"no matching asset in {api_url}")


def _latest_asset(api_url: str, match: Callable[[dict], bool]) -> dict:
    return _pick_asset(_latest_release(api_url), match, api_url)


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


#: NSIS's own opcode for "write this file out of the payload" (EW_EXTRACTFILE
#: in its fileform.h). Its second argument names the file, its third is where
#: the file's bytes start in the payload.
_NSIS_EXTRACTFILE = 20
_NSIS_MAGIC = b"\xef\xbe\xad\xdeNullsoftInst"


class _Stream:
    """Forward-only reads from a raw LZMA stream, discarding what is skipped."""

    def __init__(self, data: bytes, props: bytes):
        lc, rest = props[0] % 9, props[0] // 9
        self._dec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{
            "id": lzma.FILTER_LZMA1,
            "dict_size": struct.unpack("<I", props[1:5])[0],
            "lc": lc, "lp": rest % 5, "pb": rest // 5,
        }])
        self._data = data
        self._buf = b""
        self.pos = 0

    def read(self, n: int) -> bytes:
        while len(self._buf) < n and not self._dec.eof:
            more = self._dec.decompress(self._data, max_length=1 << 22)
            self._data = b""
            if not more and self._dec.needs_input:
                break
            self._buf += more
        if len(self._buf) < n:
            raise RuntimeError("the installer's payload ends early")
        out, self._buf = self._buf[:n], self._buf[n:]
        self.pos += n
        return out

    def skip_to(self, pos: int) -> None:
        while self.pos < pos:
            self.read(min(pos - self.pos, 1 << 22))


def unpack_nsis(exe: Path, names, into: Path) -> None:
    """Write the named files out of an NSIS installer without running it.

    LTK Manager ships only as an NSIS setup since v1.20.0 (its MSI is gone),
    and running a setup -- even silently -- is an install: registry entries,
    an uninstaller, shortcuts. This reads the payload instead, the way 7-Zip
    does, with nothing but the standard library: the installer Tauri builds
    is one solid LZMA stream holding NSIS's script header and then each file
    as a length and its bytes. The header's extract-file instructions say
    which name sits at which offset.
    """
    data = Path(exe).read_bytes()
    at = data.find(_NSIS_MAGIC)
    if at < 4:
        raise RuntimeError(f"{Path(exe).name} is not an NSIS installer")
    start = at - 4 + 28                     # past NSIS's 28-byte first header
    header_len, length = struct.unpack("<II", data[at + 16:at + 24])
    props = data[start:start + 5]
    if props[0] >= 225 or props[1:5] == b"\0\0\0\0":
        raise RuntimeError("the installer is not solid-LZMA compressed; "
                           "this NSIS layout is not supported")
    stream = _Stream(data[start + 5:at - 4 + length], props)
    size = struct.unpack("<I", stream.read(4))[0]
    if size != header_len:
        raise RuntimeError("the installer's header does not decode")
    header = stream.read(size)

    # Eight (offset, count) pairs after a flags word: pages, sections,
    # entries, strings, ... Entries are seven words, an opcode and six args;
    # strings are UTF-16, addressed in characters.
    blocks = [struct.unpack("<II", header[4 + 8 * k:12 + 8 * k])
              for k in range(8)]
    (entries, count), (strings, _), (lang, _) = blocks[2], blocks[3], blocks[4]
    table = header[strings:lang]

    def string(index: int) -> str:
        end = index * 2
        while table[end:end + 2] not in (b"\0\0", b""):
            end += 2
        return table[index * 2:end].decode("utf-16-le", "replace")

    want = {}
    for k in range(count):
        op, _, name, offset = struct.unpack(
            "<IIII", header[entries + 28 * k:entries + 28 * k + 16])
        if op != _NSIS_EXTRACTFILE:
            continue
        base = string(name).rsplit("\\", 1)[-1]
        if base in names:
            want.setdefault(base, offset)
    lost = [n for n in names if n not in want]
    if lost:
        raise RuntimeError(f"{', '.join(lost)} not found in {Path(exe).name}")

    # Each file's offset counts from the end of the header block.
    for name, offset in sorted(want.items(), key=lambda kv: kv[1]):
        stream.skip_to(4 + size + offset)
        blob = stream.read(struct.unpack("<I", stream.read(4))[0])
        (Path(into) / name).write_bytes(blob)


class LtkDllMissing(RuntimeError):
    """LTK Manager has not staged a patcher DLL on this machine."""

    def __init__(self) -> None:
        super().__init__(
            "LTK Manager has not downloaded its patcher yet. Install LTK "
            "Manager (github.com/LeagueToolkit/ltk-manager), open it once and "
            "let it finish, then try again -- tibbers uses the patcher it "
            "downloads.")


def ltk_data_dirs() -> List[Path]:
    """Where LTK Manager may keep its data, the configured folder first."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    default = Path(appdata) / LTK_IDENTIFIER
    dirs = [default]
    try:
        settings = json.loads((default / "settings.json").read_text("utf-8"))
        moved = settings.get("modStoragePath") if isinstance(settings, dict) else None
        if moved:
            dirs.insert(0, Path(moved))
    except (OSError, ValueError):
        pass
    return dirs


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cached_releases(root: Path) -> List[dict]:
    """The releases LTK's cached manifests offer, with the revoked ones dropped.

    The manifests are minisign-signed and LTK verified them before caching;
    tibbers has no Ed25519 verifier in its bundle, so it trusts the cache only
    as far as the DLL's own SHA-256 matching the digest the manifest names.
    """
    try:
        state = json.loads((root / LTK_DELIVERY_STATE).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    manifests = []
    for channel in LTK_CHANNELS:
        entry = (state.get("channels") or {}).get(channel) or {}
        try:
            body = base64.b64decode(entry["envelope"]["manifest"])
            manifest = json.loads(body)
        except (KeyError, TypeError, ValueError):
            continue
        if manifest.get("schema") == 1 and manifest.get("channel") == channel:
            manifests.append(manifest)
    revoked = {d for m in manifests for d in m.get("revoked") or []}
    return [m["release"] for m in manifests
            if isinstance(m.get("release"), dict)
            and m["release"].get("digest") not in revoked]


def staged_ltk_dll() -> Optional[Tuple[Path, dict]]:
    """The newest patcher DLL LTK Manager has staged, and its release.

    Returns the staging directory, which holds ``ltk_patcher_dll.dll`` and,
    once LTK has run a session from it, the matching host. Stable comes
    before beta; within those the release with the later end of life wins.
    """
    for data in ltk_data_dirs():
        root = data / LTK_PATCHER_DIR
        releases = sorted(_cached_releases(root),
                          key=lambda r: r.get("eol") or 0, reverse=True)
        for release in releases:
            digest = str(release.get("digest") or "")
            dll = root / digest / LTK_DLL
            try:
                if digest and _sha256(dll) == digest:
                    return dll.parent, release
            except OSError:
                continue
    return None


def ltk_source() -> Optional[Tuple[Path, Path]]:
    """The host and DLL named in ``ltk_source.json``, or None without one."""
    path = system.data_dir() / LTK_SOURCE
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not read {path}: {exc}") from exc
    if not (isinstance(data, dict) and data.get("host") and data.get("dll")):
        raise RuntimeError(f'{path} must name both "host" and "dll"')
    return (path.parent / Path(data["host"]).expanduser(),
            path.parent / Path(data["dll"]).expanduser())


def _fetch_ltk(into: Path, progress) -> None:
    source = ltk_source()
    if source is not None:
        _report(progress, "copying the patcher named in " + LTK_SOURCE)
        for src in source:
            if not src.exists():
                raise RuntimeError(f"{src}, named in {LTK_SOURCE}, does not "
                                   "exist")
        for src, name in zip(source, LTK_FILES):
            shutil.copy2(src, into / name)
        _record(into, ltk_source=[str(src) for src in source],
                ltk_dll=_sha256(into / LTK_DLL))
        return

    _report(progress, "looking for LTK Manager's patcher...")
    staged = staged_ltk_dll()
    if staged is None:
        raise LtkDllMissing()
    dll_dir, dll_release = staged

    # The host LTK staged beside the DLL is the one it runs that DLL with, so
    # the pair is matched by construction. Only before LTK's first session is
    # it absent; then the host comes out of LTK's latest setup, as before.
    if (dll_dir / LTK_HOST).exists():
        _install_pair(dll_dir, LTK_FILES, into)
        _record(into, ltk_dll=dll_release.get("digest"),
                ltk_release=dll_release.get("version"))
        return

    release = _latest_release(LTK_LATEST)
    asset = _pick_asset(
        release,
        lambda a: str(a.get("name", "")).lower().endswith("-setup.exe"),
        LTK_LATEST)
    tmp = Path(tempfile.mkdtemp(prefix="tibbers-ltk-"))
    try:
        setup = tmp / asset["name"]
        _download(asset["browser_download_url"], setup,
                  progress, "downloading injection patcher")
        extract = tmp / "x"
        extract.mkdir()
        _report(progress, "extracting injection patcher...")
        # Read out of the installer, never run: no install, no service, no
        # registry, no Vanguard interaction.
        unpack_nsis(setup, (LTK_HOST,), extract)
        shutil.copy2(dll_dir / LTK_DLL, extract / LTK_DLL)
        _install_pair(extract, LTK_FILES, into)
        _record(into, ltk=release.get("tag_name") or "",
                ltk_dll=dll_release.get("digest"),
                ltk_release=dll_release.get("version"))
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


#: Files earlier versions fetched into the tools directory and no longer use.
#: `curl-impersonate.exe` is 4 MB whose entire job was presenting Chrome's TLS
#: handshake to u.gg's build CDN. The build pages read op.gg now, so it is
#: never opened again -- and nothing else would ever remove it, since it sits
#: in the data directory rather than in the install an update replaces.
ORPHANS = ("curl-impersonate.exe",)


def remove_orphans(where: Optional[Path] = None) -> List[str]:
    """Delete tools no version still uses. Returns what was removed."""
    where = Path(where) if where is not None else tools_dir()
    gone = []
    for name in ORPHANS:
        path = where / name
        try:
            if path.exists():
                path.unlink()
                gone.append(name)
        except OSError as exc:  # noqa: BLE001
            # Somebody has it open, or it is read-only. It is 4 MB of nothing,
            # not a reason to fail a launch.
            log.debug("could not remove %s: %s", name, exc)
    return gone


def refresh_ltk(where: Optional[Path] = None,
                progress: Optional[Progress] = None) -> bool:
    """Replace the LTK patcher pair with the latest release.

    The caller must have stopped the patcher first. Both files are load-time
    images -- the host is a running exe while it watches, and the DLL is
    mapped into the game while a game is up -- and Windows refuses to
    overwrite either then, so a refresh attempted underneath a live game
    fails on the copy with the pair half replaced. `_install_pair` checks both
    sources before copying anything, but nothing can make the destination
    writable; that is the caller's job and why this says so rather than
    guessing at League's state itself.

    Returns True when the pair was replaced.
    """
    where = Path(where) if where is not None else tools_dir()
    where.mkdir(parents=True, exist_ok=True)
    try:
        _fetch_ltk(where, progress)
    except PermissionError as exc:
        raise RuntimeError(
            "the patcher is still running, so its files could not be "
            f"replaced ({exc})") from exc
    return True


def missing(where: Optional[Path] = None) -> List[str]:
    """Which of the four files are not yet present."""
    where = Path(where) if where is not None else tools_dir()
    return [n for n in (*CSLOL_FILES, *LTK_FILES) if not (where / n).exists()]
