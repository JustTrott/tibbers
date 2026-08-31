#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remembered picks, and the file they live in.

The picker re-applies these the moment a champion is locked, so what is
stored and what is dropped is the difference between the right skin and
last week's.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import prefs as prefs_mod  # noqa: E402


class Memory(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preferences.json"
        self.prefs = prefs_mod.Prefs(self.path)

    def test_a_chroma_is_keyed_by_skin_not_champion(self):
        self.prefs.remember(103, 103001, 103052)
        self.assertEqual(self.prefs.skin_for(103), 103001)
        self.assertEqual(self.prefs.chroma_for(103001), 103052)
        # A different skin for the same champion carries no chroma over.
        self.prefs.remember(103, 103002, None)
        self.assertEqual(self.prefs.skin_for(103), 103002)
        self.assertIsNone(self.prefs.chroma_for(103002))
        self.assertEqual(self.prefs.chroma_for(103001), 103052)

    def test_clearing_a_skin_forgets_it(self):
        self.prefs.remember(103, 103001, None)
        self.prefs.remember(103, None, None)
        self.assertIsNone(self.prefs.skin_for(103))

    def test_forget_all_clears_both_halves_in_one_write(self):
        for champion, skin in ((103, 103001), (222, 222002), (54, 54003)):
            self.prefs.remember(champion, skin, skin + 50)
        self.assertEqual(self.prefs.stats(), {"champions": 3, "chromas": 3})

        writes = []
        real = self.prefs.save
        self.prefs.save = lambda: (writes.append(1), real())[1]
        self.prefs.forget_all()

        self.assertEqual(len(writes), 1, "one write, not one per champion")
        self.assertEqual(self.prefs.stats(), {"champions": 0, "chromas": 0})
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["skin_by_champion"], {})
        self.assertEqual(stored["chroma_by_skin"], {})

    def test_clearing_skins_one_by_one_is_not_forgetting(self):
        """Why forget_all exists rather than a loop over remember().

        remember(champion, None, None) drops the skin and nothing else -- the
        chroma branch is only reached when a skin is being set -- so the loop
        settings used to run left every chroma behind, and the memory count on
        the page went on reporting them.
        """
        self.prefs.remember(103, 103001, 103052)
        self.prefs.remember(103, None, None)
        self.assertIsNone(self.prefs.skin_for(103))
        self.assertEqual(self.prefs.chroma_for(103001), 103052)
        self.assertEqual(self.prefs.stats(), {"champions": 0, "chromas": 1})

        self.prefs.forget_all()
        self.assertEqual(self.prefs.stats(), {"champions": 0, "chromas": 0})

    def test_forget_all_leaves_settings_and_geometry_alone(self):
        self.prefs.set("auto_import", True)
        self.prefs.remember_geometry("picker", x=10, y=20)
        self.prefs.remember(103, 103001, None)
        self.prefs.forget_all()
        self.assertTrue(self.prefs.get("auto_import"))
        self.assertEqual(self.prefs.geometry("picker"), {"x": 10, "y": 20})

    def test_it_survives_being_reloaded(self):
        self.prefs.remember(103, 103001, 103052)
        self.prefs.set("auto_show", False)
        again = prefs_mod.Prefs(self.path)
        self.assertEqual(again.skin_for(103), 103001)
        self.assertEqual(again.chroma_for(103001), 103052)
        self.assertFalse(again.get("auto_show"))
        self.assertFalse(again.first_run)


class Geometry(unittest.TestCase):
    """Positions are written on a delay; decisions are written at once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preferences.json"
        self.prefs = prefs_mod.Prefs(self.path)
        self.writes = []
        real = self.prefs.save
        # Count after the write lands, or a waiter can see the count before
        # the file exists.
        self.prefs.save = lambda: (real(), self.writes.append(1))[0]
        self.addCleanup(self.prefs.flush)

    def test_a_drag_is_one_write_not_one_per_event(self):
        for x in range(60):
            self.prefs.remember_geometry("picker", x=x, y=x * 2)
        self.assertEqual(self.writes, [], "nothing written yet")

        self.prefs.flush()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.prefs.geometry("picker"), {"x": 59, "y": 118})
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["geometry"]["picker"], {"x": 59, "y": 118})

    def test_fields_are_merged_not_replaced(self):
        """Position, size and visibility arrive on three different events."""
        self.prefs.remember_geometry("picker", x=10, y=20)
        self.prefs.remember_geometry("picker", width=620, height=470)
        self.prefs.remember_geometry("picker", visible=True)
        self.prefs.flush()
        self.assertEqual(self.prefs.geometry("picker"),
                         {"x": 10, "y": 20, "width": 620, "height": 470,
                          "visible": True})

    def test_a_none_does_not_erase_what_is_there(self):
        self.prefs.remember_geometry("picker", x=10, y=20)
        self.prefs.remember_geometry("picker", x=None, y=None, visible=False)
        self.prefs.flush()
        self.assertEqual(self.prefs.geometry("picker"),
                         {"x": 10, "y": 20, "visible": False})

    def test_flushing_with_nothing_pending_writes_nothing(self):
        self.prefs.flush()
        self.assertEqual(self.writes, [])

    def test_a_setting_is_still_written_at_once(self):
        self.prefs.set("auto_hide", True)
        self.assertEqual(len(self.writes), 1)
        self.prefs.remember(103, 103001, None)
        self.assertEqual(len(self.writes), 2)

    def test_the_delayed_write_happens_on_its_own(self):
        self.prefs.remember_geometry("settings", x=5)
        self.prefs.save_soon(0.01)      # already pending; must not stack
        for _ in range(200):
            if self.writes:
                break
            time.sleep(0.02)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(json.loads(self.path.read_text())["geometry"],
                         {"settings": {"x": 5}})


class Settings(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefs = prefs_mod.Prefs(Path(self.tmp.name) / "preferences.json")

    def test_an_unknown_setting_is_refused(self):
        with self.assertRaises(KeyError):
            self.prefs.set("not_a_setting", True)

    def test_a_file_from_an_older_build_keeps_the_new_defaults(self):
        path = Path(self.tmp.name) / "old.json"
        path.write_text(json.dumps({"settings": {"auto_show": False}}))
        loaded = prefs_mod.Prefs(path)
        self.assertFalse(loaded.get("auto_show"))
        self.assertEqual(loaded.get("import_spells"),
                         prefs_mod.DEFAULTS["import_spells"])


if __name__ == "__main__":
    unittest.main()
