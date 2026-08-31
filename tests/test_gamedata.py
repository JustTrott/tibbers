#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
What happens when the reference data cannot be read.

Every name and icon in the guide comes from here, and a failure used to be
remembered exactly like a success: with the client down and Community Dragon
briefly unreachable, one blip left the whole session rendering blank rows.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import gamedata, system  # noqa: E402

ITEMS = [{"id": 3031, "name": "Infinity Edge", "iconPath": "/ie.png"},
         {"id": 6672, "name": "Kraken Slayer", "iconPath": "/kraken.png"}]
CHAMPION = {"id": 222, "name": "Jinx",
            "passive": {"name": "Get Excited!", "abilityIconPath": "/p.png"},
            "spells": [{"spellKey": "q", "name": "Switcheroo!",
                        "abilityIconPath": "/q.png"}]}


class Offline(unittest.TestCase):
    """A GameData whose downloads can be made to fail on demand."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before = os.environ.get("TIBBERS_HOME")
        os.environ["TIBBERS_HOME"] = self.tmp.name
        self.saved = dict(system._DATA_DIRS)
        system._DATA_DIRS.clear()
        self.addCleanup(self.restore)

        self.data = gamedata.GameData(lambda: None)
        self.data._patch = "26.17"          # no client to ask
        self.attempts = []
        self.answer = None
        self.data._download = self.download

    def restore(self):
        if self.before is None:
            os.environ.pop("TIBBERS_HOME", None)
        else:
            os.environ["TIBBERS_HOME"] = self.before
        system._DATA_DIRS.clear()
        system._DATA_DIRS.update(self.saved)

    def download(self, asset_path):
        self.attempts.append(asset_path)
        return self.answer

    def test_a_failure_is_not_remembered_as_an_answer(self):
        self.assertIsNone(self.data.item(3031))
        self.assertEqual(len(self.attempts), 1)

        # The window passes and the download starts working.
        self.data._failed.clear()
        self.answer = ITEMS
        self.assertEqual(self.data.item(3031)["name"], "Infinity Edge")

    def test_within_the_window_it_does_not_ask_again(self):
        """A retry on every lookup would put the network inside the guide's
        own rendering, which is worse than the blank it replaced."""
        for _ in range(20):
            self.assertIsNone(self.data.item(3031))
            self.assertIsNone(self.data.rune(8005))
        self.assertEqual(len(self.attempts), 2, self.attempts)

    def test_a_success_is_cached_and_asked_for_once(self):
        self.answer = ITEMS
        for _ in range(10):
            self.assertEqual(self.data.item(6672)["name"], "Kraken Slayer")
        self.assertEqual(len(self.attempts), 1)

    def test_an_unknown_id_is_none_even_when_the_file_loaded(self):
        self.answer = ITEMS
        self.assertIsNone(self.data.item(9999))

    def test_abilities_do_not_pin_an_empty_answer_either(self):
        self.assertEqual(self.data.abilities(222), {})
        self.assertEqual(len(self.attempts), 1)
        self.data._failed.clear()
        self.answer = CHAMPION
        out = self.data.abilities(222)
        self.assertEqual(out["p"]["name"], "Get Excited!")
        self.assertEqual(out["q"]["icon"], "/q.png")

    def test_abilities_within_the_window_ask_once(self):
        for _ in range(10):
            self.assertEqual(self.data.abilities(222), {})
        self.assertEqual(len(self.attempts), 1)

    def test_a_loaded_file_is_read_from_disk_next_time(self):
        self.answer = ITEMS
        self.assertIsNotNone(self.data.item(3031))
        fresh = gamedata.GameData(lambda: None)
        fresh._patch = "26.17"
        fresh._download = lambda path: self.fail("should have read the cache")
        self.assertEqual(fresh.item(3031)["name"], "Infinity Edge")


class PatchResolution(unittest.TestCase):
    """The app usually starts before League, so "latest" must not stick."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before = os.environ.get("TIBBERS_HOME")
        os.environ["TIBBERS_HOME"] = self.tmp.name
        self.saved = dict(system._DATA_DIRS)
        system._DATA_DIRS.clear()
        self.addCleanup(self.restore)

        self.client = None
        self.asked = []
        self.data = gamedata.GameData(lambda: self.client)

    def restore(self):
        if self.before is None:
            os.environ.pop("TIBBERS_HOME", None)
        else:
            os.environ["TIBBERS_HOME"] = self.before
        system._DATA_DIRS.clear()
        system._DATA_DIRS.update(self.saved)

    def a_client(self, version="26.17"):
        asked = self.asked

        class Client:
            def get(self, endpoint, timeout=5.0):
                asked.append(endpoint)
                return f"{version}+branch.releases" if "game-version" in endpoint \
                    else None
        return Client()

    def test_without_a_client_it_is_the_placeholder(self):
        self.assertEqual(self.data.patch(), gamedata.PLACEHOLDER_PATCH)

    def test_the_placeholder_is_not_asked_about_until_a_client_appears(self):
        for _ in range(20):
            self.data.patch()
        self.assertEqual(self.asked, [], "nothing to ask")

        self.client = self.a_client()
        self.assertEqual(self.data.patch(), "26.17")
        self.assertEqual(len(self.asked), 1)

    def test_a_real_answer_is_asked_for_once_and_then_settled(self):
        self.client = self.a_client()
        for _ in range(20):
            self.assertEqual(self.data.patch(), "26.17")
        self.assertEqual(len(self.asked), 1)

    def test_the_build_suffix_is_dropped(self):
        self.client = self.a_client("26.18")
        self.assertEqual(self.data.patch(), "26.18")

    def test_the_cache_moves_out_of_the_placeholder_directory(self):
        """Otherwise the files stay filed under a patch nobody is on, and the
        in-memory copies go on answering from there all session."""
        self.data._download = lambda path: ITEMS
        self.assertEqual(self.data.item(3031)["name"], "Infinity Edge")
        placeholder = Path(self.tmp.name) / "gamedata" / \
            gamedata.PLACEHOLDER_PATCH
        self.assertTrue((placeholder / "items.json").is_file())

        # The client turns up. The very next lookup is what notices.
        self.client = self.a_client()
        self.assertEqual(self.data.item(3031)["name"], "Infinity Edge")
        self.assertEqual(self.data.patch(), "26.17")
        self.assertTrue((Path(self.tmp.name) / "gamedata" / "26.17"
                         / "items.json").is_file(),
                        "the lookup should have re-filed it under the patch")

    def test_a_client_that_will_not_say_leaves_the_placeholder(self):
        class Silent:
            def get(self, endpoint, timeout=5.0):
                return None
        self.client = Silent()
        self.assertEqual(self.data.patch(), gamedata.PLACEHOLDER_PATCH)
        # And it keeps asking, because a client is there to ask.
        self.assertEqual(self.data.patch(), gamedata.PLACEHOLDER_PATCH)


if __name__ == "__main__":
    unittest.main()
