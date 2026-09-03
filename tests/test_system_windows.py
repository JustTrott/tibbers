#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The Windows platform layer.

Imported directly rather than through `system`, so these run on macOS too --
the Windows paths were written on a Mac and the point is that a change there
is caught without a Windows machine to hand.

Two things here earned a test by having been wrong. `select_modtools` used to
check only for `mod-tools.exe`, but the exe imports `cslol-dll.dll` at load
time and Windows fails the process at the loader when it is missing: no
output, nothing in the patcher log, just a patcher that never appears. And the
holder now has two spellings -- a `-c` script from source, the app re-running
itself when frozen -- which have to agree on where the log path and the
command sit in argv, because each reads them out of a different offset.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import _system_windows as win  # noqa: E402


class SelectModtools(unittest.TestCase):
    """Both halves of the pair, or a message saying which one is missing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tools = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_exe_names_the_exe(self):
        with self.assertRaises(FileNotFoundError) as caught:
            win.select_modtools(self.tools)
        self.assertIn("mod-tools.exe", str(caught.exception))

    def test_missing_dll_is_its_own_error(self):
        (self.tools / "mod-tools.exe").write_bytes(b"")
        with self.assertRaises(FileNotFoundError) as caught:
            win.select_modtools(self.tools)
        message = str(caught.exception)
        self.assertIn("cslol-dll.dll", message)
        # The user has to be told what to do about it, not just what is wrong.
        self.assertIn("fetch_modtools", message)

    def test_the_pair_resolves(self):
        (self.tools / "mod-tools.exe").write_bytes(b"")
        (self.tools / "cslol-dll.dll").write_bytes(b"")
        self.assertEqual(win.select_modtools(self.tools),
                         self.tools / "mod-tools.exe")


class HolderArgv(unittest.TestCase):
    """The two holder spellings stay in step."""

    HOST = "ltk_patcher_host.exe"
    PROTOCOL = ["config flags 4", "config prefix OVERLAY\\", "start scan"]

    def argv(self, frozen: bool):
        was = getattr(sys, "frozen", None)
        sys.frozen = frozen  # type: ignore[attr-defined]
        try:
            return win.holder_argv(Path("OVERLAY"), Path("LOG"),
                                   Path(self.HOST), self.PROTOCOL)
        finally:
            if was is None:
                del sys.frozen  # type: ignore[attr-defined]
            else:
                sys.frozen = was  # type: ignore[attr-defined]

    def test_from_source_runs_the_script(self):
        argv = self.argv(frozen=False)
        self.assertEqual(argv[1], "-c")
        self.assertEqual(argv[2], win._HOLDER_SCRIPT)

    def test_frozen_reruns_the_app(self):
        argv = self.argv(frozen=True)
        # No `-c`: sys.executable is the app, which would otherwise start a
        # second copy of tibbers instead of holding a pipe.
        self.assertNotIn("-c", argv)
        self.assertEqual(argv[1], win.HOLDER_FLAG)

    def test_both_carry_what_discovery_matches_on(self):
        for frozen in (False, True):
            argv = self.argv(frozen=frozen)
            joined = " ".join(argv)
            self.assertIn(win.HOLDER_MARK, joined, f"frozen={frozen}")
            self.assertIn("OVERLAY", joined, f"frozen={frozen}")
            self.assertIn(self.HOST, joined, f"frozen={frozen}")

    def test_the_two_forms_agree_on_argv_offsets(self):
        """The `-c` script reads log at argv[3], host at [4], protocol at [5:];
        `hold_patcher` gets the same one slot earlier (its argv has no `-c`).
        Drift here arms the wrong host, or logs to the wrong file."""
        source = self.argv(frozen=False)
        frozen = self.argv(frozen=True)

        # What the -c script sees as its own sys.argv: "-c" then the tail.
        script_argv = ["-c", *source[3:]]
        self.assertEqual(script_argv[3], "LOG")
        self.assertEqual(script_argv[4], self.HOST)
        self.assertEqual(script_argv[5:], self.PROTOCOL)

        # What main.py hands hold_patcher: everything after the flag.
        held = frozen[2:]
        self.assertEqual(held[2], "LOG")
        self.assertEqual(held[3], self.HOST)
        self.assertEqual(held[4:], self.PROTOCOL)


class ProcessTableCost(unittest.TestCase):
    """argv is read for candidates only, never for the whole machine.

    Reading `cmdline` costs ~2s across a desktop's worth of processes on
    Windows, against ~1ms for the names, and this table sits on the path the
    picker polls -- so a regression here is not a slow function, it is a
    visibly laggy app. The shape of the scan is the thing worth pinning.
    """

    class FakeProc:
        def __init__(self, pid, name, argv, counter):
            self.pid = pid
            self.info = {"pid": pid, "name": name}
            self._argv = argv
            self._counter = counter

        def cmdline(self):
            self._counter.append(self.pid)
            return self._argv

    def setUp(self):
        self.cmdline_reads = []
        noise = [self.FakeProc(i, f"noise{i}.exe", ["noise"], self.cmdline_reads)
                 for i in range(200)]
        # The LTK host has a bare argv -- it is driven over stdin, so the
        # overlay is not on its command line.
        self.patcher = self.FakeProc(
            900, win.LTK_HOST, [win.LTK_HOST], self.cmdline_reads)
        # The holder names the host and carries the mark and overlay.
        self.holder = self.FakeProc(
            901, "python.exe",
            ["python.exe", "-c", "...", win.HOLDER_MARK, "OV", win.LTK_HOST],
            self.cmdline_reads)
        self.fake = noise + [self.patcher, self.holder]

        real_iter = win.psutil.process_iter
        win.psutil.process_iter = lambda attrs=None: iter(self.fake)
        self.addCleanup(setattr, win.psutil, "process_iter", real_iter)
        win._TABLE_CACHE = (0.0, [])
        self.addCleanup(setattr, win, "_TABLE_CACHE", (0.0, []))

    def test_argv_is_read_only_for_candidates(self):
        win._read_process_table()
        self.assertEqual(sorted(self.cmdline_reads), [900, 901])

    def test_the_rows_it_does_return_are_the_useful_ones(self):
        rows = dict(win._read_process_table())
        self.assertIn(win.LTK_HOST, rows[900])
        self.assertIn(win.HOLDER_MARK, rows[901])

    def test_discovery_still_finds_both(self):
        # Unscoped (no overlay): the host by name, the holder by mark.
        self.assertEqual(win.runoverlay_pids(fresh=True), [900])
        self.assertEqual(win.holder_pids(fresh=True), [901])

    def test_a_holder_is_not_counted_as_a_patcher(self):
        # The holder's argv names the host too, so a scan that did not exclude
        # the mark would report two patchers for one game.
        self.assertNotIn(901, win.runoverlay_pids(fresh=True))


class InstallShape(unittest.TestCase):
    """Riot's Windows layout: the client at the root, the game one level in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "League of Legends"
        (self.root / "Game" / "DATA" / "FINAL").mkdir(parents=True)
        (self.root / "LeagueClient.exe").write_bytes(b"")
        (self.root / "Game" / "League of Legends.exe").write_bytes(b"")
        self.addCleanup(self.tmp.cleanup)

    def test_recognises_both_halves(self):
        self.assertTrue(win.is_client_dir(self.root))
        self.assertTrue(win.is_game_dir(self.root / "Game"))

    def test_a_client_dir_is_not_a_game_dir(self):
        # The two names differ only by the space Riot puts in the game exe,
        # so a check that confused them would still pass on the client.
        self.assertFalse(win.is_game_dir(self.root))

    def test_data_final_is_required(self):
        import shutil
        shutil.rmtree(self.root / "Game" / "DATA")
        self.assertFalse(win.is_game_dir(self.root / "Game"))


