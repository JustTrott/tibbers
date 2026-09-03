#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
No subprocess may open a console window.

The packaged Windows app is windowed and has no console, so any console
program it starts without CREATE_NO_WINDOW gets a fresh console of its own --
on Windows 11, a Windows Terminal window over champ select. That has now
happened three times (mkoverlay, the patcher host, the update script), each
in a different file, each a call site that simply forgot. So it is checked
here, statically, for every spawn in the package: each `subprocess.run` /
`Popen` / `check_output` / `check_call` / `call` reachable on Windows must
pass `creationflags=`. The macOS-only modules are exempt by name, and so is
any call of a POSIX binary (`/usr/bin/...`, `/bin/...`): those lines cannot
run on Windows at all.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "main.py", *sorted((ROOT / "tibbers").glob("*.py"))]
MACOS_ONLY = {"_system_macos.py", "_shell_macos.py", "privileged.py"}

POSIX_BINARY = re.compile(r'"/(?:usr/)?bin/')
SPAWN = re.compile(r"subprocess\.(?:run|Popen|check_output|check_call|call)\(")


def calls(text: str):
    """Each spawn as (line number, the call's argument text)."""
    for match in SPAWN.finditer(text):
        depth, i = 1, match.end()
        while depth and i < len(text):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        yield text.count("\n", 0, match.start()) + 1, text[match.end():i - 1]


class EverySpawnIsWindowless(unittest.TestCase):
    def test_every_spawn_passes_creationflags(self):
        missing = []
        for path in SOURCES:
            if path.name in MACOS_ONLY:
                continue
            text = path.read_text(encoding="utf-8")
            for line, args in calls(text):
                if POSIX_BINARY.search(args):
                    continue
                if "creationflags" not in args:
                    missing.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(missing, [], "spawns without creationflags: "
                         + ", ".join(missing))

    def test_the_scan_sees_the_spawns_it_should(self):
        # The guard is only worth having if it finds the known call sites.
        seen = {p.name for p in SOURCES
                if p.name not in MACOS_ONLY
                and any(True for _ in calls(p.read_text(encoding="utf-8")))}
        for name in ("injector.py", "update.py", "_system_windows.py",
                     "wintools.py", "ugg.py"):
            self.assertIn(name, seen)


if __name__ == "__main__":
    unittest.main()
