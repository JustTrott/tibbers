#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The champ-select session, and the memo in front of the guide fetch.

The memo is what stops switching between the enemies in one champ select
refetching the build over the network, and it is bounded so a long evening
does not accumulate every guide it has ever shown.
"""

from __future__ import annotations

import sys
import unittest
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


class Memo(unittest.TestCase):

    def test_it_returns_what_it_stored(self):
        memo = OrderedDict()
        self.assertEqual(main.memoise(memo, "k", {"a": 1}, 4), {"a": 1})
        self.assertEqual(memo["k"], {"a": 1})

    def test_the_oldest_go_first(self):
        memo = OrderedDict()
        for i in range(10):
            main.memoise(memo, i, i, 4)
        self.assertEqual(list(memo), [6, 7, 8, 9])

    def test_a_key_stored_again_is_not_a_second_entry(self):
        memo = OrderedDict()
        main.memoise(memo, "k", 1, 4)
        main.memoise(memo, "k", 2, 4)
        self.assertEqual(list(memo), ["k"])
        self.assertEqual(memo["k"], 2)

    def test_the_two_bounds_are_the_ones_the_guide_uses(self):
        self.assertEqual((main.GUIDE_MEMO_MAX, main.SHARED_MEMO_MAX), (8, 4))


class SessionDefaults(unittest.TestCase):
    """Two instances must not share the memos."""

    def test_each_session_gets_its_own_memos(self):
        one, two = main.Session(), main.Session()
        one.guide_memo["k"] = 1
        one.shared_memo["k"] = 1
        self.assertEqual(two.guide_memo, {})
        self.assertEqual(two.shared_memo, {})

    def test_it_starts_with_nothing_selected(self):
        session = main.Session()
        self.assertIsNone(session.selected)
        self.assertIsNone(session.chroma)
        self.assertIsNone(session.champion_id)
        self.assertIsNone(session.auto_imported)
        self.assertIsNone(session.guide_key)
        self.assertFalse(session.was_locked)
        self.assertFalse(session.was_in_select)
        self.assertFalse(session.opponent_by_hand)
        self.assertEqual(session.guide_generation, 0)

    def test_a_mistyped_field_is_an_error_rather_than_a_new_one(self):
        """The whole reason this is not a dict: the old one grew a key and
        carried on."""
        session = main.Session()
        with self.assertRaises(AttributeError):
            session.was_lockd = True


if __name__ == "__main__":
    unittest.main()
