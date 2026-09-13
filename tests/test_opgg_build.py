#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reshaping op.gg's champion page into the build the picker already draws.

op.gg answers for a champion, mode and lane in one request, with the figures
u.gg needed three files and a spoofed TLS handshake to give. What matters here
is that the reshaping is faithful: Riot's own ids come through untouched (the
client supplies every name and icon from them), the recommendation is the row
op.gg puts first, and a win rate is wins over games played. The network is
stubbed; one payload stands in for the real one, trimmed but shaped exactly
like it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers.opgg import OPGG, Unavailable  # noqa: E402


PAGE = {
    "meta": {"version": "16.18"},
    "data": {
        "summary": {
            "average_stats": {"play": 20223, "win_rate": 0.499975},
            "positions": [
                {"name": "MID", "stats": {"play": 14495, "win_rate": 0.504657}},
                {"name": "TOP", "stats": {"play": 300, "win_rate": 0.9}},
            ],
        },
        # Ordered by pick rate, as op.gg serves them: the first row is the
        # recommendation and the rest are alternatives.
        "starter_items": [{"ids": [1056, 2003], "play": 14401, "win": 7276},
                          {"ids": [1055], "play": 200, "win": 100}],
        "core_items": [{"ids": [6655, 3100, 4645], "play": 1046, "win": 535}],
        "boots": [{"ids": [3020], "play": 10449, "win": 5367},
                  {"ids": [3158], "play": 2000, "win": 1100}],
        "last_items": [{"ids": [6655], "play": 12252, "win": 6183}],
        "runes": [{
            "primary_page_id": 8100,
            "primary_rune_ids": [8112, 8143, 8140, 8105],
            "secondary_page_id": 8200,
            "secondary_rune_ids": [8210, 8275],
            "stat_mod_ids": [5005, 5008, 5001],
            "play": 4031, "win": 1892,
        }],
        "summoner_spells": [{"ids": [4, 14], "play": 13377, "win": 6757}],
        "skills": [{"order": ["Q", "E", "W", "Q"], "play": 6378, "win": 3670}],
        "skill_masteries": [{"ids": ["Q", "E", "W"], "play": 8761, "win": 5041}],
        "counters": [
            {"champion_id": 103, "play": 632, "win": 307},
            {"champion_id": 80, "play": 34, "win": 14},
            {"champion_id": 0, "play": 50, "win": 25},      # no champion
            {"champion_id": 238, "play": 0, "win": 0},      # never met
        ],
    },
}