class NoElevation(unittest.TestCase):
    def test_windows_does_not_elevate(self):
        self.assertFalse(win.INJECTION_NEEDS_ROOT)

    def test_manual_command_names_the_ltk_host(self):
        line = win.manual_command(Path("mod-tools.exe"), Path("OV"),
                                  Path("CFG"), Path(r"C:\Game"))
        self.assertNotIn("sudo", line)
        self.assertIn("runoverlay", line)
        self.assertIn(win.LTK_HOST, line)


class SelectPatcher(unittest.TestCase):
    """The LTK host and its DLL are a pair, like mod-tools and cslol-dll."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tools = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_host_is_reported(self):
        with self.assertRaises(FileNotFoundError) as caught:
            win.select_patcher(self.tools)
        self.assertIn(win.LTK_HOST, str(caught.exception))
        self.assertIn("fetch_modtools", str(caught.exception))

    def test_missing_dll_is_its_own_error(self):
        (self.tools / win.LTK_HOST).write_bytes(b"")
        with self.assertRaises(FileNotFoundError) as caught:
            win.select_patcher(self.tools)
        self.assertIn(win.LTK_DLL, str(caught.exception))

    def test_the_pair_resolves(self):
        (self.tools / win.LTK_HOST).write_bytes(b"")
        (self.tools / win.LTK_DLL).write_bytes(b"")
        self.assertEqual(win.select_patcher(self.tools),
                         self.tools / win.LTK_HOST)


class PatcherProtocol(unittest.TestCase):
    """What the holder writes into the host's stdin to arm it."""

    def test_it_opts_out_and_starts_scanning(self):
        lines = win.patcher_protocol(Path(r"C:\overlay"))
        self.assertIn(f"config flags {win.LTK_OPT_OUT_AH_V1}", lines)
        self.assertEqual(lines[-1], "start scan")

    def test_prefix_gets_a_trailing_separator(self):
        # LTK's `config prefix` requires it; a path without one silently
        # resolves the wrong directory.
        lines = win.patcher_protocol(Path(r"C:\overlay"))
        prefix = next(x for x in lines if x.startswith("config prefix "))
        self.assertTrue(prefix.endswith(("\\", "/")), prefix)

    def test_an_already_terminated_prefix_is_not_doubled(self):
        lines = win.patcher_protocol("C:/overlay/")
        prefix = next(x for x in lines if x.startswith("config prefix "))
        self.assertFalse(prefix.endswith("//"))


