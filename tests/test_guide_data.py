#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The decodings that are silent when they are wrong.

Every case here reports a plausible number rather than raising when it is
read the wrong way round, which is why each one is pinned rather than
trusted: a win rate printed backwards looks exactly like a win rate.
"""

from __future__ import annotations

import unittest

from tibbers import modes
from tibbers.guide import FINISH_SD, TIER_BANDS, TIER_GATE, Guide
from tibbers.opgg import OPGG
from tibbers.ugg import UGG, Unavailable


class MatchupOrientation(unittest.TestCase):
    """`data[6]` counts wins in a general overview and losses in a matchup.

    Taken from the real cached payloads for Jinx (222) bottom against Caitlyn
    (51) on patch 16_16, Emerald+ worldwide. The matchup overview's `data[6]`
    is byte-for-byte the row this pairing has in the `matchups` file, which is
    `[championId, losses, matches]` -- so 16374 of 35023 games are LOSSES and
    the win rate is 53.25%, not 46.75%.
    """

    #: [wins, matches] -- Jinx bottom, all opponents.
    GENERAL_OVERALL = [183963, 350930]
    #: [losses, matches] -- Jinx bottom into Caitlyn.
    MATCHUP_OVERALL = [16374, 35023]
    #: The same pairing's row in the matchups table: [enemy, losses, matches].
    MATCHUP_TABLE_ROW = [51, 16374, 35023]

    def cell(self, overall):
        # Only data[6] is read here; the rest of the positional array is
        # padding so the index lands where the decoder looks for it.
        cell = [None] * 9
        cell[6] = overall
        return cell

    def test_general_overall_counts_wins(self):
        out = UGG().decode(self.cell(self.GENERAL_OVERALL), {}, matchup=False)
        self.assertEqual(out["overall"]["matches"], 350930)
        self.assertAlmostEqual(out["overall"]["winRate"], 52.42, places=2)

    def test_matchup_overall_counts_losses(self):
        out = UGG().decode(self.cell(self.MATCHUP_OVERALL), {}, matchup=True)
        self.assertEqual(out["overall"]["matches"], 35023)
        self.assertAlmostEqual(out["overall"]["winRate"], 53.25, places=2)

    def test_matchup_agrees_with_the_matchup_table(self):
        """The two files describe the same pairing, so they must agree.

        This is the check that caught it: `build()` said 46.75% where
        `matchup_table()` said 53.25% for the identical pairing and match
        count, and the table is the one whose match-weighted mean reproduces
        the champion's overall win rate.
        """
        _, losses, matches = self.MATCHUP_TABLE_ROW
        from_table = round((1 - losses / matches) * 100, 2)
        from_build = UGG().decode(
            self.cell(self.MATCHUP_OVERALL), {}, matchup=True)["overall"]["winRate"]
        self.assertAlmostEqual(from_build, from_table, places=2)

    def test_reading_a_matchup_as_a_general_one_inverts_it(self):
        """The bug itself, pinned, so the flag cannot be dropped unnoticed."""
        wrong = UGG().decode(
            self.cell(self.MATCHUP_OVERALL), {}, matchup=False)["overall"]["winRate"]
        right = UGG().decode(
            self.cell(self.MATCHUP_OVERALL), {}, matchup=True)["overall"]["winRate"]
        self.assertAlmostEqual(wrong + right, 100.0, places=2)
        self.assertAlmostEqual(wrong, 46.75, places=2)


class ArenaRates(unittest.TestCase):
    """What op.gg's Arena figures mean, measured rather than assumed."""

    #: Garen's real row from the all-champions file, patch 16.17.
    GAREN = {"win": 6588, "play": 12429, "total_place": 42165,
             "first_place": 2119, "pick_rate": 0.126454}

    def test_rates_are_placements(self):
        out = OPGG._rate(self.GAREN)
        self.assertEqual(out["matches"], 12429)
        self.assertAlmostEqual(out["firstRate"], 17.0, places=1)
        self.assertAlmostEqual(out["topRate"], 53.0, places=1)
        self.assertAlmostEqual(out["avgFinish"], 3.39, places=2)
        self.assertAlmostEqual(out["pickRate"], 12.6, places=1)

    def test_win_is_a_top_three_finish(self):
        """Six teams of three: pooled over the whole roster, `win/play` is
        exactly one half, `first_place/play` exactly one sixth and
        `total_place/play` exactly 3.5. Only a top-half -- top three of six --
        reading of `win` satisfies all three at once.

        Reproduced here from a synthetic population whose totals match the
        real file's, so the arithmetic is pinned without a network call.
        """
        play, first, wins, places = 1769202, 294867, 884601, 6192207
        self.assertAlmostEqual(first / play, 1 / 6, places=5)
        self.assertAlmostEqual(wins / play, 1 / 2, places=5)
        self.assertAlmostEqual(places / play, 3.5, places=4)

    def test_empty_row_does_not_divide_by_zero(self):
        out = OPGG._rate({})
        self.assertEqual(out["matches"], 0)
        self.assertIsNone(out["avgFinish"])
        self.assertEqual(out["firstRate"], 0.0)


