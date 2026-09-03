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
import unittest.mock
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



class WindowsInstall(unittest.TestCase):
    """On Windows the update is the installer, run unattended: no script of
    ours, no console, and the installer relaunches the app."""

    def test_the_windows_asset_is_the_installer(self):
        with unittest.mock.patch.object(update, "_IS_WINDOWS", True):
            self.assertEqual(update.asset_name(), "Tibbers-windows-setup.exe")
        with unittest.mock.patch.object(update, "_IS_WINDOWS", False):
            self.assertEqual(update.asset_name(), "Tibbers.zip")

    def test_the_installer_runs_unattended_and_reopens_the_app(self):
        cmd = update.installer_command(Path(r"C:\t\Tibbers-windows-setup.exe"),
                                       Path(r"C:\d a\work\update.log"))
        self.assertTrue(cmd.startswith('"C:\\t\\Tibbers-windows-setup.exe" '))
        for switch in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                       "/NOCANCEL", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"):
            self.assertIn(f" {switch}", cmd)
        # Inno wants the quotes around the value, and the path has a space.
        self.assertIn('/LOG="C:\\d a\\work\\update.log"', cmd)
        self.assertNotIn("cmd", cmd.lower().replace("tibbers-windows-setup", ""))

    def test_no_script_is_written_any_more(self):
        self.assertFalse(hasattr(update, "_swap_script_windows"))


class Digest(unittest.TestCase):
    """A download is checked against the checksum the release publishes."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "asset.bin"
        self.file.write_bytes(b"tibbers" * 1000)
        import hashlib
        self.good = "sha256:" + hashlib.sha256(self.file.read_bytes()).hexdigest()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_matching_digest_passes(self):
        update.verify_digest(self.file, self.good)
        update.verify_digest(self.file, self.good.upper())

    def test_a_wrong_digest_is_refused(self):
        with self.assertRaises(RuntimeError):
            update.verify_digest(self.file, "sha256:" + "0" * 64)

    def test_no_digest_or_an_unknown_kind_is_not_checked(self):
        update.verify_digest(self.file, None)
        update.verify_digest(self.file, "md5:abc")

    def test_check_carries_the_digest_to_stage(self):
        rel = {"tag": "v9.9.9", "version": "9.9.9", "url": "u",
               "digest": self.good, "name": "n", "notes": ""}
        with unittest.mock.patch.object(update, "latest_release", return_value=rel):
            result = update.check(current="1.0.0")
        self.assertTrue(result["available"])
        self.assertEqual(result["digest"], self.good)


if __name__ == "__main__":
    unittest.main()
