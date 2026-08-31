#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
When the passwordless helper may be claimed as usable.

Nothing here installs, uninstalls or elevates anything -- the paths are
redirected at a temporary directory and only the decisions are exercised.
Saying "installed" wrongly is the expensive direction: the caller then takes
the passwordless path, the wrapper refuses the mod-tools build it was not
given, and the app reports a failed patcher mid champ select instead of
raising the ordinary prompt.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import privileged  # noqa: E402


class Installed(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        self.saved = {name: getattr(privileged, name) for name in
                      ("WRAPPER", "SUDOERS_FILE", "MODTOOLS_ARM",
                       "MODTOOLS_X86", "rosetta_possible")}
        privileged.WRAPPER = self.tmp / "tibbers-inject"
        privileged.SUDOERS_FILE = self.tmp / "sudoers"
        privileged.MODTOOLS_ARM = self.tmp / "tibbers-mod-tools"
        privileged.MODTOOLS_X86 = self.tmp / "tibbers-mod-tools-x86_64"
        self.addCleanup(self.restore)
        self.rosetta(True)

    def restore(self):
        for name, value in self.saved.items():
            setattr(privileged, name, value)

    def rosetta(self, possible):
        privileged.rosetta_possible = lambda: possible

    def put(self, *names):
        for name in names:
            getattr(privileged, name).write_text("x")

    def test_nothing_installed_is_not_installed(self):
        self.assertFalse(privileged.installed())
        self.assertFalse(privileged.installed("arm64"))
        self.assertFalse(privileged.installed("x86_64"))

    def test_the_wrapper_alone_is_not_enough(self):
        self.put("WRAPPER")
        self.assertFalse(privileged.installed())
        self.put("SUDOERS_FILE")
        self.assertFalse(privileged.installed(), "no mod-tools yet")

    def test_a_native_game_needs_only_the_native_build(self):
        self.put("WRAPPER", "SUDOERS_FILE", "MODTOOLS_ARM")
        self.assertTrue(privileged.installed("arm64"))

    def test_a_translated_game_needs_the_intel_build(self):
        """This is the case that used to exit 69 out of the wrapper."""
        self.put("WRAPPER", "SUDOERS_FILE", "MODTOOLS_ARM")
        self.assertFalse(privileged.installed("x86_64"))
        self.put("MODTOOLS_X86")
        self.assertTrue(privileged.installed("x86_64"))

    def test_without_an_arch_both_are_required_where_rosetta_is_possible(self):
        self.put("WRAPPER", "SUDOERS_FILE", "MODTOOLS_ARM")
        self.assertFalse(privileged.installed(),
                         "cannot know which build will be wanted")
        self.put("MODTOOLS_X86")
        self.assertTrue(privileged.installed())

    def test_on_a_machine_that_cannot_translate_the_native_build_is_enough(self):
        """An Intel Mac runs the game natively; select_modtools never asks
        for the other build there."""
        self.rosetta(False)
        self.put("WRAPPER", "SUDOERS_FILE", "MODTOOLS_ARM")
        self.assertTrue(privileged.installed())

    def test_stopping_does_not_need_a_mod_tools_build_at_all(self):
        """The wrapper's stop mode pkills by name. Refusing it over a missing
        Intel copy would prompt for admin to stop a root patcher the helper
        could have stopped silently."""
        self.put("WRAPPER", "SUDOERS_FILE", "MODTOOLS_ARM")
        self.assertFalse(privileged.installed())
        self.assertTrue(privileged.installed(privileged.NO_BUILD))

    def test_available_is_false_whenever_installed_is(self):
        self.put("WRAPPER", "SUDOERS_FILE", "MODTOOLS_ARM")
        self.assertFalse(privileged.available("x86_64"))


class Staging(unittest.TestCase):
    """Where install() puts files before the elevated copy reads them."""

    def test_the_wrapper_source_is_what_gets_hashed(self):
        self.assertEqual(len(privileged.wrapper_digest()), 16)
        self.assertIn("runoverlay", privileged.WRAPPER_SOURCE)

    def test_a_sudoers_rule_names_a_path_without_spaces(self):
        """sudoers treats an unescaped space as an argument separator, so a
        rule naming a spaced path parses cleanly and then never matches."""
        rule = privileged._sudoers_rule("someone")
        self.assertIn("NOPASSWD:", rule)
        self.assertNotIn(" ", str(privileged.WRAPPER))
        self.assertTrue(rule.endswith("\n"))

    def test_a_spaced_wrapper_path_is_refused_outright(self):
        saved = privileged.WRAPPER
        privileged.WRAPPER = Path("/Library/Application Support/x/inject")
        try:
            with self.assertRaises(ValueError):
                privileged._sudoers_rule("someone")
        finally:
            privileged.WRAPPER = saved


if __name__ == "__main__":
    unittest.main()
