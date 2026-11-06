import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from tibbers import skin_catalogue as catalogue, downloader, library

def mod_bytes():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("META/info.json", '{"Author":"Original author"}')
        z.writestr("WAD/Akali.wad.client", b"test wad")
    return data.getvalue()

class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = patch.object(catalogue.system, "data_dir", return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)
        self.mode = patch.dict(os.environ, {"TIBBERS_LTK_BACKEND": "manager"})
        self.mode.start()
        self.addCleanup(self.mode.stop)
        catalogue._manifests.clear()
        catalogue._verified.clear()
        self.data = mod_bytes()
        self.digest = hashlib.sha1(f"blob {len(self.data)}\0".encode() + self.data).hexdigest()
        self.commit = "a" * 40
        self.path = "skins/84/84102/84102.fantome"
        self.index = (self.commit, {self.path: (self.digest, len(self.data))})
        self.dest = self.root / "mod.fantome"

    def test_download_is_identical_and_uses_pinned_commit(self):
        with patch.object(catalogue, "manifest", return_value=self.index), patch.object(
                catalogue, "_request", return_value=io.BytesIO(self.data)) as request:
            catalogue.fetch(84, 84102, None, self.dest)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertIn("/" + self.commit + "/" + self.path, request.call_args.args[0])

    def test_corrupt_download_preserves_previous_archive(self):
        self.dest.write_bytes(b"previous mod")
        with patch.object(catalogue, "manifest", return_value=self.index), patch.object(
                catalogue, "_request", return_value=io.BytesIO(b"x" * len(self.data))):
            with self.assertRaises(ValueError):
                catalogue.fetch(84, 84102, None, self.dest)
        self.assertEqual(self.dest.read_bytes(), b"previous mod")
        self.assertEqual(list(self.root.glob(".download-*")), [])

    def test_matching_file_does_not_download_again(self):
        self.dest.write_bytes(self.data)
        with patch.object(catalogue, "manifest", return_value=self.index), patch.object(
                catalogue, "_request") as request:
            catalogue.fetch(84, 84102, None, self.dest)
            request.assert_not_called()

    def test_verified_file_is_not_hashed_again(self):
        """Hovering a champion asks about every skin again and again."""
        with patch.object(catalogue, "manifest", return_value=self.index), patch.object(
                catalogue, "_request", return_value=io.BytesIO(self.data)):
            catalogue.fetch(84, 84102, None, self.dest)
        with patch.object(catalogue, "manifest", return_value=self.index), patch.object(
                catalogue, "_blob_hash", wraps=catalogue._blob_hash) as digest:
            catalogue.fetch(84, 84102, None, self.dest)
            digest.assert_not_called()

    def test_chroma_keeps_parent_skin_directory(self):
        path = "skins/84/84103/84105/84105.fantome"
        index = (self.commit, {path: (self.digest, len(self.data))})
        with patch.object(catalogue, "manifest", return_value=index), patch.object(
                catalogue, "_request", return_value=io.BytesIO(self.data)) as request:
            dest = downloader.mod_path(84, downloader.Target(84103, 84105))
            catalogue.fetch(84, 84103, 84105, dest)
            self.assertTrue(dest.is_file())
            self.assertTrue(request.call_args.args[0].endswith(path))

    def test_offline_uses_cached_catalogue(self):
        cache = self.root / "skins-leagueskins" / ".catalogue.json"
        cache.parent.mkdir()
        cache.write_text(json.dumps({"version": 1, "commit": self.commit,
                                    "files": [[self.path, self.digest, len(self.data)]]}))
        with patch.object(catalogue, "_request", side_effect=OSError("offline")):
            self.assertEqual(catalogue.manifest(), self.index)

    def test_manager_uses_separate_library_and_catalogue_entry_point(self):
        self.assertEqual(library.skins_dir().name, "skins-leagueskins")
        with patch.object(catalogue, "fetch") as fetch, patch.object(
                downloader.skinsmith, "generate") as generate:
            self.assertEqual(downloader.prepare(84, downloader.Target(84102), None), "catalogue")
            fetch.assert_called_once()
            generate.assert_not_called()
        with patch.dict(os.environ, {"TIBBERS_LTK_BACKEND": ""}):
            self.assertEqual(library.skins_dir().name, "skins")

    def test_missing_catalogue_skin_does_not_generate_a_substitute(self):
        with patch.object(catalogue, "manifest", return_value=(self.commit, {})), patch.object(
                downloader.skinsmith, "generate") as generate:
            self.assertIsNone(downloader.prepare(84, downloader.Target(84102), "Akali"))
            generate.assert_not_called()
        self.assertFalse(downloader.mod_path(84, downloader.Target(84102)).exists())

if __name__ == "__main__":
    unittest.main()