class ArenaChampionDecode(unittest.TestCase):
    """The all-champions Arena summary, decoded off a real response shape."""

    PAYLOAD = {
        "meta": {"version": "16.17"},
        "data": [
            {"id": 86, "average_stats": {
                "win": 6588, "play": 12429, "total_place": 42165,
                "first_place": 2119, "pick_rate": 0.126454,
                "ban_rate": 0.0593556, "tier": 2, "rank": 34}},
            {"id": 54, "average_stats": {
                "win": 2674, "play": 5259, "total_place": 18276,
                "first_place": 828, "pick_rate": 0.127618,
                "ban_rate": 0.0723871, "tier": 3, "rank": 50}},
            # Never played this patch: a rank for it would be an invention.
            {"id": 999, "average_stats": {"play": 0, "tier": None, "rank": None}},
        ],
    }

    def decode(self):
        opgg = OPGG()
        opgg._get = lambda *a, **k: self.PAYLOAD          # no network
        return opgg.champions()

    def test_drops_champions_with_no_games(self):
        out = self.decode()
        self.assertEqual([c["championId"] for c in out["champions"]], [86, 54])

    def test_carries_patch_tier_and_rank(self):
        out = self.decode()
        self.assertEqual(out["patch"], "16.17")
        garen = out["champions"][0]
        self.assertEqual((garen["tier"], garen["rank"]), (2, 34))
        self.assertAlmostEqual(garen["banRate"], 5.9, places=1)
        self.assertAlmostEqual(garen["avgFinish"], 3.39, places=2)

    def test_sorted_by_rank(self):
        out = self.decode()
        ranks = [c["rank"] for c in out["champions"]]
        self.assertEqual(ranks, sorted(ranks))


