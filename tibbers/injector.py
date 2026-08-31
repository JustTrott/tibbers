#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skin injection: build an overlay, then let cslol's patcher hook the game.

Two stages, matching cslol's `mod-tools`:

  mkoverlay   Pure local file work. Reads the game's WADs, merges the chosen
              mod, writes a replacement WAD into this tool's own directory.
              Touches nothing in the League install and needs no privileges.

  runoverlay  Hooks fopen in the game so reads of `.wad.client` are redirected
              into the overlay. Needs root (task_for_pid), and is the only
              elevated step.

Both run *before* the game starts. runoverlay has its own wait loop --

    for (;;) {
        pid = FindPid("/LeagueofLegends");
        if (!pid) { M_WAIT_START; sleep_ms(10); continue; }
        M_FOUND; scan(process); M_PATCH; patch(process);
    }

-- so it polls for the game every 10ms and patches it at a moment of its own
choosing. The tool must therefore be started early and then left alone.

In particular the game is NOT suspended. Freezing it at spawn (the approach
Windows tools use, where a different injection mechanism applies) means the
patcher scans a process that has not finished loading, computes wrong offsets,
and writes the hook over the wrong addresses -- which crashes the game the
moment it resumes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import system

log = logging.getLogger("tibbers.inject")

#: Status strings the patcher prints; see patcher.hpp STATUS_MSG.
PATCHER_READY = "Waiting for league match to start"
PATCHER_FOUND = "Found League"
PATCHER_PATCHING = "Patching"
PATCHER_EXITED = "League exited"


@dataclass
class InjectionResult:
    ok: bool
    message: str
    seconds: float = 0.0


class Disabled(RuntimeError):
    """Raised when injection is switched off for this instance."""


