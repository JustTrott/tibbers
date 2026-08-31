#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The mod library's lookups, over a real directory tree.

Skins and chromas sit at different depths but are found by the same rule --
the file named for its own id, or failing that any archive in the folder, so
a directory pulled from a community repository drops in unchanged. That rule
existed twice; these pin it now that it exists once.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import library, system  # noqa: E402


class Lookups(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before = os.environ.get("TIBBERS_HOME")
        os.environ["TIBBERS_HOME"] = self.tmp.name
        self.saved_data, self.saved_skins = (dict(system._DATA_DIRS),
                                             dict(library._SKINS_DIR))
        system._DATA_DIRS.clear()
        library._SKINS_DIR.clear()
        self.addCleanup(self.restore)
        self.root = library.skins_dir()

    def restore(self):
        if self.before is None:
            os.environ.pop("TIBBERS_HOME", None)
        else:
            os.environ["TIBBERS_HOME"] = self.before
        system._DATA_DIRS.clear()
        system._DATA_DIRS.update(self.saved_data)
        library._SKINS_DIR.clear()
        library._SKINS_DIR.update(self.saved_skins)

    def put(self, *parts, name):
        directory = self.root.joinpath(*(str(p) for p in parts))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(b"PK\x03\x04")
        return directory / name

    def test_a_skin_is_found_by_its_own_name(self):
        made = self.put(222, 222001, name="222001.fantome")
        self.assertEqual(library.find_mod(222, 222001), made)

    def test_a_zip_counts_as_a_mod(self):
        made = self.put(222, 222002, name="222002.zip")
        self.assertEqual(library.find_mod(222, 222002), made)

    def test_any_archive_in_the_folder_is_the_fallback(self):
        """A folder pulled from a repository names its file after the skin,
        not after the id."""
        made = self.put(222, 222003, name="Odyssey Jinx.fantome")
        self.assertEqual(library.find_mod(222, 222003), made)

    def test_the_named_file_wins_over_the_fallback(self):
        self.put(222, 222004, name="Aardvark.fantome")
        exact = self.put(222, 222004, name="222004.fantome")
        self.assertEqual(library.find_mod(222, 222004), exact)

    def test_fantome_wins_over_zip(self):
        self.put(222, 222005, name="222005.zip")
        preferred = self.put(222, 222005, name="222005.fantome")
        self.assertEqual(library.find_mod(222, 222005), preferred)

    def test_an_empty_or_missing_folder_has_no_mod(self):
        (self.root / "222" / "222006").mkdir(parents=True)
        self.assertIsNone(library.find_mod(222, 222006))
        self.assertIsNone(library.find_mod(999, 999001))

    def test_a_chroma_lives_inside_its_skin(self):
        made = self.put(222, 222001, 222020, name="222020.fantome")
        self.assertEqual(library.find_chroma_mod(222, 222001, 222020), made)
        # The skin's own folder holds no archive of its own here.
        self.assertIsNone(library.find_mod(222, 222001))

    def test_chroma_folders_are_not_mistaken_for_the_skin_s_mod(self):
        """The chroma directory is a directory, so the skin's glob fallback
        must not pick it up."""
        self.put(222, 222001, 222020, name="222020.fantome")
        self.assertIsNone(library.find_mod(222, 222001))

    def test_available_chromas_lists_only_the_ones_present(self):
        self.put(222, 222001, 222020, name="222020.fantome")
        self.put(222, 222001, 222021, name="anything.zip")
        (self.root / "222" / "222001" / "222022").mkdir(parents=True)
        (self.root / "222" / "222001" / "notanumber").mkdir(parents=True)
        self.assertEqual(library.available_chromas(222, 222001),
                         {222020, 222021})

    def test_available_for_champion_maps_skin_to_file(self):
        one = self.put(222, 222001, name="222001.fantome")
        two = self.put(222, 222002, name="222002.zip")
        (self.root / "222" / "222003").mkdir(parents=True)
        (self.root / "222" / "readme.txt").write_text("x")
        found = library.available_for_champion(222)
        self.assertEqual(found, {222001: one, 222002: two})

    def test_stats_counts_what_is_there(self):
        self.put(222, 222001, name="222001.fantome")
        self.put(103, 103001, name="103001.fantome")
        self.put(103, 103002, name="103002.fantome")
        out = library.stats()
        self.assertEqual((out["champions"], out["skins"]), (2, 3))
        self.assertEqual(library.champions_present(), [103, 222])

    def test_an_empty_library_answers_rather_than_raising(self):
        self.assertEqual(library.champions_present(), [])
        self.assertEqual(library.available_for_champion(1), {})
        self.assertEqual(library.available_chromas(1, 2), set())


if __name__ == "__main__":
    unittest.main()
