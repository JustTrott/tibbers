#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The lookups on the champ-select poll path.

`lockfile_path` is asked several times a second for as long as the League
client is closed, and answering it used to walk every process on the machine
twice. Nothing about the answer is visible in the UI, so the only thing that
can pin the cost is a test that counts the scans.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# These exercise the macOS implementation directly -- they patch and read
# module-internal names (_look_for_install, _DATA_DIRS) that the platform
# dispatcher in `system` deliberately does not re-export.
from tibbers import _system_macos as system  # noqa: E402


class InstallLookup(unittest.TestCase):
    """`find_install` scans once, not once per call."""

    def setUp(self):
        self.real = system._look_for_install
        self.reset()
        self.addCleanup(self.restore)

    def reset(self):
        system._INSTALL = None
        system._INSTALL_AT = 0.0

    def restore(self):
        system._look_for_install = self.real
        self.reset()

    def scanner(self, answer):
        calls = []

        def scan():
            calls.append(1)
            return answer

        system._look_for_install = scan
        return calls

    def test_a_hit_is_scanned_for_once(self):
        game, client = Path("/game"), Path("/client")
        calls = self.scanner((game, client))
        for _ in range(20):
            self.assertEqual(system.find_install(), (game, client))
        self.assertEqual(len(calls), 1)

    def test_a_miss_is_retried_rather_than_pinned(self):
        """League is normally started *after* this app, so 'not found' has to
        stop being the answer once it is running."""
        calls = self.scanner((None, None))
        system.find_install()
        system.find_install()
        self.assertEqual(len(calls), 1, "not re-scanned within the window")

        system._INSTALL_AT -= system._INSTALL_MISS_TTL + 1
        system.find_install()
        self.assertEqual(len(calls), 2, "re-scanned once the window passed")

    def test_lockfile_path_is_none_without_one(self):
        self.scanner((None, None))
        # No client dir and no standard root holding a lockfile on a test
        # machine; the point is that it answers rather than raising.
        self.assertIn(system.lockfile_path(), (None, *[
            root / "lockfile" for root in system.INSTALL_ROOTS]))


class ProcessMatching(unittest.TestCase):

    def test_several_names_are_matched_in_one_sweep(self):
        """The sweep is the cost, so both client names go through one pass."""
        seen = []
        real = system.psutil.process_iter

        def counting(attrs=None):
            seen.append(1)
            return real(attrs)

        system.psutil.process_iter = counting
        try:
            system.find_processes(system.CLIENT_PROCESS,
                                  system.CLIENT_UX_PROCESS)
        finally:
            system.psutil.process_iter = real
        self.assertEqual(len(seen), 1)

    def test_this_very_process_is_found_by_its_own_name(self):
        """Matching is on the executable's basename as psutil reports it --
        which is the resolved binary, not the symlink that launched us."""
        me = system.psutil.Process(os.getpid())
        pids = [p.pid for p in system.find_processes(
            os.path.basename(me.exe()))]
        self.assertIn(os.getpid(), pids)


class DataDir(unittest.TestCase):
    """One mkdir per home, not one per caller."""

    def setUp(self):
        self.before = os.environ.get("TIBBERS_HOME")
        self.saved = dict(system._DATA_DIRS)
        self.addCleanup(self.restore)

    def restore(self):
        if self.before is None:
            os.environ.pop("TIBBERS_HOME", None)
        else:
            os.environ["TIBBERS_HOME"] = self.before
        system._DATA_DIRS.clear()
        system._DATA_DIRS.update(self.saved)

    def test_the_override_is_created_and_then_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            os.environ["TIBBERS_HOME"] = str(home)
            system._DATA_DIRS.clear()
            self.assertEqual(system.data_dir(), home)
            self.assertTrue(home.is_dir())
            # Removed underneath: a cached answer is not re-created, which is
            # the whole point -- but it is still the same path.
            self.assertEqual(system.data_dir(), home)
            self.assertEqual(list(system._DATA_DIRS), [str(home)])

    def test_a_changed_home_gets_its_own_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "one", Path(tmp) / "two"
            system._DATA_DIRS.clear()
            os.environ["TIBBERS_HOME"] = str(first)
            self.assertEqual(system.data_dir(), first)
            os.environ["TIBBERS_HOME"] = str(second)
            self.assertEqual(system.data_dir(), second)
            self.assertTrue(first.is_dir() and second.is_dir())


if __name__ == "__main__":
    unittest.main()