class PatcherLog(unittest.TestCase):
    """Reading LTK's host/DLL output into the fields the app watches."""

    def test_scanning_is_watching(self):
        s = win.parse_patcher_log("status 0.1 injecting scanning for game")
        self.assertTrue(s["watching"])
        self.assertFalse(s["found"])
        self.assertIsNone(s["error"])

    def test_a_served_overlay_is_patched(self):
        s = win.parse_patcher_log(
            "status 1 injected dll attached\n"
            "INFO ltk_patcher_dll::verify: overlay verified 1 wad(s)\n"
            "INFO redirected wad: DATA/FINAL/Champions/Smolder.wad.client")
        self.assertTrue(s["found"] or s["patched"])
        self.assertTrue(s["patched"])

    def test_the_opted_out_warning_is_not_an_error(self):
        # OPT_OUT_AH_V1 turns the base-skin check into this WARN; treating it
        # as an error would report every successful skin as a failure.
        s = win.parse_patcher_log(
            "WARN ltk_patcher_dll::verify: AH wad scan failed c0000229 for "
            "smolder (opted out): skin0 is another skin\n"
            "INFO ltk_patcher_dll::verify: overlay verified 1 wad(s)")
        self.assertIsNone(s["error"])
        self.assertTrue(s["patched"])

    def test_a_real_error_is_surfaced(self):
        s = win.parse_patcher_log(
            "ERROR ltk_patcher_host::worker: could not open overlay")
        self.assertIsNotNone(s["error"])
        self.assertIn("could not open overlay", s["error"])


if __name__ == "__main__":
    unittest.main()


class InstanceMutex(unittest.TestCase):
    """The app and the installer must agree on the mutex name: the installer
    waits on it before touching a file (PrepareToInstall in tibbers.iss)."""

    def test_the_installer_waits_on_the_same_mutex(self):
        iss = (Path(__file__).resolve().parent.parent / "scripts"
               / "tibbers.iss").read_text(encoding="utf-8")
        self.assertIn(f"AppMutex = '{win.INSTANCE_MUTEX}';", iss)
        self.assertIn("CheckForMutexes(AppMutex)", iss)
        self.assertIn("WantsRelaunch", iss)

    @unittest.skipUnless(sys.platform.startswith("win"), "a Windows mutex")
    def test_a_second_claim_in_this_process_sees_the_first(self):
        self.assertTrue(win.claim_instance())
        # Same process, same mutex: CreateMutex reports it already exists.
        self.assertFalse(win.claim_instance())

