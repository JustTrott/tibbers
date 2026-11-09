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


class Redaction(unittest.TestCase):
    """The archive is posted where others read it."""

    def setUp(self):
        self.scrub = diagnostics.redactor("BG", secrets=["a1b2c3d4e5f6a7b8"])

    def test_the_user_name_leaves_every_spelling_of_the_profile_path(self):
        text = self.scrub(r'C:\Users\BG\AppData "C:\\Users\\BG\\AppData" C:/Users/bg/x')
        self.assertNotIn("BG", text.replace("<user>", ""))
        self.assertEqual(text.count("<user>"), 3)

    def test_a_longer_name_that_starts_the_same_is_left_alone(self):
        self.assertIn(r"\Users\BGamer", self.scrub(r"C:\Users\BGamer\AppData"))

    def test_the_bridge_token_and_client_password_go(self):
        text = self.scrub("hello token=a1b2c3d4e5f6a7b8 https://riot:Zq9xW_k2LmPa@127.0.0.1")
        self.assertNotIn("a1b2c3d4e5f6a7b8", text)
        self.assertNotIn("Zq9xW_k2LmPa", text)
        self.assertIn("riot:<password>", text)


class Collect(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "data"
        (self.data / "work").mkdir(parents=True)
        (self.data / "tibbers.log").write_text(
            r"2026-11-09 14:48:13 | INFO | game: C:\Users\BG\Game" + "\n", encoding="utf-8")
        (self.data / "work" / "runoverlay.log").write_text("status 1 injecting\n")
        appdata = root / "appdata"
        (appdata / "dev.leaguetoolkit.manager" / "logs").mkdir(parents=True)
        (appdata / "dev.leaguetoolkit.manager" / "logs" / "ltk-manager.2026-11-09.log"
         ).write_text("INFO patcher started\n")
        public = root / "public"
        public.mkdir()
        (public / "ltk_api.log").write_text("token=a1b2c3d4e5f6a7b8\n")
        self.out = root / "out"
        env = mock.patch.dict("os.environ", {
            "APPDATA": str(appdata), "PUBLIC": str(public),
            "TEMP": str(root / "no-temp"), "USERNAME": "BG"})
        env.start()
        self.addCleanup(env.stop)

    def collect(self):
        import zipfile
        path = diagnostics.collect(["===== tibbers test"], self.data, None,
                                   dest_dir=self.out, secrets=["a1b2c3d4e5f6a7b8"])
        with zipfile.ZipFile(path) as archive:
            return path, {n: archive.read(n).decode() for n in archive.namelist()}

    def test_the_logs_from_every_folder_land_in_one_zip(self):
        path, files = self.collect()
        self.assertTrue(path.name.startswith("tibbers-diagnostics-"))
        for name in ("summary.txt", "tibbers/tibbers.log", "tibbers/runoverlay.log",
                     "ltk-manager/ltk-manager.2026-11-09.log", "bridge-public/ltk_api.log"):
            self.assertIn(name, files)

    def test_the_summary_says_what_was_not_there(self):
        _, files = self.collect()
        summary = files["summary.txt"]
        self.assertIn("===== tibbers test", summary)
        self.assertIn("Not found:", summary)
        self.assertIn("tibbers/preferences.json", summary.split("Not found:")[1])

    def test_nothing_in_the_zip_names_the_user_or_holds_the_token(self):
        _, files = self.collect()
        everything = "\n".join(files.values())
        self.assertNotIn(r"\Users\BG", everything)
        self.assertNotIn("a1b2c3d4e5f6a7b8", everything)

    def test_a_huge_log_keeps_its_end(self):
        with mock.patch.object(diagnostics, "FILE_CAP", 100):
            (self.data / "tibbers.log").write_text("old\n" * 100 + "THE END\n")
            _, files = self.collect()
        log = files["tibbers/tibbers.log"]
        self.assertTrue(log.startswith("[first "))
        self.assertTrue(log.rstrip().endswith("THE END"))


@unittest.skipUnless(diagnostics.IS_WINDOWS, "Windows signature check")
class Signature(unittest.TestCase):

    def test_a_plain_file_is_unsigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.exe"
            path.write_bytes(b"MZ" + b"\0" * 510)
            self.assertNotEqual(diagnostics.signature(path), "signed")


if __name__ == "__main__":
    unittest.main()
