#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The LTF bridge's handling of LTK Manager's WebView2 being slow to mount."""

import unittest

from tibbers.ltk_bridge import is_transient_cdp_error, _error_text


class TransientCdpTests(unittest.TestCase):
    def test_cdp_timeout_is_transient(self):
        self.assertTrue(is_transient_cdp_error(
            {"ok": False, "raw": {"ok": False, "error": "CDP unavailable or timed out"}}))

    def test_invoke_not_ready_is_transient(self):
        self.assertTrue(is_transient_cdp_error(
            {"ok": False, "raw": {"ok": False,
                                  "error": "Cannot read properties of undefined (reading 'invoke')"}}))

    def test_status_command_shape_is_transient(self):
        # patcher_status answers with a "response" wrapper, not "raw".
        self.assertTrue(is_transient_cdp_error(
            {"ok": False, "response": {"ok": False, "error": "CDP unavailable or timed out"}}))

    def test_cdp_busy_is_transient(self):
        self.assertTrue(is_transient_cdp_error(
            {"ok": False, "raw": {"ok": False, "error": "CDP busy"}}))

    def test_ok_is_not_transient(self):
        self.assertFalse(is_transient_cdp_error({"ok": True}))

    def test_a_real_error_is_not_transient(self):
        self.assertFalse(is_transient_cdp_error(
            {"ok": False, "raw": {"ok": False, "error": "some real error"}}))

    def test_error_text_names_the_cdp_timeout(self):
        self.assertEqual(
            _error_text({"ok": False, "raw": {"ok": False, "error": "CDP unavailable or timed out"}}),
            "CDP unavailable or timed out")


if __name__ == "__main__":
    unittest.main()
