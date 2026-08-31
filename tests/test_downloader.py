#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
What it takes to end up with a mod, and what happens when it cannot be built.

There is one way to get a mod now -- build it out of the install -- so the
questions here are narrow: is the build reached, is a mod already on disk left
alone, and does a build that fails leave nothing behind. Nothing here touches
the install: `skinsmith` is replaced, and what is asserted is the decision, not
the file it would have written.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import downloader, library, skinsmith, system  # noqa: E402


class UnderATemporaryHome(unittest.TestCase):
    """A data directory of its own, so nothing here writes to the real one."""

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

    def patch(self, module, name, value):
        before = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, before)


class Decisions(unittest.TestCase):
    """Whether the build is reached at all, and what a failure answers."""

    def setUp(self):
        self.made = []
        self.can_make = True

        def generate(_champion_id, target, key):
            self.made.append((str(target), key))
            return self.can_make

        self.patch(downloader, "_generate", generate)
        self.patch(skinsmith, "available", lambda: True)

    def patch(self, module, name, value):
        before = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, before)

    def target(self, chroma=None):
        return downloader.Target(103005, chroma)

    def test_a_skin_is_built_from_the_install(self):
        self.assertEqual(downloader.prepare(103, self.target(), "Ahri"),
                         "local")
        self.assertEqual(self.made, [("skin 103005", "Ahri")])

    def test_a_chroma_is_built_the_same_way(self):
        self.assertEqual(downloader.prepare(103, self.target(103018), "Ahri"),
                         "local")
        self.assertEqual(self.made,
                         [("chroma 103018 of skin 103005", "Ahri")])

    def test_a_build_that_fails_is_no_mod_at_all(self):
        # There is nowhere else to get one: the skin is simply offered
        # without a mod, and the picker says so.
        self.can_make = False
        self.assertIsNone(downloader.prepare(103, self.target(), "Ahri"))

    def test_without_a_champion_key_nothing_is_built(self):
        self.assertIsNone(downloader.prepare(103, self.target(), None))
        self.assertEqual(self.made, [])

    def test_without_the_codecs_nothing_is_built(self):
        self.patch(skinsmith, "available", lambda: False)
        self.assertIsNone(downloader.prepare(103, self.target(), "Ahri"))
        self.assertEqual(self.made, [])

    # -- what the client's skin list turns into ----------------------------

    def test_chromas_become_targets_alongside_their_skin(self):
        targets = downloader.targets_from([
            {"id": 103005, "chromas": [{"id": 103018}, {"id": 103056}]},
            {"id": 103000},
        ])
        self.assertEqual([(t.skin_id, t.chroma_id) for t in targets],
                         [(103005, None), (103005, 103018), (103005, 103056)])

    def test_plain_ids_still_work(self):
        targets = downloader.targets_from([103005, 103006])
        self.assertEqual([t.leaf for t in targets], [103005, 103006])


class WhenABuildFails(UnderATemporaryHome):
    """A failure has to leave the library exactly as it found it."""

    def setUp(self):
        super().setUp()
        self.target = downloader.Target(103005)
        self.patch(skinsmith, "available", lambda: True)

    def refuse(self, exc):
        def generate(_key, _skin_id, _dest):
            raise exc
        self.patch(skinsmith, "generate", generate)

    def test_a_refused_build_writes_nothing_and_says_why(self):
        self.refuse(skinsmith.NoSuchSkin("the install has no skin5"))
        with self.assertLogs("tibbers.download", "INFO") as logged:
            self.assertIsNone(downloader.prepare(103, self.target, "Ahri"))
        self.assertFalse(downloader.mod_path(103, self.target).exists())
        self.assertIn("the install has no skin5", "\n".join(logged.output))

    def test_a_build_that_raises_writes_nothing_either(self):
        # A bug in the builder is still just "no mod": one skin must not
        # take the rest of the champion down with it.
        self.refuse(RuntimeError("boom"))
        with self.assertLogs("tibbers.download", "WARNING"):
            self.assertIsNone(downloader.prepare(103, self.target, "Ahri"))
        self.assertFalse(downloader.mod_path(103, self.target).exists())


class Staleness(UnderATemporaryHome):
    """Whether a mod already on disk is left alone."""

    def setUp(self):
        super().setUp()
        self.target = downloader.Target(103005)
        self.path = downloader.mod_path(103, self.target)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def test_a_missing_mod_is_wanted(self):
        self.assertTrue(downloader._outstanding(103, self.target))

    def test_a_mod_with_no_sidecar_is_left_alone(self):
        # Not built here -- imported, or left from when mods were downloaded.
        # Nothing local knows what it should look like.
        self.path.write_bytes(b"PK\x03\x04")
        self.assertFalse(downloader._outstanding(103, self.target))

    def test_a_mod_built_from_this_install_is_left_alone(self):
        self.path.write_bytes(b"PK\x03\x04")
        archive = self.stand_in_for_the_install()
        skinsmith._write_sidecar(self.path, "Ahri", 5,
                                 skinsmith._stat(archive))
        self.assertFalse(downloader._outstanding(103, self.target))

    def test_a_mod_built_from_an_older_install_is_built_again(self):
        self.path.write_bytes(b"PK\x03\x04")
        self.stand_in_for_the_install()
        skinsmith._write_sidecar(self.path, "Ahri", 5,
                                 {"size": 1, "mtime": 1.0})
        self.assertTrue(downloader._outstanding(103, self.target))

    def stand_in_for_the_install(self):
        """A file to be the champion's archive, so no game is needed here."""
        archive = Path(self.tmp.name) / "Ahri.wad.client"
        archive.write_bytes(b"RW\x03\x03")
        self.patch(skinsmith, "champion_wad", lambda _key: archive)
        return archive


class BaseSkins(unittest.TestCase):
    def test_the_base_skin_is_never_a_target(self):
        skins = [{"id": 102000, "isBase": True, "chromas": []},
                 {"id": 102008, "chromas": [{"id": 102009}]},
                 103000, 103005]
        got = [(t.skin_id, t.chroma_id) for t in downloader.targets_from(skins)]
        self.assertEqual(got, [(102008, None), (102008, 102009), (103005, None)])


if __name__ == "__main__":
    unittest.main()
