#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a skin mod out of the installed game, instead of downloading one.

A community skin mod carries no art. It is one `.bin` file saying "when the
game asks for this champion's base skin, hand it the pieces of skin N" -- and
every one of those pieces already sits in the install. So the mod can be
derived from the install rather than fetched, which is what this does.

The recipe, verified byte-for-byte against the community mods this library was
seeded with before any of it was built here:

1. Open `<Game>/DATA/FINAL/Champions/<Champ>.wad.client` and take the entry
   `data/characters/<c>/skins/skin<N>.bin`.
2. Keep exactly two of its entries -- `characters/<c>/skins/skin<N>` (a
   SkinCharacterDataProperties) and its `/resources` (a ResourceResolver) --
   and drop everything else, which is the per-skin data the game will read
   from the original file anyway.
3. Rewrite each kept entry's own hash to the `skin0` equivalent, so the game
   finds them when it looks up the base skin.
4. For a chroma, turn `skinClassification` from 2 (chroma) back into 1, or
   the game refuses to show it as a skin in its own right.
5. Replace the linked-file list with the mod signature plus
   `DATA/Characters/<C>/Skins/Skin<N>.bin`, which is what makes the two-entry
   file delegate to the real skin instead of describing it.
6. Emit that as `data/characters/<c>/skins/skin0.bin` inside a WAD, and zip
   the WAD up as `<skinId>.fantome`.

Some champions are several characters. Shyvana's dragon, Fizz's shark, the ten
forms of Elementalist Lux and Sona's three DJ stages each have their own
`characters/<name>` tree, referenced by hash rather than by name, so there is
nothing to read the names off. They are recovered by tokenising every property
file in the champion's archive and asking the archive which tokens have a
`skin<N>.bin` -- a second of work per champion, cached under the data
directory and keyed by the archive's size and mtime so a patch invalidates it.

