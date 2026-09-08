#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Starting tibbers when you sign in.

The state is on the machine -- a registry value, a shortcut, a LaunchAgent --
so these drive the real code against a redirected HOME and a temporary
%APPDATA% rather than mocking the module's own functions.
"""

from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import autostart  # noqa: E402


class Supported(unittest.TestCase):

    def test_a_checkout_has_nothing_to_point_at(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(autostart.executable())
            self.assertFalse(autostart.supported())
            self.assertFalse(autostart.enabled())

    def test_turning_it_on_from_a_checkout_does_nothing(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertFalse(autostart.set_enabled(True))

    def test_a_portable_copy_does_not_offer_it(self):
        """A portable app writing to the registry is not portable, and the
        entry would outlive a folder that can be unplugged."""
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.dict("os.environ", {"TIBBERS_PORTABLE": "1"}):
            self.assertFalse(autostart.supported())
            self.assertFalse(autostart.enabled())


class SharedName(unittest.TestCase):
    """The installer writes the shortcut; this module has to find it."""

    def test_the_shortcut_is_named_after_the_installer_s_app(self):
        iss = (Path(__file__).resolve().parent.parent / "scripts"
               / "tibbers.iss").read_text(encoding="utf-8")
        self.assertIn('#define MyAppName "Tibbers"', iss)
        self.assertEqual(autostart.APP_NAME, "Tibbers")
        self.assertIn(r'Name: "{userstartup}\{#MyAppName}"', iss)

    def test_a_self_update_leaves_the_shortcut_alone(self):
        """Inno remembers the task from the first install, so without this an
        update would put the shortcut back for someone who turned it off."""
        iss = (Path(__file__).resolve().parent.parent / "scripts"
               / "tibbers.iss").read_text(encoding="utf-8")
        line = next(l for l in iss.splitlines()
                    if l.startswith('Name: "{userstartup}'))
        self.assertIn("Check: not WantsRelaunch", line)


@unittest.skipUnless(sys.platform == "darwin", "a LaunchAgent")
class MacAgent(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patch = mock.patch.object(Path, "home",
                                  staticmethod(lambda: Path(self.home.name)))
        patch.start()
        self.addCleanup(patch.stop)
        self.exe = Path(self.home.name) / "Tibbers.app/Contents/MacOS/Tibbers"
        patch2 = mock.patch.object(autostart, "executable",
                                   return_value=self.exe)
        patch2.start()
        self.addCleanup(patch2.stop)

    def test_it_starts_off_and_can_be_turned_on_and_off(self):
        self.assertFalse(autostart.enabled())
        self.assertTrue(autostart.set_enabled(True))
        self.assertTrue(autostart.enabled())
        self.assertFalse(autostart.set_enabled(False))
        self.assertFalse(autostart.enabled())

    def test_the_agent_names_the_binary_and_runs_it_quiet(self):
        autostart.set_enabled(True)
        with open(autostart._agent_path(), "rb") as f:
            plist = plistlib.load(f)
        self.assertEqual(plist["ProgramArguments"], [str(self.exe), "--quiet"])
        self.assertTrue(plist["RunAtLoad"])
        # Quitting from the menu bar has to mean quitting.
        self.assertFalse(plist["KeepAlive"])

    def test_turning_it_off_twice_is_not_an_error(self):
        autostart.set_enabled(False)
        self.assertFalse(autostart.set_enabled(False))


@unittest.skipUnless(sys.platform.startswith("win"), "a Run key")
class WindowsRunKey(unittest.TestCase):
    """Against the real registry, under a test-only value name so nothing
    the user actually has is read or written."""

    def setUp(self):
        self.appdata = tempfile.TemporaryDirectory()
        self.addCleanup(self.appdata.cleanup)
        self.exe = Path(self.appdata.name) / "Tibbers.exe"
        for target, new in (("APP_NAME", "TibbersAutostartTest"),):
            patch = mock.patch.object(autostart, target, new)
            patch.start()
            self.addCleanup(patch.stop)
        patch2 = mock.patch.object(autostart, "executable", return_value=self.exe)
        patch2.start()
        self.addCleanup(patch2.stop)
        # A Startup folder of our own, so the real one is never touched.
        self.startup = Path(self.appdata.name) / "Startup"
        self.startup.mkdir()
        patch3 = mock.patch.object(autostart, "_startup_shortcut",
                                   return_value=self.startup / "Tibbers.lnk")
        patch3.start()
        self.addCleanup(patch3.stop)
        self.addCleanup(autostart._windows_disable)

    def test_it_starts_off_and_can_be_turned_on_and_off(self):
        autostart._windows_disable()
        self.assertFalse(autostart.enabled())
        self.assertTrue(autostart.set_enabled(True))
        self.assertFalse(autostart.set_enabled(False))
        self.assertFalse(autostart.enabled())

    def test_the_run_value_quotes_the_exe_and_asks_for_a_quiet_start(self):
        autostart.set_enabled(True)
        self.assertEqual(autostart._run_value(), f'"{self.exe}" --quiet')

    def test_the_installer_s_shortcut_alone_counts_as_on(self):
        autostart._windows_disable()
        (self.startup / "Tibbers.lnk").write_bytes(b"")
        self.assertTrue(autostart.enabled())

    def test_turning_it_on_drops_the_installer_s_shortcut(self):
        """Two mechanisms would start two copies, and the instance mutex would
        turn the second into a puzzling error at every sign-in."""
        (self.startup / "Tibbers.lnk").write_bytes(b"")
        autostart.set_enabled(True)
        self.assertFalse((self.startup / "Tibbers.lnk").exists())
        self.assertTrue(autostart.enabled())

    def test_turning_it_off_removes_both(self):
        autostart.set_enabled(True)
        (self.startup / "Tibbers.lnk").write_bytes(b"")
        autostart.set_enabled(False)
        self.assertIsNone(autostart._run_value())
        self.assertFalse((self.startup / "Tibbers.lnk").exists())


if __name__ == "__main__":
    unittest.main()