class AugmentTier(unittest.TestCase):
    """The tier rule: average finish against its own rarity, gated on games."""

    def rate(self, rows):
        return {r["id"]: r for r in Guide.rate_augments(rows, "prismatic")}

    @staticmethod
    def row(ident, matches, finish):
        return {"id": ident, "matches": matches, "avgFinish": finish,
                "firstRate": 0.0, "pickRate": 0.0}

    def test_baseline_is_the_pick_weighted_mean_of_the_rarity(self):
        rows = [self.row("a", 300, 3.0), self.row("b", 100, 3.8)]
        out = self.rate(rows)
        # (300*3.0 + 100*3.8) / 400 = 3.2
        self.assertAlmostEqual(out["a"]["baseline"], 3.2, places=2)
        self.assertAlmostEqual(out["b"]["baseline"], 3.2, places=2)

    def test_below_the_gate_there_is_no_letter(self):
        rows = [self.row("thin", TIER_GATE - 1, 2.0),
                self.row("bulk", 5000, 3.5)]
        out = self.rate(rows)
        self.assertIsNone(out["thin"]["tier"])
        self.assertIsNotNone(out["bulk"]["tier"])

    def test_bands_fall_on_the_stated_z_scores(self):
        """Each band floor, probed from just above and just below.

        A row placed exactly on a floor is not worth asserting: the baseline
        is a weighted mean and lands a rounding error either side of the
        nominal one. What matters is that crossing a floor changes the letter
        and nothing else does, so each probe sits a clear margin from it.
        """
        import math

        # A filler large enough that the probes cannot move the baseline more
        # than a rounding error, and a margin far larger than that error.
        base, games, margin = 3.30, 400, 0.05
        spread = FINISH_SD / math.sqrt(games)
        floors = list(TIER_BANDS) + [(None, "C")]

        for i, (floor, letter) in enumerate(floors):
            above = floor + margin if floor is not None else -9.0
            rows = [self.row("filler", 5_000_000, base),
                    self.row("probe", games, base - above * spread)]
            self.assertEqual(self.rate(rows)["probe"]["tier"], letter,
                             f"z={above:+.2f} should be {letter}")

            # Just under this floor is the next band down.
            if floor is not None:
                below = floor - margin
                rows = [self.row("filler", 5_000_000, base),
                        self.row("probe", games, base - below * spread)]
                self.assertEqual(self.rate(rows)["probe"]["tier"],
                                 floors[i + 1][1],
                                 f"z={below:+.2f} should be {floors[i + 1][1]}")

    def test_the_gate_is_on_this_augment_not_the_pool(self):
        """A thin row beside a huge one still gets no letter."""
        rows = [self.row("filler", 5_000_000, 3.30),
                self.row("probe", TIER_GATE, 2.5),
                self.row("under", TIER_GATE - 1, 2.5)]
        out = self.rate(rows)
        self.assertIsNotNone(out["probe"]["tier"])
        self.assertIsNone(out["under"]["tier"])

    def test_lower_average_finish_is_better(self):
        rows = [self.row("good", 500, 2.9), self.row("bad", 500, 3.7),
                self.row("filler", 4000, 3.3)]
        out = self.rate(rows)
        order = [r["id"] for r in Guide.rate_augments(rows, "prismatic")]
        self.assertLess(order.index("good"), order.index("bad"))
        self.assertGreater(out["good"]["avgFinish"], 0)

    def test_popular_and_bad_is_not_rescued_by_its_pick_rate(self):
        """The whole reason to show a tier: Dreadbringer is the joint second
        most picked prismatic and still places badly."""
        rows = [
            {"id": "popular-bad", "matches": 900, "avgFinish": 3.59,
             "firstRate": 15.0, "pickRate": 6.8},
            {"id": "rare-good", "matches": 300, "avgFinish": 2.96,
             "firstRate": 29.9, "pickRate": 2.7},
            {"id": "filler", "matches": 4000, "avgFinish": 3.27,
             "firstRate": 18.0, "pickRate": 40.0},
        ]
        order = [r["id"] for r in Guide.rate_augments(rows, "prismatic")]
        self.assertLess(order.index("rare-good"), order.index("popular-bad"))

    def test_untiered_rows_sort_last(self):
        rows = [self.row("thin", 10, 1.5), self.row("solid", 4000, 3.3)]
        order = [r["id"] for r in Guide.rate_augments(rows, "prismatic")]
        self.assertEqual(order[-1], "thin")

    def test_rarity_is_stamped_on_every_row(self):
        out = Guide.rate_augments([self.row("a", 100, 3.2)], "gold")
        self.assertEqual(out[0]["rarity"], "gold")


