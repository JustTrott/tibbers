#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Building a skin mod out of the game install.

The unit tests here are about the two file formats, because everything else
in `skinsmith` is arithmetic on top of them: get a hash or an offset wrong and
the mod is silently a different skin. The vectors are not invented -- the path
and entry hashes are read off a mod somebody else authored, which is the only
kind of vector worth having for a format with no specification.

The last case is the real one: regenerate part of the user's own library from
the install and check it comes back the same, decompressed. It needs the game
installed and a library to compare against, so it skips itself when either is
missing.
"""

from __future__ import annotations

import json
import os
import random
import struct
import sys
import tempfile
import unittest

import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import library, skinsmith, system, wad  # noqa: E402
from tibbers.wad import fnv1a32                      # noqa: E402

HAVE_CODECS = wad.available()


# ---------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------

class Fnv1a(unittest.TestCase):
    """FNV-1a/32, which is what a .bin names its entries and properties with."""

    def test_published_vectors(self):
        self.assertEqual(fnv1a32(""), 0x811C9DC5)
        self.assertEqual(fnv1a32("a"), 0xE40C292C)
        self.assertEqual(fnv1a32("foobar"), 0xBF9CF968)

    def test_property_name_from_a_real_mod(self):
        # The field a chroma is a chroma by. Named in every skin bin the game
        # ships, and the one value in this file nothing else would catch.
        self.assertEqual(fnv1a32("skinClassification"), 0x87225880)

    def test_entry_hashes_from_a_real_mod(self):
        # Read off Challenger Ahri's downloaded mod: the two entries it keeps
        # are hashed from these names, so if this drifts the game looks up a
        # skin that is not there.
        self.assertEqual(fnv1a32("characters/ahri/skins/skin0"), 0x2A5DEB8F)
        self.assertEqual(fnv1a32("characters/ahri/skins/skin0/resources"),
                         0x1CED2C85)

    def test_case_and_bytes_are_the_same_hash(self):
        self.assertEqual(fnv1a32("SkinClassification"),
                         fnv1a32("skinclassification"))
        self.assertEqual(fnv1a32(b"Characters/Ahri"), fnv1a32("characters/ahri"))


@unittest.skipUnless(HAVE_CODECS, "xxhash is not installed")
class Xxh64(unittest.TestCase):
    """xxh64, which is what a WAD files a path under."""

    def test_published_vector(self):
        self.assertEqual(wad.xxh64_path(""), 0xEF46DB3751D8E999)

    def test_path_from_a_real_mod(self):
        # The single entry in Challenger Ahri's mod is filed under this. It is
        # the hash of the *base* skin's path, which is the whole trick.
        self.assertEqual(wad.xxh64_path("data/characters/ahri/skins/skin0.bin"),
                         0x49E643F9C8A74BC7)

    def test_case_does_not_matter(self):
        self.assertEqual(wad.xxh64_path("DATA/Characters/Ahri/Skins/Skin0.bin"),
                         wad.xxh64_path("data/characters/ahri/skins/skin0.bin"))


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_CODECS, "xxhash and zstandard are not installed")
class Archive(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "Test.wad.client"

    def test_round_trip(self):
        items = [
            (0x1122334455667788, b"PROP" + b"\x03\x00\x00\x00" + b"x" * 500),
            (0x00000000000000FF, b"small"),
            (0xFFFFFFFFFFFFFFFF, bytes(range(256)) * 40),
        ]
        wad.write_wad(self.path, items)
        with wad.Wad(self.path) as archive:
            self.assertEqual(len(archive.order), len(items))
            for path_hash, payload in items:
                entry = archive.entries[path_hash]
                self.assertEqual(archive.read(entry), payload)
                self.assertEqual(entry.usize, len(payload))

    def test_entries_are_ordered_by_hash(self):
        wad.write_wad(self.path, [(9, b"nine"), (1, b"one"), (5, b"five")])
        with wad.Wad(self.path) as archive:
            self.assertEqual([e.hash for e in archive.order], [1, 5, 9])

    def test_an_empty_archive_is_still_an_archive(self):
        wad.write_wad(self.path, [])
        with wad.Wad(self.path) as archive:
            self.assertEqual(archive.order, [])

    def test_lookup_is_by_path(self):
        payload = b"content"
        wad.write_wad(self.path,
                      [(wad.xxh64_path("data/characters/x/skins/skin0.bin"),
                        payload)])
        with wad.Wad(self.path) as archive:
            found = archive.get("DATA/Characters/X/Skins/Skin0.bin")
            self.assertIsNotNone(found)
            self.assertEqual(archive.read(found), payload)
            self.assertIsNone(archive.get("data/characters/x/skins/skin1.bin"))

    def test_something_else_entirely_is_refused(self):
        self.path.write_bytes(b"PK\x03\x04not a wad at all")
        with self.assertRaises(wad.WadError):
            wad.Wad(self.path)

    def test_a_truncated_archive_is_refused(self):
        wad.write_wad(self.path, [(1, b"one"), (2, b"two")])
        whole = self.path.read_bytes()
        self.path.write_bytes(whole[:len(whole) // 2])
        # Either the index runs off the end or the body does; both are
        # structural and both have to be the typed error.
        with self.assertRaises(wad.WadError):
            with wad.Wad(self.path) as archive:
                for entry in archive.order:
                    archive.read(entry)

    def test_a_corrupt_payload_is_refused(self):
        wad.write_wad(self.path, [(1, b"a payload worth compressing" * 20)])
        blob = bytearray(self.path.read_bytes())
        blob[-5:] = b"\x00\x00\x00\x00\x00"
        self.path.write_bytes(bytes(blob))
        with wad.Wad(self.path) as archive:
            with self.assertRaises(wad.WadError):
                archive.read(archive.order[0])


# ---------------------------------------------------------------------------
# The property file
# ---------------------------------------------------------------------------

U32, STRING, HASH, LINK = 7, 16, 17, 0x84


def _string(text):
    """A STRING value: its length, then its bytes."""
    encoded = text.encode("utf-8")
    return struct.pack("<H", len(encoded)) + encoded


def prop(entries, version=3, linked=("original.bin",)):
    """A synthetic `.bin`. *entries* are ``(class, hash, [(name, type, value)])``."""
    out = bytearray(b"PROP" + struct.pack("<I", version))
    out += struct.pack("<I", len(linked))
    for line in linked:
        encoded = line.encode("utf-8")
        out += struct.pack("<H", len(encoded)) + encoded
    out += struct.pack("<I", len(entries))
    for cls, _hash, _fields in entries:
        out += struct.pack("<I", cls)
    for _cls, entry_hash, fields in entries:
        body = bytearray(struct.pack("<I", entry_hash)
                         + struct.pack("<H", len(fields)))
        for name, kind, value in fields:
            body += struct.pack("<I", name) + bytes([kind]) + value
        out += struct.pack("<I", len(body)) + bytes(body)
    return bytes(out)


def a_skin(number=3, character="testy", classification=1):
    """A minimal skin bin: the two entries a mod keeps, plus one it drops.

    The skin entry carries the three fields the second convention rewrites --
    `objectPath`, `mResourceResolver`, `championSkinName` -- in among the ones
    it must not touch, so a test can tell a rewrite from a stampede.
    """
    return prop([
        (skinsmith.SKIN_CLASS,
         fnv1a32(f"characters/{character}/skins/skin{number}"),
         [(skinsmith.SKIN_CLASSIFICATION, U32,
           struct.pack("<I", classification)),
          (skinsmith.OBJECT_PATH, HASH,
           struct.pack("<I",
                       fnv1a32(f"characters/{character}/skins/skin{number}"))),
          (skinsmith.CHAMPION_SKIN_NAME, STRING,
           _string(f"TestySkin{number}")),
          (skinsmith.RESOURCE_RESOLVER, LINK,
           struct.pack("<I", fnv1a32(
               f"characters/{character}/skins/skin{number}/resources"))),
          (fnv1a32("skinAudioProperties"), STRING, _string("kept!!"))]),
        (skinsmith.RESOLVER_CLASS,
         fnv1a32(f"characters/{character}/skins/skin{number}/resources"),
         [(fnv1a32("resourceMap"), HASH, struct.pack("<I", 0xDEADBEEF))]),
        (0x11223344,
         fnv1a32(f"characters/{character}/skins/skin{number}/extra"),
         [(fnv1a32("dropMe"), U32, struct.pack("<I", 7))]),
    ])


class Parsing(unittest.TestCase):

    def test_a_synthetic_bin_reads_back(self):
        parsed = skinsmith.Bin(a_skin())
        self.assertEqual(parsed.version, 3)
        self.assertEqual(parsed.linked, ["original.bin"])
        self.assertEqual([e.cls for e in parsed.entries],
                         [skinsmith.SKIN_CLASS, skinsmith.RESOLVER_CLASS,
                          0x11223344])

    def test_not_a_property_file(self):
        with self.assertRaises(skinsmith.BinError):
            skinsmith.Bin(b"BKHD" + b"\x00" * 40)

    def test_truncated(self):
        whole = a_skin()
        with self.assertRaises(skinsmith.BinError):
            skinsmith.Bin(whole[:len(whole) - 8])

    def test_an_unknown_value_type(self):
        broken = prop([(skinsmith.SKIN_CLASS, 1,
                        [(fnv1a32("what"), 0x7F, b"\x00")])])
        with self.assertRaises(skinsmith.BinError):
            skinsmith.Bin(broken)


class Rewriting(unittest.TestCase):

    def rewritten(self, **kwargs):
        source = skinsmith.Bin(a_skin(**kwargs))
        built = skinsmith.rewrite(source, kwargs.get("character", "testy"),
                                  kwargs.get("number", 3), "Testy")
        self.assertIsNotNone(built)
        return skinsmith.Bin(built)

    def test_entry_hashes_become_the_base_skin(self):
        out = self.rewritten()
        self.assertEqual(
            sorted(e.hash for e in out.entries),
            sorted([fnv1a32("characters/testy/skins/skin0"),
                    fnv1a32("characters/testy/skins/skin0/resources")]))

    def test_only_the_two_entries_survive(self):
        out = self.rewritten()
        self.assertEqual([e.cls for e in out.entries],
                         [skinsmith.SKIN_CLASS, skinsmith.RESOLVER_CLASS])

    def test_the_rest_of_the_entry_is_carried_through(self):
        out = self.rewritten()
        self.assertIn(b"kept!!", out.data)

    def test_the_linked_list_delegates_to_the_real_skin(self):
        out = self.rewritten(number=42)
        self.assertEqual(out.linked, list(skinsmith.SIGNATURE)
                         + ["DATA/Characters/Testy/Skins/Skin42.bin"])

    def classification(self, parsed):
        entry = parsed.entry(fnv1a32("characters/testy/skins/skin0"),
                             skinsmith.SKIN_CLASS)
        for field in entry.fields:
            if field.name == skinsmith.SKIN_CLASSIFICATION:
                return struct.unpack_from("<I", parsed.data, field.at)[0]
        self.fail("skinClassification did not survive the rewrite")

    def test_a_chroma_becomes_an_ordinary_skin(self):
        self.assertEqual(self.classification(self.rewritten(classification=2)), 1)

    def test_an_ordinary_skin_is_left_alone(self):
        self.assertEqual(self.classification(self.rewritten(classification=1)), 1)

    def test_a_character_with_nothing_to_say_produces_nothing(self):
        # A sub-character that has no entry for this skin: the archive holds a
        # bin for it, but none of it is about skin 3.
        source = skinsmith.Bin(a_skin(character="somebodyelse"))
        self.assertIsNone(skinsmith.rewrite(source, "testy", 3, "Testy"))

    # -- what the first convention must leave alone ------------------------

    def skin_entry(self, parsed):
        return parsed.entry(fnv1a32("characters/testy/skins/skin0"),
                            skinsmith.SKIN_CLASS)

    def named(self, parsed):
        return {f.name: f for f in self.skin_entry(parsed).fields}

    def test_the_three_second_convention_fields_are_kept_by_default(self):
        fields = self.named(self.rewritten())
        self.assertIn(skinsmith.OBJECT_PATH, fields)
        self.assertIn(skinsmith.CHAMPION_SKIN_NAME, fields)
        self.assertIn(skinsmith.RESOURCE_RESOLVER, fields)


class SecondConvention(unittest.TestCase):
    """The other way the repository's author writes a two-entry mod.

    Verified against Risen Legend Ahri, which it reproduces byte for byte;
    applied to the whole of a real library it matches that one mod and breaks
    every other, so `rewrite` only ever does this when asked.
    """

    def rewritten(self, **kwargs):
        source = skinsmith.Bin(a_skin(**kwargs))
        built = skinsmith.rewrite(source, kwargs.get("character", "testy"),
                                  kwargs.get("number", 3), "Testy",
                                  second=True)
        self.assertIsNotNone(built)
        # Re-parsing is the assertion that the splice kept the entry's own
        # length prefix and field count honest.
        return skinsmith.Bin(built)

    def skin_entry(self, parsed):
        return parsed.entry(fnv1a32("characters/testy/skins/skin0"),
                            skinsmith.SKIN_CLASS)

    def value(self, parsed, name):
        for field in self.skin_entry(parsed).fields:
            if field.name == name:
                return parsed.data[field.at:field.end]
        return None

    def test_object_path_is_dropped(self):
        self.assertIsNone(self.value(self.rewritten(), skinsmith.OBJECT_PATH))

    def test_the_resolver_link_follows_the_entry_to_skin_zero(self):
        self.assertEqual(
            self.value(self.rewritten(), skinsmith.RESOURCE_RESOLVER),
            struct.pack("<I",
                        fnv1a32("characters/testy/skins/skin0/resources")))

    def test_the_skin_is_named_after_the_champion(self):
        self.assertEqual(
            self.value(self.rewritten(), skinsmith.CHAMPION_SKIN_NAME),
            _string("Testy"))

    def test_everything_else_is_carried_through(self):
        out = self.rewritten()
        self.assertIn(b"kept!!", out.data)
        self.assertEqual(self.value(out, skinsmith.SKIN_CLASSIFICATION),
                         struct.pack("<I", 1))

    def test_a_chroma_still_becomes_an_ordinary_skin(self):
        self.assertEqual(
            self.value(self.rewritten(classification=2),
                       skinsmith.SKIN_CLASSIFICATION),
            struct.pack("<I", 1))

    def test_the_source_linked_list_is_carried_under_the_delegation(self):
        out = self.rewritten(number=42)
        self.assertEqual(out.linked, list(skinsmith.SIGNATURE)
                         + ["DATA/Characters/Testy/skins/Skin42.bin",
                            "original.bin"])

    def test_the_resolver_entry_survives_untouched(self):
        out = self.rewritten()
        resolver = out.entry(
            fnv1a32("characters/testy/skins/skin0/resources"),
            skinsmith.RESOLVER_CLASS)
        self.assertIsNotNone(resolver)
        self.assertEqual([f.name for f in resolver.fields],
                         [fnv1a32("resourceMap")])

    def test_the_field_count_is_one_lower(self):
        first = skinsmith.Bin(skinsmith.rewrite(
            skinsmith.Bin(a_skin()), "testy", 3, "Testy"))
        self.assertEqual(
            len(self.skin_entry(self.rewritten()).fields),
            len(self.skin_entry(first).fields) - 1)

    def test_the_list_of_ids_is_only_what_has_been_checked(self):
        # A rule with no derivable trigger is a list, and the list is only
        # allowed to grow when a real mod has been diffed against it.
        self.assertEqual(skinsmith.SECOND_CONVENTION, frozenset({103085}))


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------

#: Ground truth is the repository's own files. When the live library has
#: been rebuilt locally, the pre-skinsmith backup still holds the downloaded
#: originals; comparing against our own output would prove nothing.
_data = Path.home() / "Library" / "Application Support" / "tibbers"
_backups = sorted(_data.glob("skins.before-skinsmith-*"))
REAL_LIBRARY = _backups[-1] if _backups else _data / "skins"

#: The twenty ids whose backup mod does not come back identical, because the
#: backup mod was not built from this install in the first place. They differ
#: from the repository's hand work, not from anything this machine can do: the
#: local build of every one of them was accepted in game on 2026-09-01, and it
#: is now the only mod there is for those skins. They stay named here because
#: the *backup* is what this sweep diffs against.
#:
#: Each was diffed entry by entry. What stands in the way, per mod:
#:
#:   25080           Spirit Blossom Morgana -- an `animations/skin0.bin` whose
#:                   graph is an older patch's: float literals rounded to six
#:                   digits and an `mEndFrame` the install's
#:                   `animations/skin80.bin` has not got.
#:   202005          Dark Cosmic Jhin -- a `particleOverride` naming a troy the
#:                   install's skin5 does not, and the loadscreen written out
#:                   as a path string (below).
#:   202037          Dark Cosmic Erasure Jhin -- the same, plus an
#:                   `animations/skin0.bin` with no counterpart in the install:
#:                   the game has no `animations/skin37`, and none of the
#:                   twelve Jhin animation bins it does have matches after the
#:                   transformation.
#:   222029..222036  Heartseeker Jinx and her chromas -- a 1.8 MB
#:                   `jinx_base_sfx_audio.bnk` merged by hand. The install's
#:                   base bank is 1,709,459 bytes and skin29's is 1,463,782;
#:                   the backup's is 1,834,886 and matches neither.
#:   222060          Arcane Fractured Jinx -- 224 `.dds` textures under names
#:                   carrying a `.skins_jinx_skin60` postfix. The install has
#:                   that art as `.tex` under unpostfixed names, and not one of
#:                   the 224 hashes.
#:   360030          Soul Fighter Samira -- links
#:                   `gameplay.samiraskin30viewcontroller.bin`, drops
#:                   `skinUpgradeData`, and writes the loadscreen out as a path
#:                   string (below).
#:   876046..876052  Petals of Spring Lillia and her chromas -- keep a
#:                   `GearSkinUpgrade` entry and write the loadscreen and both
#:                   HUD icon lists out as path strings (below).
#:
#: The path strings are the wall the last ten share. A `.bin` stores an asset
#: reference as the xxh64 of its lowercased path; these mods store the path
#: itself, spelled `ASSETS/Characters/Lillia/HUD/Lillia_Ass_Circle.tex`. That
#: casing is not in the install -- searching every property file in Lillia's,
#: Samira's and Jhin's archives for those spellings finds none of them -- so it
#: can only have come from an external hash list.
#:
#: Anything the recipe *has* learned comes back off the list, so an id here
#: and in `SECOND_CONVENTION` is a contradiction rather than a belt and
#: braces: the sweep must see it come back identical.
DIVERGES_FROM_THE_BACKUP = frozenset({
    25080, 202005, 202037, 360030,
    222029, 222030, 222031, 222032, 222033, 222034, 222035, 222036, 222060,
    876046, 876047, 876048, 876049, 876050, 876051, 876052,
}) - skinsmith.SECOND_CONVENTION

#: Named because they are the ones the recipe was worked out against -- a
#: skin, its chroma, Elementalist Lux, whose ten forms are the reason the
#: sub-character scan exists at all, and every id in `SECOND_CONVENTION`,
#: which is a list precisely because nothing derives it and so has to be
#: re-checked against the real mod on every run rather than sampled.
NAMED_SAMPLE = [103005, 103018, 99007] + sorted(skinsmith.SECOND_CONVENTION)
SAMPLE_SIZE = 20


def champion_aliases():
    """``{champion id: internal name}``, from whatever knows it.

    The client if it is running, and failing that the summary this app has
    already cached beside the library. Read only; nothing here writes to the
    real data directory.
    """
    from tibbers import lcu
    client = lcu.LCU.connect()
    rows = None
    if client is not None:
        rows = client.get("/lol-game-data/assets/v1/champion-summary.json")
    if not rows:
        cached = sorted((REAL_LIBRARY.parent / "gamedata").glob(
            "*/champions.json"))
        for path in reversed(cached):
            try:
                rows = json.loads(path.read_text())
                break
            except (OSError, ValueError):
                continue
    return {int(row["id"]): row.get("alias") or ""
            for row in (rows or []) if row.get("id")}


def library_mods():
    """``[(champion id, mod id, path)]`` for the user's real library."""
    found = []
    for champion in sorted(REAL_LIBRARY.glob("[0-9]*")):
        if not champion.name.isdigit():
            continue
        for skin in sorted(champion.glob("[0-9]*")):
            if not skin.name.isdigit():
                continue
            for mod in sorted(skin.glob("*.fantome")) + sorted(
                    skin.glob("*.zip")):
                found.append((int(champion.name), int(skin.name), mod))
            for chroma in sorted(skin.glob("[0-9]*")):
                if not chroma.name.isdigit():
                    continue
                for mod in sorted(chroma.glob("*.fantome")) + sorted(
                        chroma.glob("*.zip")):
                    found.append((int(champion.name), int(chroma.name), mod))
    return found


