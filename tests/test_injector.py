#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The overlay build, over several mods.

A party's skins load as one overlay: every mod unpacked under a name of its
own, and all of them listed in a single `--mods:` argument, which mkoverlay
splits on `/`. mkoverlay itself is not run here, so these pin the command
line and the guards in front of it. The patcher is not touched by any of it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import injector  # noqa: E402

MODTOOLS = Path("/tools/mod-tools")


class Build(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.game = self.root / "Game"
        self.inject = injector.Injector(self.game, self.root / "tools",
                                        self.root / "work")
        self.runs = []
        self.in_use = self.patch(injector.Injector, "overlay_in_use",
                                 return_value=False)
        self.patch(injector.subprocess, "run", side_effect=self.mkoverlay)
        self.patch(injector.system, "select_modtools", return_value=MODTOOLS)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def mkoverlay(self, cmd, **_kwargs):
        self.runs.append(cmd)
        wad = self.inject.overlay_dir / "DATA/FINAL/Champions/Ahri.wad.client"
        wad.parent.mkdir(parents=True, exist_ok=True)
        wad.write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def mod(self, name, folder="library", members=None):
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for member, data in (members or {"META/info.json": "{}",
                                             "WAD/skin0.bin": "x"}).items():
                archive.writestr(member, data)
        return path

    def command(self, mods):
        return [str(MODTOOLS), "mkoverlay", str(self.inject.mods_dir),
                str(self.inject.overlay_dir), f"--game:{self.game}",
                f"--mods:{mods}", "--noTFT", "--ignoreConflict"]

    def test_one_mod_is_the_command_it_always_was(self):
        result = self.inject.build_overlay([self.mod("103015.fantome")])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.runs, [self.command("103015")])

    def test_several_mods_are_one_mkoverlay_run(self):
        mods = [self.mod("103015.fantome"), self.mod("64012.fantome"),
                self.mod("105003.fantome")]
        result = self.inject.build_overlay(mods)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.runs, [self.command("103015/64012/105003")])

    def test_each_mod_is_unpacked_under_its_own_name(self):
        self.inject.build_overlay([self.mod("103015.fantome"),
                                   self.mod("64012.fantome")])
        for name in ("103015", "64012"):
            self.assertTrue(
                (self.inject.mods_dir / name / "META/info.json").is_file())

    def test_archives_sharing_a_name_get_folders_of_their_own(self):
        mods = [self.mod("skin.zip", "a"), self.mod("skin.zip", "b"),
                self.mod("skin.zip", "c")]
        self.inject.build_overlay(mods)
        self.assertEqual(self.runs, [self.command("skin/skin-2/skin-3")])
        for name in ("skin", "skin-2", "skin-3"):
            self.assertTrue((self.inject.mods_dir / name).is_dir())

    def test_a_single_path_is_refused_rather_than_read_as_a_list(self):
        with self.assertRaises(TypeError):
            self.inject.build_overlay(self.mod("103015.fantome"))
        with self.assertRaises(TypeError):
            self.inject.build_overlay(str(self.mod("103015.fantome")))
        self.assertEqual(self.runs, [])

    def test_no_mods_builds_nothing(self):
        result = self.inject.build_overlay([])
        self.assertFalse(result.ok)
        self.assertEqual(self.runs, [])

    def test_an_unsafe_archive_stops_the_build_before_mkoverlay(self):
        bad = self.mod("evil.zip", members={"../../escape.txt": "x"})
        result = self.inject.build_overlay([self.mod("103015.fantome"), bad])
        self.assertFalse(result.ok)
        self.assertIn("unsafe path", result.message)
        self.assertEqual(self.runs, [])

    def test_an_overlay_serving_a_game_is_left_alone(self):
        self.in_use.return_value = True
        self.inject.overlay_dir.mkdir(parents=True)
        (self.inject.overlay_dir / "live.wad.client").write_bytes(b"")
        result = self.inject.build_overlay([self.mod("103015.fantome"),
                                            self.mod("64012.fantome")])
        self.assertFalse(result.ok)
        self.assertEqual(self.runs, [])
        self.assertTrue((self.inject.overlay_dir / "live.wad.client").exists())

    def test_a_disabled_instance_builds_nothing(self):
        self.inject.enabled = False
        result = self.inject.build_overlay([self.mod("103015.fantome"),
                                            self.mod("64012.fantome")])
        self.assertFalse(result.ok)
        self.assertEqual(self.runs, [])

    def test_prepare_builds_every_mod_and_starts_the_patcher_once(self):
        start = self.patch(injector.Injector, "start_patcher",
                           return_value=injector.InjectionResult(True, "watching"))
        mods = [self.mod("103015.fantome"), self.mod("64012.fantome")]
        result = self.inject.prepare(mods, progress=lambda message: None,
                                     meta={"skinId": 103015})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.runs, [self.command("103015/64012")])
        start.assert_called_once_with(meta={"skinId": 103015})


if __name__ == "__main__":
    unittest.main()
