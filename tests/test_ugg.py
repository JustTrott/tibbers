#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The matchup table: what it decodes to, and how often.

Champ select asks for the same table three times on every refresh -- the
counters page, the row against the team actually in the game, and the lane
opponent nomination -- so the memo in front of it is load-bearing on a path
that runs while a 30-second timer is going.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import ugg  # noqa: E402
from tibbers.ugg import EMERALD_PLUS, OVERALL, UGG, WORLD  # noqa: E402

#: A matchups cell, in the shape the file actually stores: the rows are
#: wrapped alongside a last-updated stamp, and each row is
#: [championId, losses, matches, xp, gold, ?, cs] with the diffs negated.
ROWS = [
    [51, 16374, 35023, -2200.0, -6300.0, 0, -420.0],   # Caitlyn, common
    [22, 900, 1800, 400.0, 900.0, 0, 60.0],            # Ashe, even
    [17, 3, 5, 0.0, 0.0, 0, 0.0],                      # Teemo, noise
]


def payload(rows=None, tier=EMERALD_PLUS, role=3):
    return {str(WORLD): {str(tier): {str(role): [rows or ROWS, 1700000000]}}}


class Stubbed(UGG):
    """A UGG that answers from a dict instead of the network."""

    def __init__(self, body=None):
        super().__init__()
        self.body = payload() if body is None else body
        self.fetches = []

    def resolve_patch(self, patch=None):
        return patch or "16_16"

    def _url(self, endpoint, tail, queue, patch=None):
        return f"stub://{endpoint}/{tail}/{queue}/{patch}"

    def _get(self, url, key, ttl=0):
        self.fetches.append(key)
        return self.body


class Decoding(unittest.TestCase):

    def test_rows_carry_the_opponent_s_own_figures(self):
        table = Stubbed().matchup_table(222, "bottom")
        by_id = {r["championId"]: r for r in table}
        self.assertEqual(sorted(by_id), [17, 22, 51])
        caitlyn = by_id[51]
        self.assertEqual(caitlyn["matches"], 35023)
        # 16374 of 35023 are losses, so this champion wins 53.25%.
        self.assertAlmostEqual(caitlyn["winRate"], 53.25, places=2)
        # The diffs are stored negated, per opponent per game.
        self.assertAlmostEqual(caitlyn["goldAt15"], 0.2, places=1)
        self.assertAlmostEqual(caitlyn["csAt15"], 0.0, places=1)

    def test_share_is_of_this_champion_s_own_games(self):
        table = Stubbed().matchup_table(222, "bottom")
        total = sum(r["matches"] for r in table)
        for row in table:
            self.assertAlmostEqual(row["share"], row["matches"] / total,
                                   places=6)
        self.assertAlmostEqual(sum(r["share"] for r in table), 1.0, places=6)

    def test_a_role_with_no_cell_is_empty_not_an_error(self):
        self.assertEqual(Stubbed().matchup_table(222, "jungle"), [])

    def test_opponent_samples_reads_the_same_table(self):
        client = Stubbed()
        self.assertEqual(client.opponent_samples(222, "bottom"),
                         {51: 35023, 22: 1800, 17: 5})


class TableMemo(unittest.TestCase):

    def test_the_same_question_is_decoded_once(self):
        client = Stubbed()
        first = client.matchup_table(222, "bottom")
        for _ in range(5):
            self.assertIs(client.matchup_table(222, "bottom"), first)
        client.opponent_samples(222, "bottom")
        self.assertEqual(len(client.fetches), 1)

    def test_everything_that_changes_the_answer_is_in_the_key(self):
        client = Stubbed()
        client.matchup_table(222, "bottom")
        client.matchup_table(222, "top")
        client.matchup_table(51, "bottom")
        client.matchup_table(222, "bottom", queue="normal_aram")
        client.matchup_table(222, "bottom", patch="16_15")
        client.matchup_table(222, "bottom", tiers=(OVERALL,))
        self.assertEqual(len(client.fetches), 6)
        self.assertEqual(len(client._tables), 6)

    def test_an_empty_answer_is_memoised_too(self):
        """Otherwise the mode with no data re-fetches on every refresh."""
        client = Stubbed()
        client.matchup_table(222, "jungle")
        client.matchup_table(222, "jungle")
        self.assertEqual(len(client.fetches), 1)

    def test_the_memo_stays_within_its_bound(self):
        client = Stubbed()
        for champion in range(ugg.TABLE_MEMO_MAX + 5):
            client.matchup_table(champion, "bottom")
        self.assertEqual(len(client._tables), ugg.TABLE_MEMO_MAX)


class PayloadCache(unittest.TestCase):

    def test_the_memory_cache_stays_within_its_bound(self):
        """Every entry is on disk as well, so an eviction costs a file read."""
        client = UGG()
        for i in range(ugg.MEMORY_MAX + 10):
            client._remember(f"key-{i}", {"at": 0, "data": i})
        self.assertEqual(len(client._memory), ugg.MEMORY_MAX)
        self.assertNotIn("key-0", client._memory)
        self.assertIn(f"key-{ugg.MEMORY_MAX + 9}", client._memory)

    def test_reuse_keeps_an_entry_alive(self):
        client = UGG()
        client._remember("kept", {"at": 0, "data": "x"})
        for i in range(ugg.MEMORY_MAX - 1):
            client._remember(f"filler-{i}", {"at": 0, "data": i})
        client._remember("kept", {"at": 0, "data": "x"})   # touched again
        client._remember("one-more", {"at": 0, "data": "y"})
        self.assertIn("kept", client._memory)


if __name__ == "__main__":
    unittest.main()