class MayhemAugments(unittest.TestCase):
    """u.gg's Mayhem ranking, joined to the client's own augment table.

    Shapes taken from the real files for patch 16_17: the champion file and
    the by-rarity file are both ``{tiers: {band: [id, ...]}}`` dictionaries,
    so band order is this module's and not JSON's, and both are keyed by the
    same augment ids the client publishes in `cherry-augments.json` -- which
    is the join the whole page rests on.
    """

    #: Ahri's file, cut down. Order within a band is u.gg's ranking.
    MINE = {"tiers": {"A": [1113, 1129], "S+": [2132, 1030], "S": [1238]},
            "lastUpdated": "2026-09-06T18:40:24+00:00"}
    #: The whole mode, which ranks augments this champion has too few games
    #: for. 1030 is in both, at different bands, and 1099 is in neither.
    POOL = {"rarities": {
        "kPrismatic": {"tiers": {"S+": [1030], "B": [9001]}},
        "kGold": {"tiers": {"S+": [2132], "D": [9002]}},
    }}

    NAMES = {2132: ("Warlock Juicebox", "gold"), 1030: ("Eureka", "prismatic"),
             1238: ("Blade Waltz", "prismatic"), 1113: ("Mercy", "silver"),
             1129: ("Scavenger", "gold"), 9001: ("Wildfire", "prismatic")}

    class FakeGameData:
        def __init__(self, names):
            self.names = names

        def augment(self, augment_id):
            row = self.names.get(int(augment_id))
            if not row:
                return None
            return {"id": augment_id, "name": row[0],
                    "icon": f"/icons/{augment_id}.png", "rarity": row[1]}

        def champion_alias(self, champion_id):
            return "Ahri"

    def guide(self, pool=POOL):
        ugg = UGG()
        ugg.mayhem_augments = lambda cid, patch=None: {
            "patch": "16_17", **self.MINE}

        def whole(patch=None):
            if pool is None:
                raise Unavailable("no pool")
            return {"patch": "16_17", **pool}

        ugg.mayhem_augment_pool = whole
        return Guide(self.FakeGameData(self.NAMES), ugg)

    def rows(self, **kw):
        return self.guide(**kw).mayhem_augments(103)["augments"]

    def test_bands_are_ordered_best_first_not_as_the_file_lists_them(self):
        """The file is a dictionary. Trusting its key order puts A above S+."""
        self.assertEqual([r["tier"] for r in self.rows()][:5],
                         ["S+", "S+", "S", "A", "A"])

    def test_the_champion_s_own_ranking_wins_over_the_mode_s(self):
        """1030 is S+ for this champion and S+ for everyone; 1113 is A here.

        The champion's row is the answer to the question actually being
        asked, so a pool entry never overwrites or duplicates one.
        """
        rows = {r["id"]: r for r in self.rows()}
        self.assertEqual(len(self.rows()), len({r["id"] for r in self.rows()}))
        self.assertTrue(rows[1030]["forChampion"])
        self.assertTrue(rows[2132]["forChampion"])

    def test_the_pool_fills_in_what_the_champion_file_omits(self):
        rows = {r["id"]: r for r in self.rows()}
        self.assertIn(9001, rows)
        self.assertEqual(rows[9001]["tier"], "B")
        self.assertFalse(rows[9001]["forChampion"])

    def test_a_pool_row_sorts_under_the_champion_s_within_its_band(self):
        """Same letter, different question, and the page says which is which
        by putting this champion's rows first rather than blending them."""
        top = [(r["id"], r["forChampion"]) for r in self.rows()
               if r["tier"] == "S+"]
        self.assertEqual([mine for _, mine in top], sorted(
            [mine for _, mine in top], reverse=True))

    def test_names_and_icons_come_from_the_client(self):
        rows = {r["id"]: r for r in self.rows()}
        self.assertEqual(rows[2132]["name"], "Warlock Juicebox")
        self.assertEqual(rows[2132]["icon"], "/icons/2132.png")
        self.assertEqual(rows[2132]["rarity"], "gold")

    def test_an_augment_the_client_does_not_know_keeps_u_gg_s_rarity(self):
        """A new augment reaches u.gg before a stale cached client file has
        it, and a row with no rarity has no ring to draw."""
        rows = {r["id"]: r for r in self.rows()}
        self.assertEqual(rows[9002]["rarity"], "gold")
        self.assertEqual(rows[9002]["name"], "")

    def test_the_page_survives_the_pool_being_unavailable(self):
        """The pool is the smaller half of the answer, so it never takes the
        champion's own ranking down with it."""
        rows = self.rows(pool=None)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["forChampion"] for r in rows))


