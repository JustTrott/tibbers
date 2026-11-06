"""Fetch byte-identical skin mods from the catalogue used by Rose."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.request import Request, urlopen
import zipfile

from . import system

REPOSITORY = "Alban1911/LeagueSkins"
MANIFEST_URL = f"https://raw.githubusercontent.com/{REPOSITORY}/manifest/manifest.json"
MAX_MANIFEST = 16 * 1024 * 1024
MAX_MOD = 1024 * 1024 * 1024
log = logging.getLogger("tibbers.catalogue")
_manifest_lock = threading.Lock()
_download_locks = [threading.Lock() for _ in range(16)]
_manifests = {}
#: path -> (size, mtime_ns) of a file already checked against the catalogue.
#: Hovering a champion asks about all of its skins at once, and hashing a
#: few megabytes per skin each time would be felt on every hover.
_verified = {}


def enabled() -> bool:
    return os.environ.get("TIBBERS_LTK_BACKEND") == "manager"


def _parse(data: bytes) -> tuple[str, dict]:
    value = json.loads(data)
    commit = value.get("commit", "")
    if value.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("unsupported skin catalogue")
    files = {}
    for path, digest, size in value["files"]:
        if not path.startswith("skins/"):
            continue
        if not re.fullmatch(r"skins/\d+/\d+/(?:\d+/)?\d+\.fantome", path):
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", digest) or not isinstance(size, int):
            raise ValueError("invalid skin catalogue entry")
        if not 0 < size <= MAX_MOD:
            raise ValueError("skin archive exceeds the size limit")
        files[path] = (digest, size)
    if not files:
        raise ValueError("skin catalogue is empty")
    return commit, files


def _request(url: str):
    return urlopen(Request(url, headers={"User-Agent": "Tibbers-skin-catalogue"}), timeout=30)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".catalogue-", delete=False) as file:
        staging = Path(file.name)
        try:
            file.write(data)
        except BaseException:
            file.close()
            staging.unlink(missing_ok=True)
            raise
    try:
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def manifest() -> tuple[str, dict]:
    cache = system.data_dir() / "skins-leagueskins" / ".catalogue.json"
    with _manifest_lock:
        previous = _manifests.get(cache)
        if previous and time.monotonic() < previous[0]:
            return previous[1]
        try:
            with _request(MANIFEST_URL) as response:
                data = response.read(MAX_MANIFEST + 1)
            if len(data) > MAX_MANIFEST:
                raise ValueError("skin catalogue exceeds the size limit")
            result = _parse(data)
            _atomic_write(cache, data)
            ttl = 600
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Cannot refresh skin catalogue: %s", exc)
            # A pinned cached manifest also verifies already downloaded mods offline.
            result = previous[1] if previous else _parse(cache.read_bytes())
            ttl = 60
        _manifests[cache] = (time.monotonic() + ttl, result)
        return result


def _blob_hash(path: Path) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, size: int, digest: str) -> bool:
    """True when *path* is the catalogue's file, hashing it at most once."""
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        return False
    if _verified.get(key) == (stat.st_size, stat.st_mtime_ns):
        return True
    if stat.st_size != size or _blob_hash(path) != digest:
        _verified.pop(key, None)
        return False
    _verified[key] = (stat.st_size, stat.st_mtime_ns)
    return True


def fetch(champion_id: int, skin_id: int, chroma_id: int | None, dest: Path) -> None:
    """Atomically install the manifest's exact archive, including its metadata."""
    parts = ["skins", str(int(champion_id)), str(int(skin_id))]
    if chroma_id is not None:
        parts.append(str(int(chroma_id)))
    parts.append(f"{int(chroma_id if chroma_id is not None else skin_id)}.fantome")
    path = "/".join(parts)
    commit, files = manifest()
    if path not in files:
        raise ValueError(f"skin is absent from the catalogue: {path}")
    expected_hash, expected_size = files[path]
    lock = _download_locks[hash(str(dest.resolve())) % len(_download_locks)]
    with lock:
        if dest.is_file() and _matches(dest, expected_size, expected_hash):
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".download-", delete=False) as file:
            staging = Path(file.name)
            try:
                url = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{path}"
                with _request(url) as response:
                    received = 0
                    while chunk := response.read(1024 * 1024):
                        received += len(chunk)
                        if received > expected_size:
                            raise ValueError("skin download exceeds the catalogue size")
                        file.write(chunk)
                file.close()
                if received != expected_size or _blob_hash(staging) != expected_hash:
                    raise ValueError("skin download does not match the catalogue hash")
                with zipfile.ZipFile(staging) as archive:
                    names = archive.namelist()
                    if "META/info.json" not in names or not any(n.startswith("WAD/") for n in names):
                        raise ValueError("skin archive is missing mod data")
                    if archive.testzip() is not None:
                        raise ValueError("skin archive has a corrupt member")
                staging.replace(dest)
                stat = dest.stat()
                _verified[str(dest)] = (stat.st_size, stat.st_mtime_ns)
            finally:
                file.close()
                staging.unlink(missing_ok=True)
