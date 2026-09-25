#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noticing that the installed LTK patcher is finished.

LTK's DLL expires. Until 1.1.2 nothing ever replaced what the first launch
fetched, so an install kept one release for as long as it lived and every game
opened with no skin once that release passed its date. Two things spot it: the
patcher's own log, which needs no network and is the symptom itself, and the
release tag recorded beside the binaries. The network is stubbed throughout.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import wintools  # noqa: E402


class Expired(unittest.TestCase):
    """The patcher log is read for the line the expired DLL writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "runoverlay.log"

    def test_the_real_line_is_recognised(self):
        # Copied from a machine whose patcher had expired.
        self.log.write_text(
            "status 38.6242348 injected dll attached\n"
            "dll 38.6 28388 23896 INFO ltk_patcher_dll::entry: init in process\n"
            "dll 38.6 28388 23896 ERROR ltk_patcher_dll::entry: "
            "end of life reached, please update: 0x6aa480dd\n")
        self.assertTrue(wintools.expired(self.log))

    def test_a_healthy_log_is_not_expired(self):
        self.log.write_text(
            "dll 24.6 4528 33332 INFO ltk_patcher_dll::entry: init done\n"
            "dll 29.2 4528 30820 INFO ltk_patcher_dll::hooks::fsov::imp: "
            "redirected wad: DATA/FINAL/Champions/Tristana.wad.client\n")
        self.assertFalse(wintools.expired(self.log))

    def test_no_log_at_all_is_not_expired(self):
        # A machine that has never injected has nothing to say about it.
        self.assertFalse(wintools.expired(self.log))


class Outdated(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.where = Path(self.tmp.name)

    def _pair(self):
        for name in wintools.LTK_FILES:
            (self.where / name).write_bytes(b"MZ\x90\x00")

    def _latest(self, tag):
        return mock.patch.object(wintools, "_latest_release",
                                 return_value={"tag_name": tag})

    def test_an_install_with_no_record_is_outdated(self):
        # Every install made before 1.1.2 looks like this, and every one of
        # them is carrying a patcher that has since expired.
        self._pair()
        with self._latest("v1.19.2"):
            self.assertTrue(wintools.ltk_outdated(self.where))

    def test_a_matching_tag_is_current(self):
        self._pair()
        wintools._record(self.where, ltk="v1.19.2")
        with self._latest("v1.19.2"):
            self.assertFalse(wintools.ltk_outdated(self.where))

    def test_an_older_tag_is_outdated(self):
        self._pair()
        wintools._record(self.where, ltk="v1.15.2")
        with self._latest("v1.19.2"):
            self.assertTrue(wintools.ltk_outdated(self.where))

    def test_a_missing_pair_is_outdated(self):
        with self._latest("v1.19.2"):
            self.assertTrue(wintools.ltk_outdated(self.where))

    def test_an_unreachable_github_leaves_the_patcher_alone(self):
        # Asking failed, so we do not know it is old -- and throwing away a
        # patcher that may be working would be worse than keeping it.
        self._pair()
        wintools._record(self.where, ltk="v1.15.2")
        with mock.patch.object(wintools, "_latest_release",
                               side_effect=OSError("offline")):
            self.assertFalse(wintools.ltk_outdated(self.where))

    def test_the_record_merges_rather_than_replaces(self):
        wintools._record(self.where, cslol="2026-04-15")
        wintools._record(self.where, ltk="v1.19.2")
        data = json.loads((self.where / wintools.RECORD).read_text())
        self.assertEqual(data["cslol"], "2026-04-15")
        self.assertEqual(data["ltk"], "v1.19.2")

    def test_an_unreadable_record_reads_as_empty(self):
        (self.where / wintools.RECORD).write_text("{not json")
        self.assertEqual(wintools._read_record(self.where), {})


def _nsis(files, dict_size=1 << 20):
    """A minimal solid-LZMA NSIS installer carrying *files* ({name: bytes}).

    Shaped like the one LTK ships since v1.20.0: a stub, the first header,
    and one LZMA stream holding the script header then each file as a length
    and its bytes. Only what the unpacker reads is filled in.
    """
    import lzma
    import struct

    strings = "\0".join(["", "$PLUGINSDIR\\System.dll", *files]) + "\0"
    table = strings.encode("utf-16-le")
    names, at = [], len("\0$PLUGINSDIR\\System.dll\0")
    for name in files:
        names.append(at)
        at += len(name) + 1

    payload, offsets = b"", []
    for blob in files.values():
        offsets.append(len(payload))
        payload += struct.pack("<I", len(blob)) + blob
    # One entry that is not an extraction, one that extracts something else,
    # then the files asked for.
    entries = struct.pack("<7I", 1, 0, 0, 0, 0, 0, 0)
    entries += struct.pack("<7I", 20, 0, 1, 0, 0, 0, 0)
    for name, offset in zip(names, offsets):
        entries += struct.pack("<7I", 20, 0, name, offset, 0, 0, 0)

    top = 4 + 8 * 8
    blocks = [(0, 0), (0, 0), (top, len(entries) // 28),
              (top + len(entries), 0), (top + len(entries) + len(table), 0),
              (0, 0), (0, 0), (0, 0)]
    header = struct.pack("<I", 0) + b"".join(
        struct.pack("<II", *b) for b in blocks) + entries + table

    stream = struct.pack("<I", len(header)) + header + payload
    packed = lzma.compress(stream, format=lzma.FORMAT_RAW, filters=[{
        "id": lzma.FILTER_LZMA1, "dict_size": dict_size,
        "lc": 3, "lp": 0, "pb": 2}])
    body = bytes([0x5D]) + struct.pack("<I", dict_size) + packed
    first = struct.pack("<I", 0) + b"\xef\xbe\xad\xdeNullsoftInst" + \
        struct.pack("<II", len(header), 28 + len(body))
    return b"MZ" + b"\0" * 510 + first + body


class UnpackNsis(unittest.TestCase):
    """LTK's patcher is read out of its NSIS setup, never by running it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.where = Path(self.tmp.name)

    def test_the_patcher_pair_comes_out_byte_for_byte(self):
        files = {"ltk-manager.exe": b"MZ" + b"m" * 300_000,
                 "ltk_patcher_dll.dll": b"MZ" + b"d" * 7000,
                 "ltk_patcher_host.exe": b"MZ" + bytes(range(256)) * 40}
        setup = self.where / "LTK.Manager_x64-setup.exe"
        setup.write_bytes(_nsis(files))
        out = self.where / "out"
        out.mkdir()
        wintools.unpack_nsis(setup, wintools.LTK_FILES, out)
        for name in wintools.LTK_FILES:
            self.assertEqual((out / name).read_bytes(), files[name], name)
        self.assertFalse((out / "ltk-manager.exe").exists())

    def test_a_setup_without_the_patcher_says_what_is_missing(self):
        setup = self.where / "setup.exe"
        setup.write_bytes(_nsis({"ltk_patcher_dll.dll": b"MZ"}))
        with self.assertRaisesRegex(RuntimeError, "ltk_patcher_host.exe"):
            wintools.unpack_nsis(setup, wintools.LTK_FILES, self.where)

    def test_something_that_is_not_nsis_is_refused(self):
        setup = self.where / "setup.msi"
        setup.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 4096)
        with self.assertRaisesRegex(RuntimeError, "not an NSIS installer"):
            wintools.unpack_nsis(setup, wintools.LTK_FILES, self.where)

    def test_the_setup_asset_is_the_one_picked(self):
        # What the v1.22.1 release actually carries.
        release = {"assets": [
            {"name": n, "browser_download_url": "https://example.invalid/" + n}
            for n in ("latest.json", "LTK.Manager_1.22.1_x64-setup.exe",
                      "LTK.Manager_1.22.1_x64-setup.exe.sig",
                      "LTK.Manager_x64-setup.exe")]}
        seen = []

        def fake_unpack(setup, names, into):
            seen.append(Path(setup).name)
            for name in names:
                (Path(into) / name).write_bytes(b"MZ")

        with mock.patch.object(wintools, "_latest_release",
                               return_value={**release, "tag_name": "v1.22.1"}), \
                mock.patch.object(wintools, "_download"), \
                mock.patch.object(wintools, "unpack_nsis", fake_unpack):
            wintools._fetch_ltk(self.where, None)
        self.assertEqual(seen, ["LTK.Manager_1.22.1_x64-setup.exe"])
        self.assertEqual(wintools._read_record(self.where)["ltk"], "v1.22.1")


class Orphans(unittest.TestCase):
    """Tools an older version fetched and no version still uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.where = Path(self.tmp.name)

    def test_the_dead_curl_is_removed(self):
        # 4 MB whose only job was getting past u.gg's CDN. The data directory
        # survives every upgrade, so nothing else would ever delete it.
        (self.where / "curl-impersonate.exe").write_bytes(b"MZ\x90\x00")
        self.assertEqual(wintools.remove_orphans(self.where),
                         ["curl-impersonate.exe"])
        self.assertFalse((self.where / "curl-impersonate.exe").exists())

    def test_the_tools_still_in_use_are_left_alone(self):
        for name in (*wintools.CSLOL_FILES, *wintools.LTK_FILES):
            (self.where / name).write_bytes(b"MZ\x90\x00")
        wintools.remove_orphans(self.where)
        for name in (*wintools.CSLOL_FILES, *wintools.LTK_FILES):
            self.assertTrue((self.where / name).exists(), name)

    def test_nothing_to_remove_is_not_an_error(self):
        self.assertEqual(wintools.remove_orphans(self.where), [])


if __name__ == "__main__":
    unittest.main()