A handful of the reference mods are written a second way -- see
`SECOND_CONVENTION` -- and a handful more were not built from this install at
all, so what comes out here is not byte-identical to them. Building is the
only way a mod is obtained now: a skin this cannot produce simply has none,
and `downloader.py` says so in the log.
"""

from __future__ import annotations

import json
import logging
import re
import struct
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import system
from .wad import Wad, WadError, fnv1a32, pack_wad, xxh64_path
from . import wad as wad_mod

log = logging.getLogger("tibbers.skinsmith")


class SkinsmithError(Exception):
    """A mod could not be built. Never raised after a file has been written."""


class NoGameFiles(SkinsmithError):
    """No install, or no archive for this champion."""


class NoSuchSkin(SkinsmithError):
    """The install has no `skin<N>.bin` for this champion."""


class BinError(SkinsmithError):
    """A `.bin` was not shaped the way a property file is shaped."""


class Unsupported(SkinsmithError):
    """The skin is not one a two-entry mod can express."""


#: Bumped when the recipe above changes in a way that makes an already
#: generated mod wrong. Recorded in every sidecar, so an old mod can be told
#: apart from a current one without re-deriving it.
#:
#: 3: the skin0 overlay names the base skin (`championSkinName`) rather than
#:    the numbered skin it delegates to, so a patcher that verifies the base
#:    slot accepts it. Bytes differ from a v2 mod, so v2 mods are rebuilt.
GENERATOR = 3

#: Written beside each generated mod. Names the archive it came out of, so a
#: patch that rewrites that archive is detectable, and marks the mod as ours
#: rather than one that came from somewhere else.
SIDECAR = ".source.json"

CHAMPIONS = "DATA/FINAL/Champions"
WAD_SUFFIX = ".wad.client"

SKIN_CLASS = 0x9B67E9F6      # SkinCharacterDataProperties
RESOLVER_CLASS = 0xEF3A0F33  # ResourceResolver
SKIN_CLASSIFICATION = fnv1a32("skinClassification")     # 0x87225880
CLASSIFICATION_CHROMA, CLASSIFICATION_SKIN = 2, 1

#: The three fields the second convention rewrites. `objectPath` repeats the
#: entry's own name as a hash, `mResourceResolver` is the link from the skin to
#: its resolver, and `championSkinName` is the skin's internal name.
OBJECT_PATH = fnv1a32("objectPath")                     # 0x1D369C29
RESOURCE_RESOLVER = fnv1a32("mResourceResolver")        # 0x62286E7E
CHAMPION_SKIN_NAME = fnv1a32("championSkinName")        # 0x2D78C328

#: Skins whose repository mod is written a second way. The two-entry file is
#: the same idea, but the author's tool re-serialises the entry rather than
#: copying it: `objectPath` is dropped, `mResourceResolver` is pointed at the
#: base skin's resolver, `championSkinName` becomes the champion's own name,
#: and the source file's linked list is carried through under the delegation
#: line (which spells `skins` in lower case).
#:
#: Nothing in the install says which convention a mod was written to -- it is
#: the author's choice, not a property of the skin -- so this is a list of the
#: ids where the second convention has been *checked* to reproduce the
#: repository's mod byte for byte, and nothing else. Applied to the whole of a
#: 1,546-mod library it matches one mod and breaks the other 1,525, so it must
#: never be applied on a hunch. `tests/test_skinsmith.py` asserts the identity
#: for every id named here.
SECOND_CONVENTION = frozenset({
    103085,     # Risen Legend Ahri
})

#: The four lines every mod in the library carries ahead of its delegation.
#: Kept because they are part of what the file *is* -- changing them would
#: make a rebuilt library differ from the mods it was seeded with for no
#: reason.
SIGNATURE = ("=" * 40, "  Rose", "  discord.gg/roseskins", "=" * 40)

INFO_AUTHOR = "Rоse"
INFO_DESCRIPTION = "discord.gg/roseskins"

#: Highest skin number probed. Skin ids are ``championId * 1000 + n`` and
#: chromas share the numbering, so a champion with many chromas runs into the
#: high double digits; nothing is anywhere near 256.
MAX_SKIN = 256

#: Characters whose names appear in every archive and are never part of a
#: skin: the mode variants (`jade_ahri` and friends) have their own skin trees
#: and pulling them in produces a mod the game will not load.
EXCLUDED_PREFIXES = ("jade_",)

#: Anything that could name a character directory. Deliberately loose: this is
#: run over raw property files, so it collects mostly rubbish, and the archive
#: itself is what decides which tokens are real.
_TOKEN = re.compile(rb"[A-Za-z0-9_]{2,64}")

#: Property files bigger than this are skipped while hunting for character
#: names. A character name is written down in the small structural files, and
#: decompressing the handful of multi-megabyte ones costs more than the scan.
SCAN_LIMIT = 8 * 1024 * 1024


def available() -> bool:
    """True when a mod can be built here: the packages and the install."""
    return wad_mod.available() and champions_dir() is not None


# ---------------------------------------------------------------------------
# Where the game keeps champions
# ---------------------------------------------------------------------------

_WADS_LOCK = threading.Lock()
_WADS: Dict[str, Path] = {}
_WADS_FOR: Optional[Path] = None


def champions_dir() -> Optional[Path]:
    game, _client = system.find_install()
    if game is None:
        return None
    directory = game / CHAMPIONS
    return directory if directory.is_dir() else None


def champion_wads() -> Dict[str, Path]:
    """``{lowercased stem: path}`` for every champion archive on disk.

    Listed rather than derived from the champion's name, because the archive's
    own spelling is the one the mod has to use: the delegation line names
    `DATA/Characters/<Champ>/...` and the community mods spell it exactly as
    the archive does.
    """
    global _WADS_FOR
    directory = champions_dir()
    if directory is None:
        return {}
    with _WADS_LOCK:
        if _WADS_FOR == directory and _WADS:
            return dict(_WADS)
    found = {}
    try:
        for entry in directory.iterdir():
            name = entry.name
            # `Ahri.wad.client` is the champion; `Ahri.en_US.wad.client` is
            # its localised strings and holds no skin data.
            if not name.endswith(WAD_SUFFIX) or name.count(".") != 2:
                continue
            found[name[:-len(WAD_SUFFIX)].lower()] = entry
    except OSError as exc:
        raise NoGameFiles(f"cannot list {directory}: {exc}") from exc
    with _WADS_LOCK:
        _WADS.clear()
        _WADS.update(found)
        _WADS_FOR = directory
    return found


def champion_key(name: str) -> Optional[str]:
    """The archive stem for a champion alias, with the archive's own casing.

    `MonkeyKing`, `FiddleSticks` -- the client's alias and the archive agree on
    the letters and not always on the case, and the case is what ends up in
    the mod.
    """
    if not name:
        return None
    path = champion_wads().get(str(name).lower())
    return path.name[:-len(WAD_SUFFIX)] if path is not None else None


def champion_wad(key: str) -> Path:
    path = champion_wads().get(str(key).lower())
    if path is None:
        raise NoGameFiles(f"no archive for {key}")
    return path


def skin_number(skin_id: int) -> int:
    """The skin's number within its champion. Ids are champion * 1000 + n."""
    return int(skin_id) % 1000


