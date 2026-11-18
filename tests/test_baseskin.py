#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The base-skin fix (tibbers/baseskin), ported from Rose's injection/baseskin.

These tests exercise the pure pieces -- hashing, the PROP and bank round-trips,
the mesh/skeleton rebuild, and the audio-name helpers -- without a game install.
The end-to-end fix against real WADs is the Windows acceptance check.
"""

import struct
import unittest
from pathlib import Path

from tibbers import wad as tibbers_wad
from tibbers.baseskin import bnk, mesh, propbin, audio, steps, wad


class HashTests(unittest.TestCase):
    def test_fnv1a_lower_matches_skinsmith_constants(self):
        # The same values skinsmith.py pins for the game's own bin names.
        self.assertEqual(propbin.fnv1a_lower("SkinCharacterDataProperties"), 0x9B67E9F6)
        self.assertEqual(propbin.fnv1a_lower("championSkinName"), 0x2D78C328)
        self.assertEqual(propbin.fnv1a_lower("skinClassification"), 0x87225880)

    def test_wwise_hash_empty_and_case(self):
        self.assertEqual(bnk.wwise_hash(""), 0x811C9DC5)
        self.assertEqual(bnk.wwise_hash("Play_sfx_Aatrox"), bnk.wwise_hash("play_sfx_aatrox"))

    def test_path_hash_matches_tibbers_wad(self):
        path = "DATA/FINAL/Champions/Fiora.wad.client"
        self.assertEqual(wad.path_hash(path), tibbers_wad.xxh64_path(path))

    def test_elf_hash_is_deterministic(self):
        self.assertEqual(mesh.elf_hash_lower("null_bone"), mesh.NULL_JOINT_HASH)
        self.assertNotEqual(mesh.elf_hash_lower("bone0"), mesh.elf_hash_lower("bone1"))


class PropTests(unittest.TestCase):
    def test_build_parse_roundtrip(self):
        entry = propbin.Entry(0x1234, 0x5678, [propbin.Field(0x9ABC, propbin.Value.string("hello"))])
        data = propbin.build([entry])
        parsed = propbin.Bin(data)
        found = parsed.find_entry(0x1234)
        self.assertIsNotNone(found)
        self.assertEqual(found.path_hash, 0x5678)
        self.assertEqual(found.find_field(0x9ABC).value.as_string(), "hello")
        # Nothing changed: the write is the same bytes.
        self.assertEqual(parsed.write(), data)

    def test_edit_and_rewrite(self):
        entry = propbin.Entry(0x1234, 0x5678, [propbin.Field(0x9ABC, propbin.Value.string("hello"))])
        data = propbin.build([entry])
        parsed = propbin.Bin(data)
        parsed.find_entry(0x1234).set_field(0x9ABC, propbin.Value.string("world"))
        out = parsed.write()
        self.assertNotEqual(out, data)
        again = propbin.Bin(out)
        self.assertEqual(again.find_entry(0x1234).find_field(0x9ABC).value.as_string(), "world")

    def test_not_a_prop(self):
        with self.assertRaises(propbin.PropError):
            propbin.Bin(b"not a prop file")


class BankTests(unittest.TestCase):
    @staticmethod
    def make_bank():
        version = 120
        bkhd = b"BKHD" + struct.pack("<I", 4) + struct.pack("<I", version)
        event_body = struct.pack("<I", 0)  # zero actions (u32 for version <= 122)
        event = struct.pack("<BII", 4, 4 + len(event_body), 0x1234) + event_body
        hirc_body = struct.pack("<I", 1) + event
        hirc = b"HIRC" + struct.pack("<I", len(hirc_body)) + hirc_body
        return bkhd + hirc

    def test_parse_write_roundtrip(self):
        data = self.make_bank()
        bank = bnk.Bank.parse(data)
        self.assertEqual(bank.write(), data)

    def test_event_actions(self):
        bank = bnk.Bank.parse(self.make_bank())
        self.assertTrue(bank.has_event(0x1234))
        self.assertEqual(bank.event_actions(0x1234), [])
        bank.add_event(0x9999, [1, 2, 3])
        self.assertEqual(bank.event_actions(0x9999), [1, 2, 3])
        bank.remove_event(0x1234)
        self.assertFalse(bank.has_event(0x1234))


class MeshTests(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(mesh.is_mesh(struct.pack("<IHH", 0x00112233, 0, 0) + bytes(60)))
        self.assertTrue(mesh.is_skeleton(self.make_skeleton()))
        self.assertFalse(mesh.is_skeleton(b"garbage"))

    @staticmethod
    def make_mesh():
        # version 0, one vertex, no indices.
        return struct.pack("<IHHII", 0x00112233, 0, 0, 0, 1) + bytes(52)

    @staticmethod
    def make_skeleton():
        joints_offset = 64
        table_offset = 64 + 100
        name_offset = table_offset + 8
        data = bytearray(64)
        struct.pack_into("<I", data, 4, 0x22FD4FC3)  # format token
        struct.pack_into("<I", data, 8, 0)           # version
        struct.pack_into("<H", data, 14, 1)          # joint count
        struct.pack_into("<i", data, 20, joints_offset)
        struct.pack_into("<i", data, 24, table_offset)
        joint = bytearray(100)
        struct.pack_into("<i", joint, 96, name_offset - (joints_offset + 96))
        data += joint
        data += struct.pack("<hhI", 0, 0, mesh.elf_hash_lower("bone0"))
        data += b"bone0\x00"
        struct.pack_into("<I", data, 0, len(data))
        return bytes(data)

    def test_mesh_rebuild_adds_null_vertex(self):
        data = self.make_mesh()
        rebuilt, added = mesh.rebuild(data)
        self.assertEqual(added, "null vertex")
        self.assertTrue(mesh.is_rebuilt(rebuilt))
        # vertex count went from 1 to 2.
        self.assertEqual(struct.unpack_from("<I", rebuilt, 12)[0], 2)

    def test_skeleton_rebuild_adds_null_joint(self):
        data = self.make_skeleton()
        rebuilt, added = mesh.rebuild(data)
        self.assertEqual(added, "null joint")
        self.assertTrue(mesh.is_rebuilt(rebuilt))
        self.assertEqual(struct.unpack_from("<H", rebuilt, 14)[0], 2)  # joint count

    def test_rebuild_rejects_unknown(self):
        with self.assertRaises(mesh.MeshError):
            mesh.rebuild(b"not a mesh or skeleton")


class AudioNameTests(unittest.TestCase):
    def test_base_bank_path(self):
        self.assertEqual(
            audio.base_bank_path("assets/sounds/wwise2016/vo/en_us/characters/talon/skins/skin12/talon_skin12_vo_audio.bnk", 1),
            "assets/sounds/wwise2016/vo/en_us/characters/talon/skins/Base/talon_Base_vo_audio.bnk")
        self.assertIsNone(audio.base_bank_path("assets/sounds/wwise2016/characters/talon/skins/base/talon_base_vo_audio.bnk", 1))

    def test_base_unit_name(self):
        self.assertEqual(audio.base_unit_name("Aatrox_Skin01_SFX"), "Aatrox_Base_SFX")
        self.assertIsNone(audio.base_unit_name("Aatrox_Base_SFX"))

    def test_tag_start(self):
        self.assertEqual(audio.tag_start("Play_sfx_Aatrox_AatroxQ_OnCast"), len("Play_sfx_"))
        self.assertIsNone(audio.tag_start("Play"))

    def test_locale_helpers(self):
        self.assertTrue(audio.is_locale("fr_FR"))
        self.assertFalse(audio.is_locale("fr"))
        self.assertEqual(audio.normalize_locale("fr_fr"), "fr_FR")
        self.assertEqual(audio.strip_suffix("Fiora.wad.client", ".wad.client"), "Fiora")

    def test_has_skin_tag(self):
        self.assertTrue(audio.has_skin_tag("Play_sfx_TalonSkin12_Dance3D_buffactivate"))
        self.assertFalse(audio.has_skin_tag("Play_sfx_Talon_Dance3D_buffactivate"))


class ModuleTests(unittest.TestCase):
    def test_all_steps_named(self):
        self.assertEqual(
            steps.ALL_STEPS,
            frozenset({"audio", "banks", "mesh", "name", "bankpaths", "eventnames"}))

    def test_fix_mod_exported(self):
        from tibbers.baseskin import FixResult, fix_mod
        self.assertTrue(callable(fix_mod))
        self.assertTrue(callable(FixResult))


class RebaseTests(unittest.TestCase):
    def test_sidecar_commit(self):
        from tibbers.baseskin import rebase
        import tempfile as _tmp
        with _tmp.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "x.fantome"
            self.assertIsNone(rebase.sidecar_commit(mod))
            rebase.sidecar_path(mod).write_text('{"commit": "abc", "version": 1}', encoding="utf-8")
            self.assertEqual(rebase.sidecar_commit(mod), "abc")

    def test_rebase_without_game_dir_is_not_fatal(self):
        # No DATA/FINAL/Champions: fix_mod returns complete=False without raising,
        # so a mod fetched on a machine with no install is simply not rebased yet.
        import tempfile as _tmp
        import zipfile as _zip
        from tibbers.baseskin import rebase
        with _tmp.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mod = root / "x.fantome"
            with _zip.ZipFile(mod, "w") as z:
                z.writestr("META/info.json", "{}")
                z.writestr("WAD/Akali.wad.client", b"not a real wad")
            result = rebase.rebase_mod(mod, root / "Game", "c" * 40)
            self.assertIsNotNone(result)
            self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
