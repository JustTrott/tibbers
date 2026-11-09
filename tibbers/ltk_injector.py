"""Opt-in Windows injector using an owned LTK Manager and the LTF bridge.

No game processes are suspended. Manager must be scanning before game launch.
The macOS injector and the legacy Windows backend are unchanged.
"""
from __future__ import annotations

import logging
import os
import re
import json
import uuid
import zipfile
import subprocess
import threading
import time
from pathlib import Path

import psutil

from . import system
from .injector import Injector, InjectionResult
from .ltk_bridge import LtkBridge, BridgeUnavailable

log = logging.getLogger("tibbers.ltk")


def manager_path() -> Path:
    configured = os.environ.get("TIBBERS_LTK_MANAGER")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError(f"TIBBERS_LTK_MANAGER does not exist: {path}")
    for key in ("ProgramW6432", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(key)
        if not base:
            continue
        for folder in ("LTK Manager", "Programs/LTK Manager"):
            for name in ("ltk-manager.exe", "LTK Manager.exe"):
                path = Path(base) / folder / name
                if path.is_file():
                    return path.resolve()
    raise FileNotFoundError("Install LTK Manager or set TIBBERS_LTK_MANAGER to its exe")


class ManagerInjector(Injector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_path = self.work_dir / "ltk-session.json"
        self._session = self._read_session()
        self._bridge = None
        self._operation = threading.RLock()
        self._mods = []
        self._error = None

    def _read_session(self):
        try:
            value = json.loads(self.session_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_session(self):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.session_path.with_suffix(".part")
        tmp.write_text(json.dumps(self._session), encoding="utf-8")
        tmp.replace(self.session_path)

    def _owned_process(self):
        try:
            proc = psutil.Process(self._session["pid"])
            if (proc.create_time() == self._session["created"] and
                    proc.exe().casefold() == self._session["exe"].casefold()):
                return proc
        except (KeyError, psutil.Error):
            pass
        return None

    def _connect(self):
        if not self._owned_process():
            raise BridgeUnavailable("The Tibbers-owned LTK Manager is no longer running")
        if self._bridge is None:
            self._bridge = LtkBridge.wait_for_bridge(
                timeout=30, expected_pid=self._session["pid"])
        return self._bridge

    def _launch(self):
        if self._owned_process():
            return self._connect()
        # A dead manager may have left library changes: retain the journal so
        # the next manager restores them before staging another skin.
        manager = manager_path()
        bundled = Path(__file__).resolve().parents[1] / "tools" / "ltf"
        directory = self.tools_dir / "ltf"
        if not (directory / "ltk_launcher.exe").is_file():
            directory = bundled
        if os.environ.get("TIBBERS_LOCAL_HOST_TEST") == "1":
            directory = bundled.parent / "ltf-test"
        for name in ("ltk_launcher.exe", "LeagueToolFlop.dll"):
            if not (directory / name).is_file():
                raise FileNotFoundError(f"Missing {directory / name}; run scripts/build_ltf.py")
        result = subprocess.run(
            [str(directory / "ltk_launcher.exe"), "--exe", str(manager), "--quiet"],
            cwd=directory, capture_output=True, text=True, timeout=60,
            creationflags=system.CREATE_NO_WINDOW)
        if result.returncode == 5:
            raise RuntimeError("Close LTK Manager first; Tibbers leaves existing instances alone")
        match = re.search(r"LTK_MANAGER_PID=(\d+)", result.stdout)
        if result.returncode or not match:
            raise RuntimeError(f"LTK launcher failed ({result.returncode}): {result.stderr[-500:]}")
        proc = psutil.Process(int(match.group(1)))
        self._session.update(pid=proc.pid, created=proc.create_time(), exe=proc.exe())
        self._save_session()
        self._bridge = None
        bridge = self._connect()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            status = bridge.status()
            if status.get("ok") is False:
                raise RuntimeError(f"LTK bridge status failed: {status.get('error', status)}")
            if status.get("cdp", {}).get("available"):
                return bridge
            time.sleep(0.25)
        raise RuntimeError("LTK Manager's WebView2 did not become ready")

    def _restore(self, bridge):
        """Keep unfinished cleanup journaled; never close before restoration."""
        bridge.stop_patcher()
        # An interrupted import may have completed after the caller lost its
        # answer. Only our exclusively owned, hidden manager is modified here.
        before = self._session.get("import_before")
        if before is not None:
            new = [m["id"] for m in bridge.library() if m["id"] not in before]
            self._session.setdefault("installed", []).extend(new)
            self._session.pop("import_before", None)
            self._save_session()
        for mod_id in list(self._session.get("installed", [])):
            library = {m["id"]: m for m in bridge.library()}
            if mod_id in library:
                bridge.uninstall(mod_id)
            self._session["installed"].remove(mod_id)
            self._save_session()
        for mod_id in list(self._session.get("disabled", [])):
            library = {m["id"]: m for m in bridge.library()}
            if mod_id in library and not library[mod_id].get("enabled"):
                bridge.toggle(mod_id, True)
            self._session["disabled"].remove(mod_id)
            self._save_session()

    def build_overlay(self, fantomes, timeout=300):
        if isinstance(fantomes, (str, os.PathLike)):
            raise TypeError("build_overlay takes a sequence of mods")
        if not self.enabled:
            return InjectionResult(False, "injection is disabled")
        if system.game_pid() is not None:
            return InjectionResult(False, "a game is running; changes apply before the next game")
        if not fantomes:
            return InjectionResult(False, "no mod to build")
        # Preserve the original archives. LTK builds the final overlay itself.
        with self._operation:
            stage = self.work_dir / "manager-mods"
            stage.mkdir(parents=True, exist_ok=True)
            self._mods = []
            try:
                for index, source in enumerate(fantomes):
                    target = stage / f"{index}.fantome"
                    # LTK may identify a mod by metadata. A session-specific
                    # name prevents an import replacing a user's existing mod.
                    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as staged:
                        metadata = json.loads(original.read("META/info.json"))
                        metadata["Name"] = f"Tibbers {uuid.uuid4().hex}"
                        for entry in original.infolist():
                            data = (json.dumps(metadata).encode("utf-8")
                                    if entry.filename == "META/info.json"
                                    else original.read(entry))
                            staged.writestr(entry, data)
                    self._mods.append(target.resolve())
            except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                self._mods = []
                return InjectionResult(False, f"could not stage mod: {exc}")
        return InjectionResult(True, "mods staged for LTK Manager")

    def start_patcher(self, wait=45.0, meta=None):
        if not self.enabled:
            return InjectionResult(False, "injection is disabled")
        if system.game_pid() is not None:
            return InjectionResult(False, "LTK must be armed before the game starts")
        if not self._mods:
            return InjectionResult(False, "no staged mods")
        with self._operation:
            self._error = None
            try:
                bridge = self._launch()
                self._restore(bridge)
                library = bridge.library()
                self._session["disabled"] = [m["id"] for m in library if m.get("enabled")]
                self._session["installed"] = []
                self._save_session()
                for mod_id in self._session["disabled"]:
                    bridge.toggle(mod_id, False)
                for path in self._mods:
                    before = [m["id"] for m in bridge.library()]
                    self._session["import_before"] = before
                    self._save_session()
                    bridge.import_fantome(str(path))
                    added = [m["id"] for m in bridge.library() if m["id"] not in before]
                    self._session["installed"].extend(added)
                    self._session.pop("import_before", None)
                    self._save_session()
                    if len(added) != 1:
                        raise RuntimeError("LTK import did not create exactly one new mod")
                    bridge.toggle(added[0], True)
                self._begin_log()
                if system.game_pid() is not None:
                    raise RuntimeError("The game started before LTK was armed; select the skin earlier next game")
                bridge.start_patcher()
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    status = self.patcher_status()
                    if status["error"]:
                        raise RuntimeError(status["error"])
                    if status["watching"]:
                        self.write_record({**(meta or {}), "backend": "manager",
                                           "pid": self._session["pid"],
                                           "overlay": str(self.overlay_dir)})
                        return InjectionResult(True, "LTK Manager is scanning for the game")
                    time.sleep(0.25)
                raise RuntimeError("LTK did not report scanning before the timeout")
            except Exception as exc:
                self._error = str(exc)
                # During a game keep the manager and files alive for its WAD reads.
                if system.game_pid() is None:
                    try:
                        self.stop_patcher()
                    except Exception:
                        log.exception("LTK cleanup incomplete; journal retained")
                return InjectionResult(False, str(exc))

    def _begin_log(self):
        logs = Path(os.environ.get("APPDATA", "")) / "dev.leaguetoolkit.manager" / "logs"
        files = list(logs.glob("ltk-manager.*.log"))
        self._session["log_offsets"] = {str(p): p.stat().st_size for p in files}
        self._save_session()

    def patcher_status(self):
        status = dict(watching=False, found=False, patched=False,
                      exited=False, error=self._error, tail="")
        if not self.enabled:
            return status
        try:
            answer = self._connect().patcher_status()
            response = answer.get("response", {})
            value = response.get("value", {})
            if answer.get("ok") is False or response.get("ok") is False or not isinstance(value, dict):
                raise RuntimeError(f"LTK status unavailable: {answer}")
            phase = str(value.get("phase", "")).lower()
            status["watching"] = bool(value.get("running")) and phase not in ("", "idle", "building", "failed", "error")
            if phase in ("failed", "error"):
                status["error"] = f"LTK patcher is {phase}"
            logs = Path(os.environ.get("APPDATA", "")) / "dev.leaguetoolkit.manager" / "logs"
            files = list(logs.glob("ltk-manager.*.log"))
            if files:
                path = max(files, key=lambda p: p.stat().st_mtime)
                with open(path, "rb") as stream:
                    offset = self._session.get("log_offsets", {}).get(str(path), 0)
                    stream.seek(offset if path.stat().st_size >= offset else 0)
                    text = stream.read().decode("utf-8", "replace")
                parsed = system.parse_patcher_log(text)
                status.update(found=parsed["found"], patched=parsed["patched"],
                              exited=parsed["exited"], tail=parsed["tail"],
                              lines=parsed.get("lines", []))
                if parsed["error"]:
                    status["error"] = parsed["error"]
        except Exception as exc:
            status["error"] = str(exc)
        # A scan is readiness, never evidence that the skin was applied.
        if status["error"]:
            status["watching"] = False
            status["patched"] = False
        return status

    def is_running(self):
        return self.enabled and self._owned_process() is not None

    def patcher_pid(self):
        return self._session.get("pid") if self.is_running() else None

    def overlay_in_use(self):
        return self.enabled and system.game_pid() is not None

    def adopt(self):
        if not self.is_running():
            return None
        self._adopted = True
        record = self.read_record() or {}
        return {**record, "pid": self._session["pid"], "adopted": True}

    def stop_patcher(self):
        if not self.enabled:
            return
        with self._operation:
            if self._owned_process():
                bridge = self._connect()
                self._restore(bridge)
                bridge.kill()
            elif self._session.get("installed") or self._session.get("disabled") or "import_before" in self._session:
                # Restore a crashed session through a fresh owned manager.
                bridge = self._launch()
                self._restore(bridge)
                bridge.kill()
            proc = self._owned_process()
            if proc:
                # Do not race a new launch against a manager still closing.
                # Preserve ownership/journal when a window refuses to close.
                proc.wait(timeout=10)
            self._bridge = None
            self._session = {}
            self.session_path.unlink(missing_ok=True)
            self.clear_record()
            self._adopted = False
