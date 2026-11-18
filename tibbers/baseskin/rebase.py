#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rebase a downloaded skin mod onto the base skin, once, with a sidecar.

``fix_mod`` rewrites a ``.fantome`` in place, so afterwards the file no longer
matches the catalogue manifest's hash. The sidecar (``<mod>.baseskin.json``)
records which manifest commit the mod was downloaded for and which game WADs it
was rebased against, so ``skin_catalogue.fetch`` can tell a rebased copy of the
current commit from a stale download, and so the rebase is not run again until
the game patches.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .package import FixResult, fix_mod
from .steps import ALL_STEPS

log = logging.getLogger("tibbers.baseskin.rebase")

SIDECAR = ".baseskin.json"
#: Bumped when the steps write something else: an already-rebased mod is then
#: re-rebased (the same role as Rose's STEPS_VERSION).
STEPS_VERSION = 1
#: The steps Tibbers runs. ``mesh`` and ``name`` are what defeat the patcher's
#: base-skin check ("skin0 skeleton is one of ... other skins' meshes"); the
#: audio steps rebase the skin's sounds onto the base identity, and need the
#: sound-copy step at injection (skin_sounds.build, wired in ltk_injector).
STEPS = ALL_STEPS


def sidecar_path(mod_path) -> Path:
    return Path(mod_path).with_name(Path(mod_path).name + SIDECAR)


def read_sidecar(mod_path: Path) -> dict:
    try:
        data = json.loads(sidecar_path(mod_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sidecar_commit(mod_path: Path) -> Optional[str]:
    """The manifest commit the mod was last rebased for, or None."""
    commit = read_sidecar(mod_path).get("commit")
    return commit if isinstance(commit, str) else None


def _game_unchanged(stamps, champions_dir: Path) -> bool:
    """Whether every game WAD *stamps* names still has those bytes and mtime."""
    if not isinstance(stamps, dict) or not stamps:
        return False
    for name, stamp in stamps.items():
        if not (isinstance(stamp, list) and len(stamp) == 2):
            return False
        try:
            stat = (champions_dir / name).stat()
        except OSError:
            return False
        if stat.st_size != stamp[0] or stat.st_mtime_ns != stamp[1]:
            return False
    return True


def rebase_mod(mod_path: Path, game_dir: Path, commit: str) -> Optional[FixResult]:
    """Rebase *mod_path* in place when its recorded state is not current.

    Returns the FixResult when the mod was (re)written, None when it already
    matches (the sidecar says this commit, this steps version and this game).
    Raises OSError/ValueError for a mod that cannot be read or written; a step
    that cannot run is skipped by fix_mod, which reports ``complete`` False.
    """
    mod_path = Path(mod_path)
    game_dir = Path(game_dir)
    champions_dir = game_dir / "DATA" / "FINAL" / "Champions"
    state = read_sidecar(mod_path)
    if (state.get("version") == STEPS_VERSION
            and state.get("commit") == commit
            and state.get("complete")
            and _game_unchanged(state.get("game"), champions_dir)):
        return None
    result = fix_mod(mod_path, game_dir, steps=STEPS)
    state = {
        "version": STEPS_VERSION,
        "commit": commit,
        "complete": result.complete,
        "game": result.game_wads,
    }
    try:
        sidecar_path(mod_path).write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError as exc:
        log.debug("could not write the base-skin sidecar for %s: %s", mod_path.name, exc)
    return result