class TabDerivation(unittest.TestCase):
    """Which tabs a mode earns, derived from what it actually has.

    Pinned because the alternative is five hard-coded places, which is what
    this replaced.
    """

    def tabs(self, game_mode, map_id, queue_id=None):
        return [t["key"] for t in modes.resolve(game_mode, map_id, queue_id).tabs]

    def test_rift_has_build_and_counters(self):
        self.assertEqual(self.tabs("CLASSIC", 11, 420),
                         ["skin", "build", "counters"])

    def test_swiftplay_and_urf_match_rift(self):
        self.assertEqual(self.tabs("SWIFTPLAY", 11, 480),
                         ["skin", "build", "counters"])
        self.assertEqual(self.tabs("URF", 11, 1900),
                         ["skin", "build", "counters"])

    def test_aram_has_no_counters(self):
        self.assertEqual(self.tabs("ARAM", 12, 450), ["skin", "build"])

    def test_mayhem_is_aram_plus_its_augments(self):
        """Mayhem borrows ARAM's build and adds the page ARAM has no use for.

        The augment tab is beside the build, not instead of it, because the
        items are still worth reading -- which is the difference between this
        mode and Arena, where the build sheet has nothing to draw.
        """
        self.assertEqual(self.tabs("KIWI", 12, 3270),
                         ["skin", "build", "augments"])

    def test_mayhem_picks_no_runes(self):
        """The one thing a borrowed ARAM build must not carry through.

        ARAM's u.gg build has a rune page; Mayhem does not let you choose
        one. Drawing it, or importing it, would offer a choice the game does
        not have.
        """
        mayhem = modes.resolve("KIWI", 12, 3270)
        self.assertFalse(mayhem.runes)
        self.assertTrue(mayhem.augments)
        self.assertTrue(mayhem.borrowed)
        self.assertEqual(mayhem.queue, modes.UGG_ARAM)
        block = modes.payload(mayhem, 3270, "KIWI", 12, "ARAM: Mayhem")
        self.assertFalse(block["runes"])
        self.assertTrue(block["augments"])

    def test_every_other_mode_still_picks_runes(self):
        for game_mode, map_id, queue_id in (("CLASSIC", 11, 420),
                                            ("ARAM", 12, 450),
                                            ("SWIFTPLAY", 11, 480),
                                            ("URF", 11, 1900)):
            mode = modes.resolve(game_mode, map_id, queue_id)
            self.assertTrue(mode.runes, mode.key)
            self.assertFalse(mode.augments, mode.key)

    def test_arena_splits_its_pages(self):
        self.assertEqual(self.tabs("CHERRY", 30, 1750),
                         ["skin", "tiers", "augments", "items"])

    def test_modes_with_no_data_show_only_skins(self):
        self.assertEqual(self.tabs("NEXUSBLITZ", 21, 1300), ["skin"])
        self.assertEqual(self.tabs("TFT", 22, 1090), ["skin"])
        self.assertEqual(self.tabs("SOMETHING_NEW", 999), ["skin"])

    def test_every_tab_is_labelled(self):
        for game_mode, map_id in (("CLASSIC", 11), ("ARAM", 12),
                                  ("CHERRY", 30), ("NEXUSBLITZ", 21)):
            for tab in modes.resolve(game_mode, map_id).tabs:
                self.assertTrue(tab["label"], tab)
                self.assertEqual(tab["label"], modes.TAB_LABELS[tab["key"]])

    def test_the_payload_carries_the_tabs(self):
        """The client and the mock both build the queue block through this,
        so a mode cannot have one tab set under test and another in a game."""
        arena = modes.resolve("CHERRY", 30, 1750)
        block = modes.payload(arena, 1750, "CHERRY", 30, "Arena")
        self.assertEqual([t["key"] for t in block["tabs"]],
                         ["skin", "tiers", "augments", "items"])
        self.assertEqual(block["kind"], "arena")
        self.assertEqual(block["source"], "opgg")
        self.assertFalse(block["roles"])


if __name__ == "__main__":
    unittest.main()