# ---------------------------------------------------------------------------
# Property files
# ---------------------------------------------------------------------------

#: Value types that are a fixed number of bytes, so they can be stepped over
#: without understanding them.
_FIXED = {0: 0, 1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 4, 7: 4, 8: 8, 9: 8, 10: 4,
          11: 8, 12: 12, 13: 16, 14: 64, 15: 4, 17: 4, 18: 8,
          0x84: 4, 0x87: 1}
U32, _STRING = 7, 16
_LIST, _LIST2, _POINTER, _EMBED, _OPTION, _MAP = 0x80, 0x81, 0x82, 0x83, 0x85, 0x86
_LINK = 0x84


#: A field is a 4-byte name and a 1-byte type ahead of its value, so the field
#: itself starts five bytes before `Field.at`.
FIELD_HEAD = 5


@dataclass(slots=True)
class Field:
    name: int
    type: int
    at: int         # where this field's value starts, in the whole file
    end: int        # and where it ends, so the field can be cut out

    @property
    def start(self) -> int:
        """Where the field begins -- its name, not its value."""
        return self.at - FIELD_HEAD


@dataclass(slots=True)
class BinEntry:
    hash: int
    cls: int
    at: int         # where the entry starts, in the whole file
    length: int
    fields: List[Field]


class Bin:
    """A parsed `.bin`, structurally only.

    Values are located, not decoded: the transformation rewrites four bytes in
    two places and copies the rest through untouched, so knowing where every
    field begins is enough and decoding what it means is work with no reader.
    """

    def __init__(self, data: bytes):
        self.data = data
        self._at = 0
        try:
            self._parse()
        except BinError:
            raise
        except (struct.error, IndexError, UnicodeDecodeError, ValueError,
                RecursionError) as exc:
            raise BinError(f"malformed .bin at byte {self._at}: "
                           f"{type(exc).__name__}: {exc}") from exc

    # -- reading primitives ------------------------------------------------

    def _take(self, count: int) -> bytes:
        end = self._at + count
        if count < 0 or end > len(self.data):
            raise BinError(f"read of {count} at {self._at} runs off the end")
        chunk = self.data[self._at:end]
        self._at = end
        return chunk

    def _u8(self) -> int:
        return self._take(1)[0]

    def _u16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def _u32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def _string(self) -> str:
        return self._take(self._u16()).decode("utf-8", "replace")

    # -- structure ---------------------------------------------------------

    def _parse(self) -> None:
        magic = self._take(4)
        if magic == b"PTCH":
            self._take(8)
            magic = self._take(4)
        if magic != b"PROP":
            raise BinError(f"not a property file (magic {magic!r})")

        self.version = self._u32()
        self.linked: List[str] = []
        if self.version >= 2:
            self.linked = [self._string() for _ in range(self._u32())]

        count = self._u32()
        if count > len(self.data):
            raise BinError(f"absurd entry count {count}")
        classes = [self._u32() for _ in range(count)]

        self.entries: List[BinEntry] = []
        for cls in classes:
            start = self._at
            size = self._u32()
            end = self._at + size
            if end > len(self.data):
                raise BinError(f"entry at {start} claims {size} bytes")
            entry_hash = self._u32()
            fields = [self._field() for _ in range(self._u16())]
            if self._at != end:
                raise BinError(f"entry at {start} ended at {self._at}, "
                               f"not {end}")
            self.entries.append(BinEntry(entry_hash, cls, start, end - start,
                                         fields))

    def _field(self) -> Field:
        name = self._u32()
        kind = self._u8()
        at = self._at
        self._skip(kind)
        return Field(name, kind, at, self._at)

    def _skip(self, kind: int) -> None:
        """Step over one value of *kind*, whatever shape it is."""
        fixed = _FIXED.get(kind)
        if fixed is not None:
            self._take(fixed)
        elif kind == _STRING:
            self._string()
        elif kind in (_LIST, _LIST2):
            item = self._u8()
            self._u32()                     # byte size, which we do not trust
            for _ in range(self._u32()):
                self._skip(item)
        elif kind in (_POINTER, _EMBED):
            if self._u32() == 0:            # a null pointer stops there
                return
            self._u32()                     # byte size
            for _ in range(self._u16()):
                self._field()
        elif kind == _OPTION:
            item = self._u8()
            for _ in range(self._u8()):
                self._skip(item)
        elif kind == _MAP:
            key, value = self._u8(), self._u8()
            self._u32()                     # byte size
            for _ in range(self._u32()):
                self._skip(key)
                self._skip(value)
        else:
            raise BinError(f"unknown value type 0x{kind:02x} at {self._at}")

    # -- what the transformation asks for ----------------------------------

    def entry(self, entry_hash: int, cls: int) -> Optional[BinEntry]:
        for entry in self.entries:
            if entry.hash == entry_hash and entry.cls == cls:
                return entry
        return None

    def body(self, entry: BinEntry) -> bytes:
        return self.data[entry.at:entry.at + entry.length]


