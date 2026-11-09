#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The session header and what feeds it.

The header is written on the way up, so the property that matters most is
that a probe which cannot answer says "unknown" instead of stopping the app.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import diagnostics  # noqa: E402


class Probe(unittest.TestCase):

    def test_a_failing_probe_answers_unknown(self):
        def broken():
            raise OSError("no registry here")
        self.assertEqual(diagnostics._probe(broken), "unknown (no registry here)")

    def test_a_failing_group_of_lines_is_one_unknown_line(self):
        def broken():
            raise RuntimeError("LTK Manager is gone")
        self.assertEqual(diagnostics._probe_lines(broken, "Patcher:  "),
                         ["Patcher:  unknown (LTK Manager is gone)"])

    def test_utc_offset_reads_like_an_offset(self):
        self.assertRegex(diagnostics.utc_offset(), r"^UTC[+-]\d\d:\d\d$")


class SessionLines(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def lines(self, **kw):
        args = dict(client_dir=Path(r"D:\Riot Games\League of Legends"),
                    data_dir=self.home, tools_dir=self.home / "tools",
                    manager_backend=False, install_source="settings")
        args.update(kw)
        return diagnostics.session_lines(**args)

    def test_every_fact_has_its_own_line(self):
        text = "\n".join(self.lines())
        from tibbers import __version__
        self.assertIn(f"===== tibbers {__version__}", text)
        for prefix in ("League:", "Backend:", "Patcher:", "Rights:", "Data:"):
            self.assertIn(prefix, text)
        self.assertIn("(found by settings)", text)

    def test_missing_patcher_files_are_named_missing(self):
        text = "\n".join(self.lines())
        self.assertIn("host missing", text)
        self.assertIn("dll missing", text)

    def test_a_broken_patcher_probe_does_not_stop_the_header(self):
        with mock.patch.object(diagnostics, "patcher_lines",
                               side_effect=RuntimeError("boom")):
            lines = self.lines()
        self.assertIn("Patcher:  unknown (boom)", lines)
        self.assertTrue(lines[-1].startswith("Data:"))

    def test_the_header_is_plain_ascii(self):
        # The log file was written in the system code page; a separator it
        # cannot hold came out as a question mark on a Russian machine.
        for line in self.lines(client_dir=Path(r"C:\Riot Games\League of Legends")):
            prefix = line.split(":", 1)[0]
            self.assertTrue(prefix.isascii(), line)
            self.assertNotIn("\u00b7", line)


@unittest.skipUnless(diagnostics.IS_WINDOWS, "Windows signature check")
class Signature(unittest.TestCase):

    def test_a_plain_file_is_unsigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.exe"
            path.write_bytes(b"MZ" + b"\0" * 510)
            self.assertNotEqual(diagnostics.signature(path), "signed")


if __name__ == "__main__":
    unittest.main()
