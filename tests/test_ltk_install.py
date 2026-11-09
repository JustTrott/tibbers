#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Installing LTK Manager from the app: only LTK's own, only signed.

The button downloads and runs another program's setup, so what it agrees to
fetch and run is the part worth pinning down. Nothing here touches the
network or starts a process.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import ltk_install  # noqa: E402


class SetupUrl(unittest.TestCase):

    def answer(self, assets):
        body = json.dumps({"assets": assets}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(body)
        return mock.patch.object(ltk_install.urllib.request, "urlopen",
                                 return_value=response)

    def test_the_x64_setup_of_the_latest_release_is_picked(self):
        url = ltk_install.ALLOWED + "download/v1.33.0/LTK.Manager_1.33.0_x64-setup.exe"
        with self.answer([{"browser_download_url": ltk_install.ALLOWED + "x.msi"},
                          {"browser_download_url": url}]):
            self.assertEqual(ltk_install.setup_url(), url)

    def test_an_asset_off_ltks_releases_is_ignored(self):
        with self.answer([{"browser_download_url":
                           "https://example.com/LTK.Manager_x64-setup.exe"}]):
            self.assertEqual(ltk_install.setup_url(), ltk_install.STABLE_SETUP)

    def test_an_unreachable_api_falls_back_to_the_stable_link(self):
        with mock.patch.object(ltk_install.urllib.request, "urlopen",
                               side_effect=OSError("rate limited")):
            self.assertEqual(ltk_install.setup_url(), ltk_install.STABLE_SETUP)

    def test_the_stable_link_is_ltks_own(self):
        self.assertTrue(ltk_install.STABLE_SETUP.startswith(ltk_install.ALLOWED))


class Refusals(unittest.TestCase):

    def test_nothing_is_downloaded_from_anywhere_else(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ltk_install.urllib.request, "urlopen",
                                  side_effect=AssertionError("fetched")):
            with self.assertRaises(ltk_install.InstallError):
                ltk_install.download("https://example.com/setup.exe",
                                     Path(tmp) / "s.exe", lambda *a: None)

    def test_an_unsigned_setup_is_not_run(self):
        from tibbers import diagnostics
        with mock.patch.object(diagnostics, "signature", return_value="unsigned"):
            with self.assertRaises(ltk_install.InstallError) as caught:
                ltk_install.verify(Path("setup.exe"))
        self.assertIn("unsigned", str(caught.exception))

    def test_install_stops_before_running_an_unsigned_setup(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ltk_install, "setup_url",
                                  return_value=ltk_install.STABLE_SETUP), \
                mock.patch.object(ltk_install, "download"), \
                mock.patch.object(ltk_install, "verify",
                                  side_effect=ltk_install.InstallError("bad")), \
                mock.patch.object(ltk_install, "run_setup",
                                  side_effect=AssertionError("ran")):
            with self.assertRaises(ltk_install.InstallError):
                ltk_install.install(Path(tmp), lambda *a: None)

    def test_a_setup_that_closes_early_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ltk_install, "setup_url",
                                  return_value=ltk_install.STABLE_SETUP), \
                mock.patch.object(ltk_install, "download"), \
                mock.patch.object(ltk_install, "verify"), \
                mock.patch.object(ltk_install, "run_setup", return_value=0), \
                mock.patch.object(ltk_install, "wait_installed", return_value=False):
            with self.assertRaises(ltk_install.InstallError):
                ltk_install.install(Path(tmp), lambda *a: None)


class Restart(unittest.TestCase):

    def test_the_new_copy_waits_for_this_one_and_keeps_the_arguments(self):
        import main
        with mock.patch.object(sys, "argv",
                               ["main.py", "--quiet", "--after-pid", "111", "--port", "7790"]):
            command = main._restart_command()
        self.assertEqual(command[-2:], ["--after-pid", str(__import__("os").getpid())])
        self.assertIn("--quiet", command)
        self.assertIn("7790", command)
        self.assertNotIn("111", command)


if __name__ == "__main__":
    unittest.main()
