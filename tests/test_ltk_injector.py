"""Session integration tests. Never launch LTK or attach to a game."""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tibbers import ltk_injector as ltk


class FakeBridge:
    def __init__(self):
        self.mods = {"personal": {"id": "personal", "enabled": True}}
        self.calls = []
        self.running = False
        self.fail_import = False

    def library(self):
        return [dict(m) for m in self.mods.values()]

    def toggle(self, mod_id, enabled):
        self.calls.append(("toggle", mod_id, enabled))
        self.mods[mod_id]["enabled"] = enabled

    def stop_patcher(self):
        self.calls.append(("stop",))
        self.running = False

    def start_patcher(self):
        self.calls.append(("start",))
        self.running = True

    def patcher_status(self):
        return {"ok": True, "response": {"ok": True, "value": {
            "running": self.running, "phase": "scanning" if self.running else "idle"}}}

    def import_fantome(self, path):
        # Simulate an install that finishes but whose response is lost.
        self.mods["imported"] = {"id": "imported", "enabled": False}
        if self.fail_import:
            raise RuntimeError("import response lost")

    def uninstall(self, mod_id):
        self.calls.append(("uninstall", mod_id))
        self.mods.pop(mod_id)

    def kill(self):
        self.calls.append(("kill",))


class ManagerSession(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = mock.patch.object(ltk.system, "game_pid", return_value=None)
        self.game = patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"APPDATA": str(self.root / "appdata")})
        env.start()
        self.addCleanup(env.stop)
        self.inject = ltk.ManagerInjector(self.root / "game", self.root / "tools", self.root / "work")
        self.bridge = FakeBridge()
        self.inject._connect = mock.Mock(return_value=self.bridge)
        self.inject._launch = mock.Mock(return_value=self.bridge)
        self.inject._owned_process = mock.Mock(return_value=mock.Mock())
        self.inject._session = {"pid": 100, "created": 20, "exe": "ltk-manager.exe"}
        self.mod = self.root / "skin.fantome"
        with zipfile.ZipFile(self.mod, "w") as archive:
            archive.writestr("META/info.json", json.dumps({"Name": "My skin", "Author": "tibbers"}))
            archive.writestr("WAD/Ahri.wad.client", b"wad payload")

    def stage(self):
        self.assertTrue(self.inject.build_overlay([self.mod]).ok)

    def test_stage_keeps_wad_bytes_and_uses_unique_metadata(self):
        self.stage()
        with zipfile.ZipFile(self.inject._mods[0]) as staged:
            self.assertEqual(staged.read("WAD/Ahri.wad.client"), b"wad payload")
            self.assertTrue(json.loads(staged.read("META/info.json"))["Name"].startswith("Tibbers "))
        with zipfile.ZipFile(self.mod) as original:
            self.assertEqual(json.loads(original.read("META/info.json"))["Name"], "My skin")

    def test_ready_is_not_patched_and_cleanup_restores_personal_mod(self):
        self.stage()
        self.assertTrue(self.inject.start_patcher(meta={"skinId": 103001}).ok)
        self.assertFalse(self.bridge.mods["personal"]["enabled"])
        self.assertTrue(self.bridge.mods["imported"]["enabled"])
        status = self.inject.patcher_status()
        self.assertTrue(status["watching"])
        self.assertFalse(status["patched"])
        self.assertEqual(self.inject.read_record()["skinId"], 103001)
        self.inject.stop_patcher()
        self.assertEqual(self.bridge.mods, {"personal": {"id": "personal", "enabled": True}})
        self.assertEqual(self.bridge.calls[-1], ("kill",))
        self.assertFalse(self.inject.session_path.exists())

    def test_lost_import_response_is_reconciled_before_restoring(self):
        self.stage()
        self.bridge.fail_import = True
        result = self.inject.start_patcher()
        self.assertFalse(result.ok)
        self.assertIn("response lost", result.message)
        self.assertNotIn("imported", self.bridge.mods)
        self.assertTrue(self.bridge.mods["personal"]["enabled"])

    def test_cleanup_failure_keeps_journal_and_does_not_close_manager(self):
        self.inject._session.update(installed=["imported"], disabled=["personal"])
        self.bridge.mods["imported"] = {"id": "imported", "enabled": True}
        self.inject._save_session()
        self.bridge.uninstall = mock.Mock(side_effect=RuntimeError("busy"))
        with self.assertRaisesRegex(RuntimeError, "busy"):
            self.inject.stop_patcher()
        self.assertTrue(self.inject.session_path.exists())
        self.assertNotIn(("kill",), self.bridge.calls)

    def test_disabled_instance_never_sends_commands(self):
        self.inject.enabled = False
        self.assertFalse(self.inject.build_overlay([self.mod]).ok)
        self.assertFalse(self.inject.start_patcher().ok)
        self.inject.stop_patcher()
        self.assertIsNone(self.inject.adopt())
        self.inject.patcher_status()
        self.inject._connect.assert_not_called()
        self.inject._launch.assert_not_called()

    def test_live_game_rejects_staging_and_launch(self):
        self.game.return_value = 42
        self.assertFalse(self.inject.build_overlay([self.mod]).ok)
        self.assertFalse(self.inject.start_patcher().ok)
        self.inject._launch.assert_not_called()

    def test_fresh_logs_report_host_refusal_even_with_a_running_bridge(self):
        self.stage()
        self.inject._begin_log()
        logs = self.root / "appdata/dev.leaguetoolkit.manager/logs"
        logs.mkdir(parents=True)
        (logs / "ltk-manager.today.log").write_text(
            "INFO overlay verified 2 wad(s)\n"
            "ERROR host verification failed, refusing overlay: [VRF00000007]\n")
        self.bridge.running = True
        status = self.inject.patcher_status()
        self.assertIn("VRF", status["error"])
        self.assertFalse(status["watching"])
        self.assertFalse(status["patched"])

    def test_old_log_success_is_ignored(self):
        logs = self.root / "appdata/dev.leaguetoolkit.manager/logs"
        logs.mkdir(parents=True)
        path = logs / "ltk-manager.today.log"
        path.write_text("INFO overlay verified 1 wad(s)\n")
        self.inject._begin_log()
        self.assertFalse(self.inject.patcher_status()["patched"])
        with path.open("a") as stream:
            stream.write("INFO redirected wad: new.wad.client\n")
        self.assertTrue(self.inject.patcher_status()["patched"])

    def test_session_can_be_adopted_without_restarting_or_importing(self):
        self.inject._save_session()
        self.inject.write_record({"skinId": 103001})
        record = self.inject.adopt()
        self.assertEqual(record["skinId"], 103001)
        self.assertTrue(record["adopted"])
        self.inject._launch.assert_not_called()
        self.assertEqual(self.bridge.calls, [])

    def test_reused_pid_is_not_our_manager(self):
        # Bypass the fixture's mock to exercise the real ownership check.
        proc = mock.Mock()
        proc.create_time.return_value = 21  # journal recorded 20
        with mock.patch.object(ltk.psutil, "Process", return_value=proc):
            self.assertIsNone(ltk.ManagerInjector._owned_process(self.inject))


class StopPatcherResponse(unittest.TestCase):
    def test_already_stopped_is_success(self):
        bridge = ltk.LtkBridge(port=46321)
        with mock.patch.object(bridge, "_ok", side_effect=ltk.BridgeUnavailable("offline")):
            with self.assertRaises(ltk.BridgeUnavailable):
                bridge.stop_patcher()
        from tibbers.ltk_bridge import BridgeCommandError
        with mock.patch.object(bridge, "_ok", side_effect=BridgeCommandError(
                "stop_patcher failed: PATCHER NOT_RUNNING: The patcher is not running")):
            self.assertEqual(bridge.stop_patcher(), {"ok": True, "already_stopped": True})

    def test_other_stop_errors_still_fail(self):
        from tibbers.ltk_bridge import BridgeCommandError
        bridge = ltk.LtkBridge(port=46321)
        with mock.patch.object(bridge, "_ok", side_effect=BridgeCommandError(
                "stop_patcher failed: PATCHER ACCESS_DENIED: denied")):
            with self.assertRaises(BridgeCommandError):
                bridge.stop_patcher()


if __name__ == "__main__":
    unittest.main()