def payloads(path):
    """``{path hash: decompressed bytes}`` for every entry in a mod's WADs."""
    out = {}
    with zipfile.ZipFile(path) as bundle:
        inner = [n for n in bundle.namelist() if n.upper().startswith("WAD/")]
        for name in inner:
            with tempfile.NamedTemporaryFile(suffix=".wad") as tmp:
                tmp.write(bundle.read(name))
                tmp.flush()
                with wad.Wad(tmp.name) as archive:
                    for entry in archive.order:
                        out[entry.hash] = archive.read(entry)
    return out


@unittest.skipUnless(HAVE_CODECS, "xxhash and zstandard are not installed")
@unittest.skipUnless(skinsmith.champions_dir() is not None,
                     "no League install to build from")
@unittest.skipUnless(REAL_LIBRARY.is_dir(), "no library to compare against")
class AgainstTheLibrary(unittest.TestCase):
    """Rebuild part of the real library and check it came back the same.

    Never writes to the real data directory: TIBBERS_HOME is pointed at a
    temporary one for the duration, which is where the scan cache lands and
    where the rebuilt mods go.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before = os.environ.get("TIBBERS_HOME")
        os.environ["TIBBERS_HOME"] = self.tmp.name
        self.saved_data = dict(system._DATA_DIRS)
        self.saved_skins = dict(library._SKINS_DIR)
        system._DATA_DIRS.clear()
        library._SKINS_DIR.clear()
        self.addCleanup(self.restore)

    def restore(self):
        if self.before is None:
            os.environ.pop("TIBBERS_HOME", None)
        else:
            os.environ["TIBBERS_HOME"] = self.before
        system._DATA_DIRS.clear()
        system._DATA_DIRS.update(self.saved_data)
        library._SKINS_DIR.clear()
        library._SKINS_DIR.update(self.saved_skins)

    def test_a_sample_comes_back_identical(self):
        aliases = champion_aliases()
        if not aliases:
            self.skipTest("no champion list, so no way to name the archives")
        mods = library_mods()
        if not mods:
            self.skipTest("the library is empty")

        by_id = {mod_id: (champion, path) for champion, mod_id, path in mods}
        chosen = [i for i in NAMED_SAMPLE if i in by_id]
        rest = sorted(set(by_id) - set(chosen))
        # Seeded, so a failure can be reproduced. Only the library changing
        # changes which twenty come up.
        chosen += random.Random(20250829).sample(
            rest, min(SAMPLE_SIZE, len(rest)))

        out = Path(self.tmp.name) / "built"
        same, differ, failed = [], [], []
        for mod_id in chosen:
            champion, theirs = by_id[mod_id]
            key = skinsmith.champion_key(aliases.get(champion, ""))
            if key is None:
                failed.append(f"{mod_id} (no archive for champion {champion})")
                continue
            mine = out / str(mod_id) / f"{mod_id}.fantome"
            try:
                skinsmith.generate(key, mod_id, mine)
            except skinsmith.SkinsmithError as exc:
                failed.append(f"{mod_id} ({exc})")
                continue
            if payloads(mine) == payloads(theirs):
                same.append(mod_id)
            else:
                differ.append(mod_id)

        # The sidecar is what the app reads to know a mod is one of ours.
        for mod_id in same:
            self.assertTrue(
                skinsmith.sidecar_path(out / str(mod_id) / f"{mod_id}.fantome")
                .is_file(), f"{mod_id} was built without a sidecar")

        unexpected = sorted(set(differ + [int(f.split()[0]) for f in failed])
                            - DIVERGES_FROM_THE_BACKUP)
        self.assertEqual(
            unexpected, [],
            f"{len(same)}/{len(chosen)} identical; differ={sorted(differ)} "
            f"failed={failed}; unexpected ids: {unexpected}")


if __name__ == "__main__":
    unittest.main()