# ---------------------------------------------------------------------------
# The transformation
# ---------------------------------------------------------------------------

def rewrite(source: Bin, character: str, number: int, spelled: str,
            signature: Sequence[str] = SIGNATURE,
            second: bool = False,
            base_name: Optional[str] = None) -> Optional[bytes]:
    """One character's `skin<N>.bin`, rewritten as its `skin0.bin`.

    *second* selects the second convention -- see `SECOND_CONVENTION`, and
    only ever for an id listed there.

    *base_name*, when given, renames the delegated skin's `championSkinName` to
    the base skin's own (e.g. `SmolderSkin11` -> `BaseSmolder`). The repository
    mods leave the numbered name in place and cslol serves that, but a stricter
    patcher verifies that a skin0 overlay actually names the base skin and
    rejects one that does not. The name is metadata -- what renders comes from
    the delegation below -- so this is safe for either patcher, and left off
    (None) reproduces the repository bytes exactly. Ignored under *second*,
    which sets the name itself.

    None when this character has nothing to say about the skin, which is the
    ordinary answer for a sub-character that only exists in some skins.
    """
    want_root = fnv1a32(f"characters/{character}/skins/skin{number}")
    want_resources = fnv1a32(
        f"characters/{character}/skins/skin{number}/resources")
    into_root = fnv1a32(f"characters/{character}/skins/skin0")
    into_resources = fnv1a32(f"characters/{character}/skins/skin0/resources")

    kept: List[Tuple[int, bytes]] = []
    for entry in source.entries:
        if entry.hash == want_root and entry.cls == SKIN_CLASS:
            body = bytearray(source.body(entry))
            struct.pack_into("<I", body, 4, into_root)
            # Both of these leave every offset where it was, so the second
            # convention's splice below can still trust the parsed fields.
            _declassify_chroma(body, entry)
            if second:
                body = bytearray(_reserialise(entry, body, into_resources,
                                              spelled))
            elif base_name is not None:
                body = bytearray(_rename_skin(entry, bytes(body), base_name))
            kept.append((SKIN_CLASS, bytes(body)))
        elif entry.hash == want_resources and entry.cls == RESOLVER_CLASS:
            body = bytearray(source.body(entry))
            struct.pack_into("<I", body, 4, into_resources)
            if second:
                body = bytearray(_reserialise(entry, body, into_resources,
                                              spelled))
            kept.append((RESOLVER_CLASS, bytes(body)))

    if not kept:
        return None
    # The skin comes before its resolver, which is the order the game's own
    # files use and the order the reference mods are in.
    kept.sort(key=lambda item: 0 if item[0] == SKIN_CLASS else 1)

    if second:
        # The second convention spells the delegation line's `skins` in lower
        # case and carries the source file's own linked list underneath it.
        linked = list(signature) + [
            f"DATA/Characters/{spelled}/skins/Skin{number}.bin"] + list(
                source.linked)
    else:
        linked = list(signature) + [
            f"DATA/Characters/{spelled}/Skins/Skin{number}.bin"]
    out = bytearray(b"PROP" + struct.pack("<I", source.version))
    out += struct.pack("<I", len(linked))
    for line in linked:
        encoded = line.encode("utf-8")
        out += struct.pack("<H", len(encoded)) + encoded
    out += struct.pack("<I", len(kept))
    for cls, _body in kept:
        out += struct.pack("<I", cls)
    for _cls, body in kept:
        out += body
    return bytes(out)