class Injector:
    def __init__(self, game_dir: Path, tools_dir: Path, work_dir: Path,
                 enabled: bool = True):
        self.game_dir = Path(game_dir)
        self.tools_dir = Path(tools_dir)
        self.work_dir = Path(work_dir)
        self.mods_dir = self.work_dir / "mods"
        self.overlay_dir = self.work_dir / "overlay"
        self.patcher_log = self.work_dir / "runoverlay.log"
        #: What the running patcher was started for. Survives the app, so a
        #: restart can work out what it is looking at instead of guessing.
        self.record_path = self.work_dir / "patcher.json"
        #: False in a dev instance. Every path that would touch the overlay,
        #: the game, or root checks this first -- a flag that only the caller
        #: honours is not a guard.
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._adopted = False

    # -- what the running patcher is for -----------------------------------
    #
    # The patcher now outlives the app, so the app can come back to a patcher
    # it did not start. Everything needed to describe that patcher -- and to
    # decide whether it may be disturbed -- is written down when it starts.

    def read_record(self) -> Optional[dict]:
        try:
            data = json.loads(self.record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def write_record(self, record: dict) -> None:
        try:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.record_path.with_suffix(".json.part")
            tmp.write_text(json.dumps(record, indent=2))
            tmp.replace(self.record_path)
        except OSError as exc:
            log.debug("could not write the patcher record: %s", exc)

    def clear_record(self) -> None:
        try:
            self.record_path.unlink(missing_ok=True)
        except OSError:
            pass

    def adopt(self) -> Optional[dict]:
        """Take over a patcher left running by an earlier run of the app.

        Returns its record, or None if there is nothing to adopt. Crucially
        this does NOT touch the overlay directory: the adopted patcher is
        serving out of it, and rebuilding it underneath a patcher that has
        already hooked a live game is the one thing that corrupts a game.
        """
        if not self.enabled:
            return None
        pids = system.runoverlay_pids(self.overlay_dir)
        if not pids:
            # Nothing of ours is running. A record left over from a patcher
            # that has since exited is worse than no record.
            if self.read_record() is not None:
                self.clear_record()
            return None

        record = self.read_record() or {}
        record.update({"pid": pids[0], "adopted": True,
                       "overlay": str(self.overlay_dir)})
        self.write_record(record)
        self._adopted = True
        log.info("adopted a running patcher (pid %d) for %s",
                 pids[0], self.overlay_dir)
        return record

    def adopted(self) -> bool:
        return self._adopted

    def overlay_in_use(self) -> bool:
        """Whether rebuilding the overlay would pull it out from under a game.

        A patcher waiting in champ select can have its overlay swapped freely
        -- that is how changing your mind about a skin works. Once the GAME is
        up, the same rebuild deletes files the patcher is redirecting reads
        into, and the game is the one holding the consequences.
        """
        if not self.enabled:
            return False
        return bool(system.runoverlay_pids(self.overlay_dir)) and \
            system.game_pid() is not None

    # -- overlay construction ---------------------------------------------

    def _reset_dirs(self) -> None:
        for d in (self.mods_dir, self.overlay_dir):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)

    def _extract(self, fantome: Path) -> str:
        """Unpack a .fantome/.zip into the mods directory; return its name."""
        name = Path(fantome).stem
        dest = self.mods_dir / name
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(fantome) as zf:
            for member in zf.namelist():
                target = (dest / member).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise ValueError(f"unsafe path in mod archive: {member}")
            zf.extractall(dest)
        return name

    def build_overlay(self, fantome: Path, timeout: int = 300) -> InjectionResult:
        """Run mkoverlay. Needs no game running and no privileges."""
        if not self.enabled:
            return InjectionResult(
                False, "injection is disabled for this instance")
        if self.overlay_in_use():
            # Refused here rather than at the call site, so no caller can get
            # this wrong: _reset_dirs() is two lines away and it deletes the
            # directory a live patcher is serving a live game from.
            return InjectionResult(
                False, "a patcher is serving a running game from this overlay "
                       "-- refusing to rebuild it underneath")
        started = time.time()
        self._reset_dirs()

        try:
            mod_name = self._extract(Path(fantome))
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            return InjectionResult(False, f"could not read mod: {exc}")

        modtools = system.select_modtools(self.tools_dir)
        cmd = [
            str(modtools), "mkoverlay",
            str(self.mods_dir), str(self.overlay_dir),
            f"--game:{self.game_dir}",
            f"--mods:{mod_name}",
            "--noTFT", "--ignoreConflict",
        ]
        log.info("mkoverlay: %s", " ".join(cmd))

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return InjectionResult(False, f"mkoverlay timed out after {timeout}s")

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return InjectionResult(
                False, f"mkoverlay failed: {tail[-1] if tail else proc.returncode}")

        wads = list(self.overlay_dir.rglob("*.wad.client"))
        if not wads:
            return InjectionResult(False, "mkoverlay produced no overlay WAD")

        elapsed = time.time() - started
        log.info("overlay built in %.2fs (%d wad)", elapsed, len(wads))
        return InjectionResult(True, "overlay ready", elapsed)

    # -- the patcher -------------------------------------------------------

    def is_running(self) -> bool:
        """Whether a runoverlay process is alive for THIS overlay.

        Scoped to the overlay so a dev instance never mistakes the real
        instance's patcher for its own, and never stops it.

        The process scan goes through `ps`, not psutil: the patcher runs as
        root, and an ordinary user is denied its executable path and its
        cmdline through libproc, so psutil reports it as though it were not
        there at all.
        """
        if not self.enabled:
            return False
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            return True
        return bool(system.runoverlay_pids(self.overlay_dir))

    def patcher_pid(self) -> Optional[int]:
        pids = system.runoverlay_pids(self.overlay_dir)
        return pids[0] if pids else None

    def start_patcher(self, wait: float = 12.0,
                      meta: Optional[dict] = None) -> InjectionResult:
        """Start runoverlay and wait until it reports it is watching.

        Raises one authorization prompt. The patcher then runs until stopped,
        waiting for the game on its own -- and, since it is started detached,
        keeps waiting across a restart of this app.
        """
        if not self.enabled:
            return InjectionResult(
                False, "injection is disabled for this instance")
        if self.is_running():
            return InjectionResult(True, "patcher already running")

        config = self.overlay_dir / "cslol-config.json"
        modtools = system.select_modtools(self.tools_dir)

        try:
            self.patcher_log.unlink(missing_ok=True)
        except OSError:
            pass

        log.info("runoverlay (elevated). Equivalent manual command:")
        log.info("  %s", system.manual_command(
            modtools, self.overlay_dir, config, self.game_dir))

        started = time.time()
        try:
            proc = system.spawn_runoverlay_detached(
                modtools, self.overlay_dir, config, self.game_dir,
                self.patcher_log,
            )
            with self._lock:
                self._proc = proc
        except Exception as exc:  # noqa: BLE001
            return InjectionResult(False, f"could not start patcher: {exc}")

        # runoverlay dies instantly if its stdin is at EOF, so a process that
        # has already exited means the pipe was not held, not that the user
        # refused anything.
        if proc is not None and proc.poll() is not None:
            return InjectionResult(
                False, f"patcher exited immediately (code {proc.returncode}); "
                       f"see {self.patcher_log}")

        def remember() -> None:
            """Write down what this patcher is for, for the next app start."""
            record = {
                "overlay": str(self.overlay_dir),
                "config": str(config),
                "gameDir": str(self.game_dir),
                "log": str(self.patcher_log),
                "arch": system.modtools_arch(modtools),
                "startedAt": time.time(),
                "startedBy": os.getpid(),
                # Freshly read: the patcher is seconds old, and the cached
                # process table predates it.
                "pid": (system.runoverlay_pids(self.overlay_dir, fresh=True)
                        or [None])[0],
                "holders": system.holder_pids(self.overlay_dir),
                "gamePid": system.game_pid(),
                "adopted": False,
            }
            record.update(meta or {})
            self.write_record(record)

        # Confirm it is actually watching rather than assuming success.
        deadline = time.time() + wait
        while time.time() < deadline:
            status = self.patcher_status()
            if status["watching"] or status["found"]:
                remember()
                return InjectionResult(
                    True, "patcher watching for the game", time.time() - started)
            if status["error"]:
                return InjectionResult(False, f"patcher error: {status['error']}")
            time.sleep(0.25)

        if self.is_running():
            remember()
            return InjectionResult(True, "patcher started (no status yet)",
                                   time.time() - started)
        return InjectionResult(
            False, "patcher did not start (authorization cancelled?)")

    def patcher_status(self) -> dict:
        """Parse what runoverlay has reported so far."""
        try:
            text = self.patcher_log.read_text(errors="replace")
        except OSError:
            return {"watching": False, "found": False, "patched": False,
                    "error": None, "tail": ""}

        error = None
        for line in text.splitlines():
            if re.search(r"error|failed|exception|throw", line, re.I):
                if "Waiting" not in line:
                    error = line.strip()

        return {
            "watching": PATCHER_READY in text,
            "found": PATCHER_FOUND in text,
            "patched": PATCHER_PATCHING in text,
            "exited": PATCHER_EXITED in text,
            "error": error,
            "tail": "\n".join(text.splitlines()[-6:]),
        }

    def stop_patcher(self) -> None:
        """Stop runoverlay, and the holder keeping its stdin open.

        Closing its stdin is the shutdown it was built for -- it exits(0) on
        EOF. That only works when this process holds the pipe, which it
        deliberately no longer does; the elevated pkill through the helper is
        now the normal path, and the holder is reaped after it so no `sleep`
        is left behind waiting on a pipeline that will never finish.
        """
        if not self.enabled:
            return
        with self._lock:
            proc = self._proc
            self._proc = None
        self._adopted = False

        if proc is not None and proc.poll() is None and proc.stdin is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.terminate()
                except OSError:
                    pass

        if self.is_running():
            try:
                system.kill_runoverlay()
            except Exception as exc:  # noqa: BLE001
                log.debug("could not stop patcher: %s", exc)

        try:
            system.kill_holders(self.overlay_dir)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not stop the patcher holder: %s", exc)
        self.clear_record()

    # -- convenience -------------------------------------------------------

    def prepare(self, fantome: Path, progress=None,
                meta: Optional[dict] = None) -> InjectionResult:
        """Build the overlay and start the patcher, ready for the next game."""
        def report(msg: str) -> None:
            # progress() is state.say, which logs too -- logging here as well
            # duplicates every line in the terminal.
            if progress:
                progress(msg)
            else:
                log.info(msg)

        if not self.enabled:
            report("injection is disabled for this instance -- nothing armed")
            return InjectionResult(False, "injection is disabled")

        report("building overlay...")
        built = self.build_overlay(fantome)
        if not built.ok:
            return built
        report(f"overlay ready ({built.seconds:.1f}s)")

        report("starting patcher (authorization required)...")
        started = self.start_patcher(meta=meta)
        if not started.ok:
            return started

        report("patcher is watching -- start your game")
        return InjectionResult(True, "ready", built.seconds + started.seconds)
