#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Riot's WAD archive: read from the game install, written for a mod.

A `.wad.client` is a flat archive with no directory. Every entry is filed
under the xxh64 of its *lowercased* path, so nothing in the file says what any
of it is called -- you have to already know the path to ask for it. That is
what makes the whole of `skinsmith` possible without a hash list: the paths a
skin mod needs are `data/characters/<c>/skins/skin<n>.bin`, and those are
spelled out by the champion and the skin number.

Only what a skin mod needs is implemented. Reading covers versions 1 to 3
because old installs exist; writing only ever emits 3.3, which is what the
game reads today and what every community mod ships.

`xxhash` and `zstandard` are the two packages this needs, and both are
optional at import time on purpose: without them `available()` is False and
the app falls back to downloading mods, rather than failing to start.
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

try:
    import xxhash
except ImportError:  # pragma: no cover - exercised by not having the wheel
    xxhash = None

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None


class WadError(Exception):
    """A WAD was not shaped the way a WAD is shaped.

    Every structural surprise raises this rather than letting a `struct.error`
    or an IndexError out, so a caller can tell "this archive is not what I
    expected" from "the disk went away" and fall back accordingly.
    """


MAGIC = b"RW"

#: The v3 entry: hash, offset, compressed size, uncompressed size, a byte
#: holding the compression type in its low nibble and the subchunk count in
#: its high one, a duplicate flag, the first subchunk index, and an xxh3-64
#: checksum of the compressed bytes.
_ENTRY_V3 = struct.Struct("<QIIIBBHQ")
_ENTRY_V1 = struct.Struct("<QIIII")

#: Refuse an index that claims more entries than any real WAD holds. A
#: corrupt header would otherwise have us allocate against a 4-billion count.
MAX_ENTRIES = 1 << 20

#: Compression types. 2 is a redirection to a file outside the archive, which
#: has no content to return.
RAW, GZIP, REDIRECT, ZSTD, ZSTD_CHUNKED = 0, 1, 2, 3, 4


def available() -> bool:
    """True when the archive can actually be read and written."""
    return xxhash is not None and zstandard is not None


def xxh64_path(path) -> int:
    """The key a WAD files an entry under: xxh64 of the lowercased path."""
    if xxhash is None:
        raise WadError("xxhash is not installed")
    if isinstance(path, str):
        path = path.encode("utf-8")
    return xxhash.xxh64(path.lower()).intdigest()


def fnv1a32(text) -> int:
    """The 32-bit hash a `.bin` files an entry or a property name under.

    Also case-insensitive, and written out rather than taken from a library
    because it is eight lines and nothing else in the app hashes anything.
    """
    if isinstance(text, str):
        text = text.encode("utf-8")
    digest = 0x811C9DC5
    for byte in text.lower():
        digest = ((digest ^ byte) * 0x01000193) & 0xFFFFFFFF
    return digest


@dataclass(slots=True)
class Entry:
    hash: int
    offset: int
    csize: int
    usize: int
    type: int
    subchunks: int = 0
    duplicate: int = 0
    subchunk_index: int = 0
    checksum: int = 0