def _reserialise(entry: BinEntry, body: bytes, into_resources: int,
                 spelled: str) -> bytes:
    """The second convention's three field edits, on one entry's body.

    `objectPath` goes, `mResourceResolver` is pointed at the base skin's
    resolver, and `championSkinName` becomes the champion's own name. An entry
    that carries none of the three -- most resolvers -- comes back untouched.

    *body* is the entry as it stands with its hash already rewritten, which is
    a same-size edit, so the offsets `entry.fields` recorded still hold.
    """
    cuts: List[Tuple[int, int, bytes]] = []     # start, end, replacement
    dropped = 0
    for field in entry.fields:
        if field.name == OBJECT_PATH:
            cuts.append((field.start, field.end, b""))
            dropped += 1
        elif field.name == RESOURCE_RESOLVER and field.type == _LINK:
            cuts.append((field.at, field.end,
                         struct.pack("<I", into_resources)))
        elif field.name == CHAMPION_SKIN_NAME and field.type == _STRING:
            name = spelled.encode("utf-8")
            cuts.append((field.at, field.end,
                         struct.pack("<H", len(name)) + name))
    if not cuts:
        return bytes(body)

    out = bytearray()
    taken = 0
    for start, end, replacement in sorted(cuts):
        start, end = start - entry.at, end - entry.at
        if not 0 <= taken <= start <= end <= len(body):
            raise BinError("a field lies outside its own entry")
        out += body[taken:start]
        out += replacement
        taken = end
    out += body[taken:]

    # The entry's own length prefix and field count are both now wrong.
    struct.pack_into("<I", out, 0, len(out) - 4)
    struct.pack_into("<H", out, 8,
                     struct.unpack_from("<H", out, 8)[0] - dropped)
    return bytes(out)


def _rename_skin(entry: BinEntry, body: bytes, new_name: str) -> bytes:
    """Rewrite one skin entry's `championSkinName` string to *new_name*.

    The same single edit `_reserialise` makes to that field, but on its own and
    without dropping or repointing anything else -- the field count is
    unchanged. *body* has already had its hash rewritten (a same-size edit), so
    the offsets `entry.fields` recorded still hold. Returns *body* untouched
    when the entry carries no `championSkinName`, which is the ordinary case
    for a resolver.
    """
    for field in entry.fields:
        if field.name == CHAMPION_SKIN_NAME and field.type == _STRING:
            encoded = new_name.encode("utf-8")
            replacement = struct.pack("<H", len(encoded)) + encoded
            start, end = field.at - entry.at, field.end - entry.at
            if not 0 <= start <= end <= len(body):
                raise BinError("championSkinName lies outside its own entry")
            out = bytearray(body[:start]) + replacement + body[end:]
            # Only the entry's length prefix moves; the field count is the same.
            struct.pack_into("<I", out, 0, len(out) - 4)
            return bytes(out)
    return bytes(body)


def _declassify_chroma(body: bytearray, entry: BinEntry) -> None:
    """Turn a chroma back into an ordinary skin, in place.

    A chroma is a skin whose `skinClassification` is 2, and the game will not
    offer one as the base skin. Every other difference between a chroma and
    its parent is already in the data being delegated to.
    """
    for field in entry.fields:
        if field.name != SKIN_CLASSIFICATION or field.type != U32:
            continue
        at = field.at - entry.at
        if not 0 <= at <= len(body) - 4:
            raise BinError("skinClassification lies outside its own entry")
        if struct.unpack_from("<I", body, at)[0] == CLASSIFICATION_CHROMA:
            struct.pack_into("<I", body, at, CLASSIFICATION_SKIN)


# ---------------------------------------------------------------------------
# Which characters a champion is made of
# ---------------------------------------------------------------------------

_SCAN_LOCK = threading.Lock()
_LOCKS: Dict[str, threading.Lock] = {}
_TOKENS: Dict[Tuple[str, int, int], frozenset] = {}


def _champion_lock(key: str) -> threading.Lock:
    with _SCAN_LOCK:
        return _LOCKS.setdefault(key.lower(), threading.Lock())


def cache_dir() -> Path:
    directory = system.data_dir() / "skinsmith"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _stat(path: Path) -> dict:
    info = path.stat()
    return {"size": info.st_size, "mtime": info.st_mtime}


