import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tibbers import ltk_backend


def make_bridge(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ltk_backend.BRIDGE_FILES:
        (directory / name).write_bytes(b"bridge")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = self.root / "tools"
        self.mode = patch.dict(os.environ, {}, clear=False)
        self.mode.start()
        self.addCleanup(self.mode.stop)
        os.environ.pop("TIBBERS_LTK_BACKEND", None)
        self.windows = patch.object(ltk_backend.sys, "platform", "win32")
        self.windows.start()
        self.addCleanup(self.windows.stop)
        # The checkout's own tools/ltf must not answer for the temp tools dir.
        self.checkout = patch.object(ltk_backend, "_root", return_value=self.root)
        self.checkout.start()
        self.addCleanup(self.checkout.stop)

    def test_direct_is_kept_when_asked_for(self):
        os.environ["TIBBERS_LTK_BACKEND"] = "direct"
        with patch.object(ltk_backend, "_manager_installed") as installed:
            self.assertFalse(ltk_backend.configure(self.tools))
            installed.assert_not_called()
        self.assertEqual(os.environ["TIBBERS_LTK_BACKEND"], "direct")

    def test_not_windows_never_uses_the_manager(self):
        with patch.object(ltk_backend.sys, "platform", "darwin"), patch.object(
                ltk_backend, "_manager_installed") as installed:
            self.assertFalse(ltk_backend.configure(self.tools))
            installed.assert_not_called()

    def test_autodetect_without_manager_keeps_the_direct_backend(self):
        with patch.object(ltk_backend, "_manager_installed", return_value=False):
            self.assertFalse(ltk_backend.configure(self.tools))
        self.assertNotEqual(os.environ.get("TIBBERS_LTK_BACKEND"), "manager")

    def test_autodetect_with_manager_switches_and_finds_the_bridge(self):
        make_bridge(self.tools / "ltf")
        with patch.object(ltk_backend, "_manager_installed", return_value=True):
            self.assertTrue(ltk_backend.configure(self.tools))
        self.assertEqual(os.environ["TIBBERS_LTK_BACKEND"], "manager")

    def test_a_missing_bridge_can_be_built(self):
        with patch.object(ltk_backend, "_manager_installed", return_value=True), patch.object(
                ltk_backend, "_build", side_effect=lambda tools: make_bridge(Path(tools) / "ltf") or True) as build:
            self.assertTrue(ltk_backend.configure(self.tools))
        build.assert_called_once()
        self.assertEqual(os.environ["TIBBERS_LTK_BACKEND"], "manager")

    def test_no_bridge_and_no_build_falls_back_to_direct(self):
        with patch.object(ltk_backend, "_manager_installed", return_value=True), patch.object(
                ltk_backend, "_build", return_value=False):
            self.assertFalse(ltk_backend.configure(self.tools))
        self.assertNotEqual(os.environ.get("TIBBERS_LTK_BACKEND"), "manager")

    def test_staged_bridge_is_installed_once(self):
        pending = self.tools / "ltf" / "pending"
        make_bridge(pending)
        ltk_backend._install_pending(self.tools)
        for name in ltk_backend.BRIDGE_FILES:
            self.assertTrue((self.tools / "ltf" / name).is_file())
            self.assertFalse((pending / name).exists())

    def test_bundled_bridge_counts_when_the_tools_dir_is_empty(self):
        bundled = self.root / "bundle" / "tools" / "ltf"
        make_bridge(bundled)
        with patch.object(ltk_backend, "_directories", return_value=iter([self.tools / "ltf", bundled])):
            self.assertEqual(ltk_backend.bridge_dir(self.tools), bundled)


if __name__ == "__main__":
    unittest.main()