class Wad:
    """Read-only view of a `.wad.client`.

    The index is read up front -- it is 32 bytes an entry and answers every
    "is this path in here" without touching the body -- and entry payloads are
    read on demand, because a champion archive is a few hundred megabytes and
    a skin mod wants two entries out of six thousand.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.entries: Dict[int, Entry] = {}
        self.order: List[Entry] = []
        try:
            self._fh = self.path.open("rb")
        except OSError as exc:
            raise WadError(f"cannot open {self.path}: {exc}") from exc
        try:
            self._read_index()
        except WadError:
            self.close()
            raise
        except (struct.error, ValueError, OSError) as exc:
            self.close()
            raise WadError(f"{self.path.name}: unreadable index: {exc}") from exc

    # -- lifetime ----------------------------------------------------------

    def close(self) -> None:
        fh = getattr(self, "_fh", None)
        if fh is not None:
            fh.close()
            self._fh = None

    def __enter__(self) -> "Wad":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __contains__(self, path_hash: int) -> bool:
        return path_hash in self.entries

    # -- the index ---------------------------------------------------------

    def _read_index(self) -> None:
        head = self._fh.read(4)
        if head[:2] != MAGIC:
            raise WadError(f"{self.path.name}: not a WAD (magic {head[:2]!r})")
        self.major, self.minor = head[2], head[3]

        if self.major == 1:
            offset, size, count = struct.unpack("<HHI", self._read(8))
        elif self.major == 2:
            # A signature block whose length is in the first byte, then an
            # 83-byte remainder and an 8-byte checksum, none of which matters
            # for reading the index.
            self._read(1 + 83 + 8)
            offset, size, count = struct.unpack("<HHI", self._read(8))
        elif self.major == 3:
            self._read(256 + 8)     # signature, then the archive checksum
            count = struct.unpack("<I", self._read(4))[0]
            size, offset = _ENTRY_V3.size, self._fh.tell()
        else:
            raise WadError(f"{self.path.name}: unsupported version {self.major}")

        if count > MAX_ENTRIES:
            raise WadError(f"{self.path.name}: absurd entry count {count}")
        minimum = _ENTRY_V1.size if self.major == 1 else _ENTRY_V3.size
        if size < minimum:
            raise WadError(f"{self.path.name}: entry size {size} too small")

        self._fh.seek(offset)
        raw = self._read(size * count)
        for i in range(count):
            chunk = raw[i * size:(i + 1) * size]
            if self.major == 1:
                fields = _ENTRY_V1.unpack(chunk[:_ENTRY_V1.size])
                entry = Entry(*fields[:4], type=fields[4])
            else:
                (path_hash, off, csize, usize, packed, duplicate,
                 subchunk, checksum) = _ENTRY_V3.unpack(chunk[:_ENTRY_V3.size])
                entry = Entry(path_hash, off, csize, usize, packed & 0xF,
                              packed >> 4, duplicate, subchunk, checksum)
            self.entries[entry.hash] = entry
            self.order.append(entry)

    def _read(self, count: int) -> bytes:
        blob = self._fh.read(count)
        if len(blob) != count:
            raise WadError(f"{self.path.name}: truncated ({len(blob)}/{count})")
        return blob

    # -- content -----------------------------------------------------------

    def get(self, path: str):
        """The entry for *path*, or None. Paths are lowercased for you."""
        return self.entries.get(xxh64_path(path))

    def raw(self, entry: Entry) -> bytes:
        """The entry's bytes as stored, still compressed."""
        if self._fh is None:
            raise WadError(f"{self.path.name}: archive is closed")
        try:
            self._fh.seek(entry.offset)
            blob = self._fh.read(entry.csize)
        except OSError as exc:
            raise WadError(f"{self.path.name}: read failed: {exc}") from exc
        if len(blob) != entry.csize:
            raise WadError(f"{self.path.name}: entry {entry.hash:016x} runs "
                           f"past the end of the file")
        return blob

    def read(self, entry: Entry) -> bytes:
        """The entry's content, decompressed."""
        blob = self.raw(entry)
        try:
            if entry.type == RAW:
                data = blob
            elif entry.type == GZIP:
                data = gzip.decompress(blob)
            elif entry.type == REDIRECT:
                raise WadError(f"entry {entry.hash:016x} is a redirection")
            elif entry.type in (ZSTD, ZSTD_CHUNKED):
                data = _unzstd(blob)
            else:
                raise WadError(f"entry {entry.hash:016x}: unknown "
                               f"compression {entry.type}")
        except WadError:
            raise
        except Exception as exc:  # noqa: BLE001 - any codec failure is structural
            raise WadError(f"entry {entry.hash:016x}: "
                           f"{type(exc).__name__}: {exc}") from exc
        if len(data) != entry.usize:
            raise WadError(f"entry {entry.hash:016x}: expected {entry.usize} "
                           f"bytes, decompressed {len(data)}")
        return data


def _unzstd(blob: bytes) -> bytes:
    """Decompress, across frames.

    A subchunked entry is several zstd frames written back to back. Reading
    only the first -- which is what a plain decompressobj does -- returns a
    prefix that then fails the size check, so the whole stream is read.
    """
    if zstandard is None:
        raise WadError("zstandard is not installed")
    reader = zstandard.ZstdDecompressor().stream_reader(
        blob, read_across_frames=True)
    return reader.read()


def pack_wad(items: Iterable[Tuple[int, bytes]], level: int = 3) -> bytes:
    """A v3.3 WAD holding *items*, ``(path_hash, payload)`` pairs.

    Entries are sorted by hash, which is what the game's own archives do and
    what makes two runs of this produce the same bytes. Returned rather than
    written: a skin mod is a few kilobytes and its only destination is inside
    a zip, so a temporary file on the way there buys nothing.
    """
    if zstandard is None or xxhash is None:
        raise WadError("xxhash and zstandard are needed to write a WAD")

    entries = sorted(items)
    header = (MAGIC + bytes([3, 3]) + b"\x00" * 256 + b"\x00" * 8
              + struct.pack("<I", len(entries)))
    body_at = len(header) + _ENTRY_V3.size * len(entries)

    compressor = zstandard.ZstdCompressor(level=level)
    index, body, offset = bytearray(), bytearray(), body_at
    for path_hash, payload in entries:
        packed = compressor.compress(payload)
        index += _ENTRY_V3.pack(path_hash, offset, len(packed), len(payload),
                                ZSTD, 0, 0,
                                xxhash.xxh3_64(packed).intdigest())
        body += packed
        offset += len(packed)

    return bytes(header) + bytes(index) + bytes(body)


def write_wad(path, items: Iterable[Tuple[int, bytes]], level: int = 3) -> None:
    """`pack_wad` onto disk, through a temporary name."""
    target = Path(path)
    tmp = target.with_name(f".{target.name}.part")
    try:
        tmp.write_bytes(pack_wad(items, level=level))
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
