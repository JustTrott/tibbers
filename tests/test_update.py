#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
When the app may swap itself out.

The automatic install quits and reopens the app. Doing that in champ select
takes the picker away at the one moment it is wanted, and doing it in a game
takes away the build; the rule that decides is small, so it is pinned here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import update  # noqa: E402


class Idle(unittest.TestCase):

    def test_no_client_is_idle(self):
        self.assertTrue(update.league_idle(None, False))
        self.assertTrue(update.league_idle("None", False))

    def test_the_lobby_is_idle(self):
        self.assertTrue(update.league_idle("Lobby", False))

    def test_champ_select_is_not(self):
        self.assertFalse(update.league_idle("ChampSelect", False))

    def test_a_queue_is_not(self):
        # A pop mid-swap would open champ select with no picker.
        self.assertFalse(update.league_idle("Matchmaking", False))
        self.assertFalse(update.league_idle("ReadyCheck", False))

    def test_a_running_game_is_not_whatever_the_client_says(self):
        self.assertFalse(update.league_idle("InProgress", True))
        self.assertFalse(update.league_idle(None, True))


class Versions(unittest.TestCase):

    def test_newer_means_numerically_newer(self):
        self.assertTrue(update._version_tuple("1.0.0") > update._version_tuple("0.1.1"))
        self.assertTrue(update._version_tuple("0.1.10") > update._version_tuple("0.1.9"))

    def test_a_tag_prefix_is_ignored(self):
        self.assertEqual(update._version_tuple("v1.2.3"), (1, 2, 3))


class Schedule(unittest.TestCase):
    """The intervals are what keeps the check inside GitHub's rate limit."""

    def test_settings_rechecks_far_less_often_than_the_page_polls(self):
        self.assertGreaterEqual(update.SETTINGS_RECHECK, 60)

    def test_the_scheduled_check_is_hours_apart(self):
        self.assertGreaterEqual(update.CHECK_INTERVAL, 60 * 60)



class WindowsSwap(unittest.TestCase):
    """The .bat that replaces the install: what it must and must not do."""

    def script(self) -> str:
        path = update._swap_script_windows(
            Path(r"C:\tmp\unpacked\Tibbers"), Path(r"C:\Programs\Tibbers"),
            Path(r"C:\data\work\update.log"))
        try:
            return path.read_text()
        finally:
            path.unlink()

    def robocopies(self):
        return [line for line in self.script().splitlines()
                if line.startswith("robocopy")]

    def test_robocopy_gives_up_rather_than_retrying_for_days(self):
        # Its default is a million retries thirty seconds apart, and a locked
        # Tibbers.exe -- any running copy, the patcher holder included --
        # held the swap forever.
        for line in self.robocopies():
            self.assertIn("/R:", line)
            self.assertIn("/W:", line)

    def test_the_exe_is_copied_first_and_alone(self):
        first, rest = self.robocopies()
        self.assertIn("Tibbers.exe", first)
        self.assertNotIn("/MIR", first)
        self.assertIn("/MIR", rest)

    def test_a_failed_copy_reopens_nothing(self):
        s = self.script()
        self.assertLess(s.index("errorlevel 8"), s.index('start ""'))

    def test_no_second_copy_is_opened(self):
        self.assertIn("imagename eq Tibbers.exe", self.script())

    def test_what_happened_is_logged(self):
        self.assertIn(r"C:\data\work\update.log", self.script())

    def test_the_swap_runs_without_a_window(self):
        # DETACHED_PROCESS makes Windows ignore CREATE_NO_WINDOW, which is how
        # the update's terminal came to sit over the desktop.
        self.assertFalse(update._SWAP_FLAGS & 0x00000008)
        self.assertTrue(update._SWAP_FLAGS & 0x08000000)


if __name__ == "__main__":
    unittest.main()
