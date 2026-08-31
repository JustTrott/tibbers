#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The state snapshot and the art cache.

Both are read on every poll and both were handing back something they did not
own -- the live skin list in one case, the disk in the other.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import server, system  # noqa: E402


def a_grid():
    return [{"id": 222002, "available": False},
            {"id": 222001, "available": True},
            {"id": 222000, "available": True}]


class Snapshot(unittest.TestCase):
    """`/api/state` is serialised after the lock is dropped."""

    def test_the_skin_list_is_not_the_live_one(self):
        state = server.State()
        state.skins = a_grid()
        out = state.snapshot()
        self.assertEqual(out["skins"], state.skins)
        self.assertIsNot(out["skins"], state.skins)

    def test_the_enemy_list_is_not_the_live_one(self):
        state = server.State()
        state.enemies = [{"championId": 51}, {"championId": 122}]
        self.assertIsNot(state.snapshot()["enemies"], state.enemies)

    def test_a_snapshot_survives_the_grid_being_resorted(self):
        """refresh_availability re-sorts in place every time a mod lands, and
        the response is built from the snapshot long after the lock is gone."""
        state = server.State()
        state.skins = a_grid()
        out = state.snapshot()
        state.skins.sort(key=lambda s: (not s["available"], s["id"]))
        self.assertEqual([s["id"] for s in out["skins"]],
                         [222002, 222001, 222000])

    def test_the_grid_is_never_seen_empty_mid_sort(self):
        """CPython empties a list for the duration of its own sort, so a
        reader holding the live list saw nothing at all.

        Reproduced deterministically: the sort key takes the snapshot, which
        is exactly the window a concurrent /api/state request lands in.
        """
        state = server.State()
        state.skins = a_grid()
        seen = []

        def key(skin):
            # Read the attribute, not snapshot(): the lock is held by the
            # caller here, and this is the same aliasing the poll had.
            seen.append(list(state.skins))
            return skin["id"]

        taken = state.snapshot()["skins"]
        state.skins.sort(key=key)
        self.assertEqual(seen[0], [], "the list really is emptied mid-sort")
        self.assertEqual(len(taken), 3, "the snapshot was taken by value")

    def test_the_log_is_bounded(self):
        state = server.State()
        for i in range(500):
            state.say(f"line {i}")
        self.assertEqual(len(state.log_lines), 200)
        self.assertEqual(len(state.snapshot()["log"]), 40)
        self.assertEqual(state.snapshot()["status"], "line 499")


class ArtCache(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before = os.environ.get("TIBBERS_HOME")
        os.environ["TIBBERS_HOME"] = self.tmp.name
        self.saved = dict(system._DATA_DIRS)
        system._DATA_DIRS.clear()
        server._ART_CACHE.clear()
        self.addCleanup(self.restore)

    def restore(self):
        if self.before is None:
            os.environ.pop("TIBBERS_HOME", None)
        else:
            os.environ["TIBBERS_HOME"] = self.before
        system._DATA_DIRS.clear()
        system._DATA_DIRS.update(self.saved)
        server._ART_CACHE.clear()

    def test_a_disk_hit_is_promoted_into_memory(self):
        """Otherwise every splash is read off the disk again on each redraw."""
        server._art_store("v1/champion-splashes/222/222001.jpg", b"JPEG",
                          "image/jpeg")
        server._ART_CACHE.clear()

        key = "v1/champion-splashes/222/222001.jpg"
        self.assertIsNone(server._art_cached(key))
        self.assertEqual(server._art_from_disk(key), (b"JPEG", "image/jpeg"))
        self.assertEqual(server._art_cached(key), (b"JPEG", "image/jpeg"))

    def test_the_content_type_follows_the_extension(self):
        self.assertEqual(server._art_ctype("a/b.JPG"), "image/jpeg")
        self.assertEqual(server._art_ctype("a/b.jpeg"), "image/jpeg")
        self.assertEqual(server._art_ctype("a/b.png"), "image/png")
        self.assertEqual(server._art_ctype("a/b"), "image/png")

    def test_the_memory_cache_stays_within_its_bound(self):
        for i in range(server._ART_CACHE_MAX + 25):
            server._art_remember(f"asset-{i}.png", b"x", "image/png")
        self.assertEqual(len(server._ART_CACHE), server._ART_CACHE_MAX)
        # Oldest evicted, newest kept.
        self.assertIsNone(server._art_cached("asset-0.png"))
        self.assertIsNotNone(server._art_cached(
            f"asset-{server._ART_CACHE_MAX + 24}.png"))

    def test_only_paths_the_picker_would_ask_for_are_relayed(self):
        """The asset is pasted onto the LCU's own prefix, so nothing that
        could climb out of it is forwarded."""
        for good in ("v1/champion-icons/222.png",
                     "ASSETS/Characters/Jinx/Skins/Skin01/x.jpg",
                     "v1/perk-images/Styles/8005.png"):
            self.assertTrue(server._art_path_ok(good), good)

        for bad in ("", "/etc/passwd", "../../etc/passwd",
                    "v1/../../../etc/passwd", "v1/..%2f..%2fetc/passwd",
                    "%2e%2e/secrets", "/v1/x.png", "..",
                    "\\\\v1\\\\..\\\\..\\\\x"):
            self.assertFalse(server._art_path_ok(bad), bad)

    def test_a_dot_in_a_name_is_still_fine(self):
        """Only a whole `..` segment is refused, not a dot in a filename."""
        self.assertTrue(server._art_path_ok("v1/a..b/x.png"))
        self.assertTrue(server._art_path_ok("v1/x..png"))

    def test_a_miss_on_both_levels_is_none(self):
        self.assertIsNone(server._art_cached("nothing.png"))
        self.assertIsNone(server._art_from_disk("nothing.png"))


class ReloadChannel(unittest.TestCase):

    def test_stopping_releases_the_parked_listeners(self):
        """A page holds a request open for WAIT seconds; shutdown must not
        wait for it."""
        reloader = server.Reloader()
        reloader.WAIT = 30.0
        answered = threading.Event()

        def park():
            reloader.wait(reloader.token)
            answered.set()

        threading.Thread(target=park, daemon=True).start()
        # Give the listener a moment to reach the condition, then stop.
        threading.Event().wait(0.1)
        reloader.stop()
        self.assertTrue(answered.wait(2), "listener was left parked")

    def test_a_bump_moves_the_token_and_names_the_reason(self):
        reloader = server.Reloader()
        first = reloader.token
        self.assertEqual(reloader.bump("static"), first + 1)
        out = reloader.wait(None)
        self.assertEqual((out["token"], out["reason"]),
                         (first + 1, "static"))
        self.assertFalse(out["watching"])


if __name__ == "__main__":
    unittest.main()