def _read_cache(key: str, source: dict) -> dict:
    """The cached scan for this champion, if it was taken from this archive."""
    try:
        blob = json.loads((cache_dir() / f"{key.lower()}.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(blob, dict) or blob.get("generator") != GENERATOR:
        return {}
    if blob.get("wad") != source:
        return {}
    found = blob.get("characters")
    return found if isinstance(found, dict) else {}


def _write_cache(key: str, source: dict, characters: dict) -> None:
    path = cache_dir() / f"{key.lower()}.json"
    payload = json.dumps({"generator": GENERATOR, "wad": source,
                          "characters": characters}, indent=1)
    try:
        tmp = path.with_suffix(".json.part")
        tmp.write_text(payload)
        tmp.replace(path)
    except OSError as exc:
        log.debug("could not cache the scan for %s: %s", key, exc)


def _tokens(archive: Wad, key: str, source: dict) -> frozenset:
    """Every word in every property file in the champion's archive.

    This is the expensive half -- a second of decompression for a big champion
    -- so it is held for the life of the process as well as being distilled
    into the on-disk cache.
    """
    memo_key = (key.lower(), source["size"], int(source["mtime"] * 1000))
    with _SCAN_LOCK:
        found = _TOKENS.get(memo_key)
    if found is not None:
        return found

    words = set()
    for entry in archive.order:
        if entry.usize > SCAN_LIMIT:
            continue
        try:
            data = archive.read(entry)
        except WadError:
            continue            # not every entry is readable, and most are art
        if data[:4] != b"PROP":
            continue
        words.update(match.group(0).lower() for match in _TOKEN.finditer(data))

    # Matched as bytes, because the files are bytes and decoding a few
    # megabytes of art headers to find a word is work for nothing. Decoded
    # once here, where there are thousands of words rather than millions.
    found = frozenset(word.decode("ascii", "ignore") for word in words)
    with _SCAN_LOCK:
        _TOKENS[memo_key] = found
    return found


def characters_for(archive: Wad, key: str, number: int,
                   source: dict) -> List[str]:
    """The character directories this champion's skin *number* is made of.

    The champion itself always, plus whichever sub-characters the archive
    confirms have a `skin<number>.bin` of their own. Cached under the data
    directory, per champion and per skin number, keyed on the archive's size
    and mtime so a patch throws the answer away.
    """
    champion = key.lower()
    with _champion_lock(key):
        cached = _read_cache(key, source)
        found = cached.get(str(number))
        if isinstance(found, list):
            return [str(name) for name in found]

        words = _tokens(archive, key, source)
        others = sorted(
            word for word in words
            if word != champion
            and not word.startswith(EXCLUDED_PREFIXES)
            and xxh64_path(f"data/characters/{word}/skins/skin{number}.bin")
            in archive.entries)
        found = [champion] + others
        cached[str(number)] = found
        _write_cache(key, source, cached)
    return list(found)


# ---------------------------------------------------------------------------
# Sidecars
# ---------------------------------------------------------------------------

def sidecar_path(mod_path) -> Path:
    return Path(mod_path).parent / SIDECAR


def read_sidecar(mod_path) -> dict:
    """What was recorded beside a mod, or ``{}`` if it was not built here."""
    try:
        blob = json.loads(sidecar_path(mod_path).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def is_generated(mod_path) -> bool:
    return bool(read_sidecar(mod_path))


def is_stale(mod_path) -> bool:
    """True when a generated mod no longer matches the install it came from.

    A mod with no sidecar was not built here -- imported by hand, or left from
    when mods were downloaded -- and is never stale: nothing local knows what it
    should look like, so it is kept exactly as it is.
    """
    recorded = read_sidecar(mod_path)
    if not recorded:
        return False
    if recorded.get("generator") != GENERATOR:
        return True
    key = recorded.get("champion")
    try:
        current = _stat(champion_wad(key)) if key else None
    except (NoGameFiles, OSError):
        # The install is gone or unreadable. Whatever is on disk is the best
        # answer available, so leave it alone rather than deleting it.
        return False
    return current != recorded.get("wad")


# ---------------------------------------------------------------------------
# Building one mod
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GenerateResult:
    path: Path
    champion: str
    number: int
    characters: Tuple[str, ...]
    seconds: float
    source: dict


def _base_skin_name(archive: "Wad", character: str) -> Optional[str]:
    """The `championSkinName` of *character*'s base skin, read from the install.

    None when it cannot be read -- a missing skin0, an unparseable bin -- in
    which case the caller leaves the delegated name in place (the repository
    behaviour). What is served does not depend on this; a stricter patcher's
    base-skin check does.
    """
    entry = archive.get(f"data/characters/{character}/skins/skin0.bin")
    if entry is None:
        return None
    try:
        parsed = Bin(archive.read(entry))
    except (BinError, WadError):
        return None
    skin = parsed.entry(
        fnv1a32(f"characters/{character}/skins/skin0"), SKIN_CLASS)
    if skin is None:
        return None
    for field in skin.fields:
        if field.name == CHAMPION_SKIN_NAME and field.type == _STRING:
            length = struct.unpack_from("<H", parsed.data, field.at)[0]
            return parsed.data[field.at + 2:field.at + 2 + length].decode(
                "utf-8", "replace")
    return None


def generate(champion_key: str, skin_id: int, dest_path) -> GenerateResult:
    """Build the mod for *skin_id* out of the install, at *dest_path*.

    *champion_key* is the champion's archive stem -- `Ahri`, `MonkeyKing` --
    which `champion_key()` resolves from the client's alias.

    Raises a `SkinsmithError` for anything structural and leaves no file
    behind when it does; the caller falls back to downloading.
    """
    started = time.monotonic()
    if not wad_mod.available():
        raise SkinsmithError("xxhash and zstandard are not installed")

    key = champion_key
    number = skin_number(skin_id)
    dest = Path(dest_path)
    wad_path = champion_wad(key)
    try:
        source = _stat(wad_path)
    except OSError as exc:
        raise NoGameFiles(f"cannot stat {wad_path}: {exc}") from exc

    champion = key.lower()
    second = int(skin_id) in SECOND_CONVENTION
    with Wad(wad_path) as archive:
        root = archive.get(f"data/characters/{champion}/skins/skin{number}.bin")
        if root is None:
            raise NoSuchSkin(f"{key} has no skin{number}")

        characters = characters_for(archive, key, number, source)
        pieces: List[Tuple[int, bytes]] = []
        for character in characters:
            entry = (root if character == champion else archive.get(
                f"data/characters/{character}/skins/skin{number}.bin"))
            if entry is None:
                continue
            parsed = Bin(archive.read(entry))
            spelled = key if character == champion else character
            # The base skin's own name, so the skin0 overlay names the base
            # skin rather than the numbered one it delegates to. There is no
            # one pattern (Smolder's is `BaseSmolder`, Jhin's is `Jhin`), so it
            # is read from the install rather than guessed.
            base_name = _base_skin_name(archive, character)
            built = rewrite(parsed, character, number, spelled,
                            second=second, base_name=base_name)
            if built is not None:
                pieces.append((
                    xxh64_path(f"data/characters/{character}/skins/skin0.bin"),
                    built))

    if not pieces:
        raise Unsupported(
            f"{key} skin{number} has no skin entry a mod can delegate to")

    _write_fantome(dest, key, number, pieces)
    _write_sidecar(dest, key, number, source)

    return GenerateResult(dest, key, number, tuple(characters),
                          time.monotonic() - started, source)


def _write_fantome(dest: Path, key: str, number: int,
                   pieces: Sequence[Tuple[int, bytes]]) -> None:
    """Zip the archive up as a mod, atomically.

    Built under a dot name and moved into place, because `library.find_mod`
    will happily hand the injector any archive it finds in the folder and a
    half-written one must never be that archive.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".{dest.name}.part"
    info = json.dumps({
        "Author": INFO_AUTHOR,
        "Description": INFO_DESCRIPTION,
        "Name": f"{key} skin{number}",
        "Version": "1.0",
    }, indent=2)
    try:
        blob = pack_wad(pieces)
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"WAD/{key}{WAD_SUFFIX}", blob)
            archive.writestr("META/info.json", info)
        staging.replace(dest)
    except (OSError, zipfile.BadZipFile, WadError) as exc:
        staging.unlink(missing_ok=True)
        raise SkinsmithError(f"could not write {dest.name}: {exc}") from exc


def _write_sidecar(dest: Path, key: str, number: int, source: dict) -> None:
    payload = json.dumps({"generator": GENERATOR, "champion": key,
                          "skin": number, "wad": source,
                          "at": time.time()}, indent=1)
    try:
        sidecar_path(dest).write_text(payload)
    except OSError as exc:
        # The mod itself is good; only staleness detection is lost, and a
        # missing sidecar reads as "not ours", which is the safe answer.
        log.debug("could not write the sidecar for %s: %s", dest.name, exc)
