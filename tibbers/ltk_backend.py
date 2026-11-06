"""Choose the Windows skin backend and get its bridge ready before launch.

Tibbers drives an installed LTK Manager through a small bridge DLL of its own
(see docs/ltk-manager-bridge.md). That used to be opt-in through a launcher
script; this module makes it the default when LTK Manager is present, and
leaves the direct patcher backend for machines that do not have it.

``TIBBERS_LTK_BACKEND`` still overrides the choice: ``manager`` asks for the
Manager backend even where auto-detection would say no, ``direct`` keeps the
old one.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("tibbers.backend")

#: Files the bridge needs, in the order they are reported when one is missing.
BRIDGE_FILES = ("ltk_launcher.exe", "LeagueToolFlop.dll")

_ENV = "TIBBERS_LTK_BACKEND"


def explicit_direct() -> bool:
    """True when the user asked for the old backend by name."""
    return (os.environ.get(_ENV) or "").strip().lower() == "direct"


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _manager_installed() -> bool:
    from . import ltk_injector

    try:
        ltk_injector.manager_path()
    except FileNotFoundError:
        return False
    return True


def _directories(tools_dir: Path):
    """Where the bridge may live: the writable tools dir, then the bundle."""
    yield Path(tools_dir) / "ltf"
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        yield base / "tools" / "ltf"
    else:
        yield _root() / "tools" / "ltf"


def bridge_dir(tools_dir: Path):
    """The first directory holding a complete bridge, or None."""
    for directory in _directories(tools_dir):
        if all((directory / name).is_file() for name in BRIDGE_FILES):
            return directory
    return None


def _install_pending(tools_dir: Path) -> None:
    """Apply a staged bridge update, as the launcher script used to."""
    directory = Path(tools_dir) / "ltf"
    pending = directory / "pending"
    if not pending.is_dir():
        return
    directory.mkdir(parents=True, exist_ok=True)
    for name in BRIDGE_FILES:
        source = pending / name
        if not source.is_file():
            continue
        try:
            shutil.copy2(source, directory / name)
            source.unlink()
        except OSError as exc:
            # A running manager or a locked DLL: the current bridge still
            # works, so this is a warning rather than a failed launch.
            log.warning("could not install the staged LTK bridge: %s", exc)
            return
    log.info("installed the staged LTK bridge")


def _build(tools_dir: Path) -> bool:
    """Compile the bridge from the checkout, when this is a checkout."""
    if getattr(sys, "frozen", False):
        return False
    script = _root() / "scripts" / "build_ltf.py"
    if not script.is_file():
        return False
    log.info("building the LTK bridge -- this happens once")
    try:
        result = subprocess.run(
            [sys.executable, str(script)], cwd=str(_root()),
            capture_output=True, text=True, timeout=900,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not build the LTK bridge: %s", exc)
        return False
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        log.warning("could not build the LTK bridge: %s",
                    detail[-1] if detail else f"exit {result.returncode}")
        return False
    return bridge_dir(tools_dir) is not None


def configure(tools_dir: Path) -> bool:
    """Pick the backend for this run and make its bridge ready.

    Sets ``TIBBERS_LTK_BACKEND=manager`` when the Manager backend is used, so
    the rest of the app -- the injector class, the catalogue skin source and
    the library directory -- all answer the same way. Returns whether that
    happened.
    """
    requested = (os.environ.get(_ENV) or "").strip().lower()
    if not sys.platform.startswith("win"):
        return False
    if requested == "direct":
        return False
    if not requested:
        if not _manager_installed():
            log.info("LTK Manager is not installed; using the direct backend")
            return False
        log.info("LTK Manager found; using the Manager backend")

    _install_pending(Path(tools_dir))
    if bridge_dir(Path(tools_dir)) is None and not _build(Path(tools_dir)):
        log.warning("the LTK bridge is missing; using the direct backend "
                    "(run scripts/build_ltf.py to build it)")
        os.environ[_ENV] = "direct"
        return False

    os.environ[_ENV] = "manager"
    return True
