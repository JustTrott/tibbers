#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Whether this League build is still one the patcher can patch.

The patcher overwrites a function it finds by scanning for Riot's code. When
a League patch moves that ground the patch still reports success and the game
dies at launch with an empty log folder, so the checks that catch it before a
patcher is started are worth having tests of their own.
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import patchcheck  # noqa: E402

TEXT_ADDR = 0x100000000
NOP = 0xD503201F
RET = 0xD65F03C0


def text(size: int = 0x1000) -> bytearray:
    return bytearray(struct.pack("<I", NOP) * (size // 4))


def put(buf: bytearray, at: int, word: int) -> None:
    buf[at:at + 4] = struct.pack("<I", word)


def bl(from_off: int, to_off: int) -> int:
    return 0x94000000 | (((to_off - from_off) // 4) & 0x03FFFFFF)


def target(buf: bytearray, pattern_at: int, func_at: int, length: int) -> None:
    """Lay down the pattern, the call after it, and a function of *length*."""
    buf[pattern_at:pattern_at + 8] = patchcheck.PATTERN
    put(buf, pattern_at + 8, bl(pattern_at + 8, func_at))
    put(buf, func_at + length - 4, RET)


class ScanText(unittest.TestCase):
    def test_a_function_with_room_passes(self):
        buf = text()
        target(buf, 0x40, 0x200, patchcheck.PAYLOAD_ROOM)
        ok, why = patchcheck.scan_text(bytes(buf), TEXT_ADDR)
        self.assertTrue(ok, why)
        self.assertIn(hex(TEXT_ADDR + 0x200), why)

    def test_a_function_too_small_is_refused(self):
        buf = text()
        target(buf, 0x40, 0x200, 104)
        ok, why = patchcheck.scan_text(bytes(buf), TEXT_ADDR)
        self.assertFalse(ok)
        self.assertIn("104 bytes", why)

    def test_a_pattern_that_is_gone_is_refused(self):
        ok, why = patchcheck.scan_text(bytes(text()), TEXT_ADDR)
        self.assertFalse(ok)
        self.assertIn("gone", why)

    def test_a_pattern_that_matches_twice_is_refused(self):
        buf = text()
        target(buf, 0x40, 0x200, patchcheck.PAYLOAD_ROOM)
        target(buf, 0x600, 0x800, patchcheck.PAYLOAD_ROOM)
        ok, why = patchcheck.scan_text(bytes(buf), TEXT_ADDR)
        self.assertFalse(ok)
        self.assertIn("2 places", why)

    def test_a_pattern_no_longer_followed_by_a_call_is_refused(self):
        buf = text()
        buf[0x40:0x48] = patchcheck.PATTERN
        ok, why = patchcheck.scan_text(bytes(buf), TEXT_ADDR)
        self.assertFalse(ok)
        self.assertIn("no longer a call", why)

    def test_a_function_that_never_returns_is_refused(self):
        buf = text()
        buf[0x40:0x48] = patchcheck.PATTERN
        put(buf, 0x48, bl(0x48, 0x200))
        ok, why = patchcheck.scan_text(bytes(buf), TEXT_ADDR)
        self.assertFalse(ok)
        self.assertIn("does not return", why)


class InstalledGame(unittest.TestCase):
    """Against the real install, when there is one."""

    GAME = Path("/Applications/League of Legends.app/Contents/LoL/Game")

    def test_the_installed_build_is_patchable(self):
        if not patchcheck.binary_path(self.GAME).exists():
            self.skipTest("League is not installed here")
        ok, why = patchcheck.check(self.GAME)
        self.assertTrue(ok, why)

    def test_a_missing_binary_is_refused_not_crashed(self):
        ok, why = patchcheck.check(Path("/nowhere/LoL/Game"))
        self.assertFalse(ok)
        self.assertIn("could not read", why)


if __name__ == "__main__":
    unittest.main()
