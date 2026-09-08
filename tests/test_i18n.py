#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which language the windows are drawn in."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import i18n  # noqa: E402


class Resolve(unittest.TestCase):

    def test_a_chosen_language_is_used_as_is(self):
        self.assertEqual(i18n.resolve("ru"), "ru")
        self.assertEqual(i18n.resolve("en"), "en")

    def test_system_follows_the_display_language(self):
        with mock.patch.object(i18n, "system_language", return_value="ru"):
            self.assertEqual(i18n.resolve("system"), "ru")
            self.assertEqual(i18n.resolve(None), "ru")

    def test_an_unknown_choice_falls_back_to_the_system(self):
        with mock.patch.object(i18n, "system_language", return_value="en"):
            self.assertEqual(i18n.resolve("xx"), "en")

    def test_an_unsupported_display_language_is_english(self):
        with mock.patch.object(i18n, "_windows_ui_language", return_value=None), \
                mock.patch.object(i18n, "_posix_language", return_value="de"):
            self.assertEqual(i18n.system_language(), "en")

    def test_the_windows_primary_language_is_read_off_the_langid(self):
        # 0x0419 is Russian (Russia); 0x0819 Russian (Moldova): same language.
        with mock.patch("sys.platform", "win32"):
            for langid in (0x0419, 0x0819):
                kernel = mock.Mock()
                kernel.GetUserDefaultUILanguage.return_value = langid
                with mock.patch("ctypes.windll", create=True) as windll:
                    windll.kernel32 = kernel
                    self.assertEqual(i18n.system_language(), "ru")


if __name__ == "__main__":
    unittest.main()