class Build(unittest.TestCase):

    def setUp(self):
        self.client = OPGG()
        self.asked = []

        def fake_get(url, key, ttl=None):
            self.asked.append(url)
            return PAGE

        patcher = mock.patch.object(self.client, "_get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.build = self.client.build(142, "middle", "rift")

    def test_the_lane_is_in_the_url(self):
        # tibbers says "middle", op.gg says "mid".
        self.assertIn("/ranked/142/mid", self.asked[0])

    def test_a_lane_less_mode_asks_for_the_pooled_row(self):
        self.client.build(142, None, "aram")
        self.assertIn("/aram/142/none", self.asked[-1])

    def test_the_patch_comes_from_the_payload(self):
        # No separate manifest to resolve, so no asking for an unpublished one.
        self.assertEqual(self.build["patch"], "16.18")

    def test_figures_are_this_lane_not_every_lane(self):
        self.assertEqual(self.build["matches"], 14495)
        self.assertEqual(self.build["overall"]["winRate"], 50.47)

    def test_riot_item_ids_come_through_untouched(self):
        self.assertEqual(self.build["start"]["items"], [1056, 2003])
        self.assertEqual(self.build["core"]["items"], [6655, 3100, 4645])

    def test_the_first_row_is_the_recommendation(self):
        self.assertEqual(self.build["start"]["matches"], 14401)

    def test_a_win_rate_is_wins_over_games(self):
        self.assertEqual(self.build["core"]["winRate"], 51.15)

    def test_the_keystone_leads_the_primary_tree(self):
        runes = self.build["runes"]
        self.assertEqual(runes["keystone"], 8112)
        self.assertEqual(runes["primary"], [8143, 8140, 8105])
        self.assertEqual(runes["primaryTree"], 8100)
        self.assertEqual(runes["secondaryTree"], 8200)

    def test_stat_shards_are_their_own_block(self):
        self.assertEqual(self.build["shards"]["ids"], [5005, 5008, 5001])

    def test_skills_carry_order_and_priority(self):
        self.assertEqual(self.build["skills"]["order"], ["Q", "E", "W", "Q"])
        self.assertEqual(self.build["skills"]["priority"], ["Q", "E", "W"])

    def test_boots_and_late_items_are_option_lists(self):
        # op.gg has no fourth/fifth/sixth distribution, so they are reported
        # as what it does have rather than sliced into three.
        self.assertEqual([o["itemId"] for o in self.build["boots"]],
                         [3020, 3158])
        self.assertEqual(self.build["late"][0]["itemId"], 6655)

    def test_no_matchup_build_is_offered(self):
        self.assertIsNone(self.build["matchup"])

    def test_a_thin_sample_is_flagged_not_hidden(self):
        page = {"meta": {"version": "16.18"},
                "data": {**PAGE["data"],
                         "summary": {"average_stats": {"play": 12,
                                                       "win_rate": 0.75},
                                     "positions": []}}}
        with mock.patch.object(self.client, "_get", return_value=page):
            thin = self.client.build(142, "middle", "rift")
        self.assertTrue(thin["thin"])
        self.assertEqual(thin["matches"], 12)


class Counters(unittest.TestCase):

    def setUp(self):
        self.client = OPGG()
        patcher = mock.patch.object(self.client, "_get", return_value=PAGE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rows = self.client.counters(142, "middle", "rift")

    def test_worst_matchup_first(self):
        self.assertEqual(self.rows[0]["championId"], 80)
        self.assertEqual(self.rows[0]["winRate"], 41.18)

    def test_rows_without_a_champion_or_a_game_are_dropped(self):
        self.assertEqual([r["championId"] for r in self.rows], [80, 103])


class Modes(unittest.TestCase):

    def test_a_mode_opgg_has_no_build_for_says_so(self):
        # Mayhem borrows ARAM's build, exactly as it did from u.gg; what it
        # must not do is quietly serve Rift numbers.
        with self.assertRaises(Unavailable):
            OPGG().build(142, None, "mayhem")

    def test_an_empty_payload_is_unavailable(self):
        client = OPGG()
        with mock.patch.object(client, "_get", return_value={"data": None}):
            with self.assertRaises(Unavailable):
                client.build(142, "middle", "rift")


class NoLaneYet(unittest.TestCase):
    """Champ select does not always say which lane you are in.

    Blind pick never does, and a draft has not assigned one while you are
    still hovering. u.gg had a pooled row for that; op.gg refuses a laned
    build without a lane -- `/ranked/<id>/none` is a 422, "The position must
    be one of the following types" -- so one has to be chosen.
    """

    ROSTER = [
        {"id": 25, "positions": [
            {"name": "SUPPORT", "stats": {"role_rate": 0.82}},
            {"name": "MID", "stats": {"role_rate": 0.15}}]},
        {"id": 64, "positions": [
            {"name": "JUNGLE", "stats": {"role_rate": 0.95}}]},
    ]

    def setUp(self):
        self.client = OPGG()
        self.asked = []

        def fake_get(url, key, ttl=None):
            self.asked.append(url)
            if key.startswith("roster-"):
                return {"data": self.ROSTER}
            return PAGE

        patcher = mock.patch.object(self.client, "_get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_lane_the_champion_is_actually_played_in_is_used(self):
        self.client.build(25, None, "rift")
        self.assertTrue(any("/ranked/25/support" in u for u in self.asked))
        self.assertFalse(any(u.endswith("/ranked/25/none") for u in self.asked))

    def test_the_build_says_which_lane_it_settled_on(self):
        # Shown rather than left blank: the numbers are a support Morgana's,
        # and a page that does not say so is a page that misleads.
        self.assertEqual(self.client.build(25, None, "rift")["role"], "utility")

    def test_the_highest_share_wins_not_the_first_listed(self):
        self.assertEqual(self.client.primary_position(25, "rift"), "support")

    def test_a_champion_not_in_the_roster_still_gets_a_build(self):
        # Better a mid build than a blank page while champ select catches up.
        self.assertEqual(self.client.primary_position(9999, "rift"), "mid")

    def test_an_explicit_role_is_never_second_guessed(self):
        self.client.build(25, "middle", "rift")
        self.assertTrue(any("/ranked/25/mid" in u for u in self.asked))

    def test_a_mode_with_no_lanes_still_asks_for_the_pooled_row(self):
        self.client.build(25, None, "aram")
        self.assertTrue(any("/aram/25/none" in u for u in self.asked))


if __name__ == "__main__":
    unittest.main()
