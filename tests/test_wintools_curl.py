#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetching the browser-handshake curl for the u.gg transport on Windows.

The interesting parts are choosing the right architecture's tarball and
flattening the one executable out of it; the download itself is stubbed. Runs
on any platform (the archive handling is not Windows-specific).
"""

from __future__ import annotations

import gzip
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import wintools  # noqa: E402


def fake_tarball(dest: Path, inner_name: str, payload: bytes) -> None:
    """Write a .tar.gz holding one file, under a leading directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(inner_name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    dest.write_bytes(gzip.compress(buf.getvalue()))


class FetchBrowserCurl(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.where = Path(self.tmp.name)

    def _wire(self, machine, assets):
        """Make _latest_asset run the real matcher over *assets* and
        _download write a fake tarball for the chosen one."""
        chosen = {}

        def latest(api, match):
            self.assertEqual(api, wintools.CURL_LATEST)
            for a in assets:
                if match(a):
                    chosen["name"] = a["name"]
                    return a
            raise RuntimeError("no asset matched")

        def download(url, dest, progress=None, label=""):
            fake_tarball(Path(dest),
                         f"curl-impersonate-x/{wintools.CURL_EXE}", b"MZ\x90\x00")

        self.chosen = chosen
        for target, repl in (("_latest_asset", latest), ("_download", download)):
            p = mock.patch.object(wintools, target, repl)
            p.start()
            self.addCleanup(p.stop)
        mp = mock.patch("platform.machine", return_value=machine)
        mp.start()
        self.addCleanup(mp.stop)

    def test_picks_the_machine_arch_and_flattens_the_exe(self):
        assets = [
            {"name": "libcurl-impersonate-v2.2.2.x86_64-win32.tar.gz",
             "browser_download_url": "u"},
            {"name": "curl-impersonate-v2.2.2.arm64-win32.tar.gz",
             "browser_download_url": "u"},
            {"name": "curl-impersonate-v2.2.2.x86_64-win32.tar.gz",
             "browser_download_url": "u"},
        ]
        self._wire("AMD64", assets)
        path = wintools.ensure_browser_curl(self.where)
        # The standalone x86_64 curl, not the arm64 one and not libcurl.
        self.assertEqual(self.chosen["name"],
                         "curl-impersonate-v2.2.2.x86_64-win32.tar.gz")
        self.assertIsNotNone(path)
        self.assertEqual(path, self.where / wintools.CURL_EXE)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"MZ\x90\x00")

    def test_arm_windows_gets_the_arm_tarball(self):
        assets = [
            {"name": "curl-impersonate-v2.2.2.x86_64-win32.tar.gz",
             "browser_download_url": "u"},
            {"name": "curl-impersonate-v2.2.2.arm64-win32.tar.gz",
             "browser_download_url": "u"},
        ]
        self._wire("ARM64", assets)
        wintools.ensure_browser_curl(self.where)
        self.assertEqual(self.chosen["name"],
                         "curl-impersonate-v2.2.2.arm64-win32.tar.gz")

    def test_a_present_exe_is_not_refetched(self):
        (self.where / wintools.CURL_EXE).write_bytes(b"already")
        with mock.patch.object(wintools, "_fetch_browser_curl",
                               side_effect=AssertionError("should not fetch")):
            path = wintools.ensure_browser_curl(self.where)
        self.assertEqual(path, self.where / wintools.CURL_EXE)

    def test_force_refetches_even_when_present(self):
        (self.where / wintools.CURL_EXE).write_bytes(b"old")
        self._wire("AMD64", [
            {"name": "curl-impersonate-v2.2.2.x86_64-win32.tar.gz",
             "browser_download_url": "u"}])
        wintools.ensure_browser_curl(self.where, force=True)
        self.assertEqual((self.where / wintools.CURL_EXE).read_bytes(),
                         b"MZ\x90\x00")


if __name__ == "__main__":
    unittest.main()
