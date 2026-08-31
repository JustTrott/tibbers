#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The local mod library.

Layout mirrors the community skin repositories, so a folder pulled from one
drops in unchanged::

    skins/<championId>/<skinId>/<skinId>.fantome

A `.fantome` is a zip holding `META/info.json` and a `WAD/` directory. The mods
carry no art: they rewrite the base skin's asset pointers to reference the
requested skin's files, which already ship with the game. That is why they are
kilobytes rather than megabytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import skinsmith, system

EXTENSIONS = (".fantome", ".zip")


#: The created skins directory, per data directory. Made once rather than on
#: every lookup: this is asked for once per skin and once per chroma, and the
#: picker re-marks availability on every file that lands.
_SKINS_DIR: Dict[Path, Path] = {}


def skins_dir() -> Path:
    root = system.data_dir()
    made = _SKINS_DIR.get(root)
    if made is None:
        made = root / "skins"
        made.mkdir(parents=True, exist_ok=True)
        _SKINS_DIR[root] = made
    return made


def _numbered_dirs(base: Path) -> List[Tuple[int, Path]]:
    """``(id, directory)`` for every child of *base* named after a number."""
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    out = []
    for entry in entries:
        try:
            out.append((int(entry.name), entry))
        except ValueError:
            continue
    return out


def _mod_in(directory: Path, ident: int) -> Optional[Path]:
    """The mod archive for *ident* inside *directory*, if one is there.

    Skins and chromas are laid out the same way and were looked up by two
    copies of this: the file named for its own id, and failing that any
    archive in the directory, which is what lets a folder pulled from a
    community repository drop in unchanged.
    """
    if not directory.is_dir():
        return None
    for ext in EXTENSIONS:
        candidate = directory / f"{ident}{ext}"
        if candidate.is_file():
            return candidate
    for ext in EXTENSIONS:
        for candidate in sorted(directory.glob(f"*{ext}")):
            return candidate
    return None


def find_chroma_mod(champion_id: int, skin_id: int,
                    chroma_id: int) -> Optional[Path]:
    """Locate a chroma's mod, which lives inside its parent skin's folder.

        skins/<championId>/<skinId>/<chromaId>/<chromaId>.fantome
    """
    return _mod_in(
        skins_dir() / str(champion_id) / str(skin_id) / str(chroma_id),
        chroma_id)


def available_chromas(champion_id: int, skin_id: int) -> set:
    """Chroma ids with a local mod, for this skin."""
    base = skins_dir() / str(champion_id) / str(skin_id)
    return {chroma_id for chroma_id, entry in _numbered_dirs(base)
            if _mod_in(entry, chroma_id) is not None}


def find_mod(champion_id: int, skin_id: int) -> Optional[Path]:
    """Locate the mod file for a skin, if it is present locally."""
    return _mod_in(skins_dir() / str(champion_id) / str(skin_id), skin_id)


def available_for_champion(champion_id: int) -> Dict[int, Path]:
    """``{skin_id: mod_path}`` for every skin this champion has locally."""
    found: Dict[int, Path] = {}
    for skin_id, skin_dir in _numbered_dirs(skins_dir() / str(champion_id)):
        mod = _mod_in(skin_dir, skin_id)
        if mod is not None:
            found[skin_id] = mod
    return found


def champions_present() -> List[int]:
    return sorted(champion_id for champion_id, _ in
                  _numbered_dirs(skins_dir()))


def stats() -> dict:
    """What is on disk, and how much of it this machine built for itself.

    A generated mod has a sidecar beside it naming the archive it came out of.
    Only its presence is looked at, not its contents: this is asked for on
    every settings poll, and opening a few hundred small files twice a second
    to read a number that a `stat` already answers is work for nothing.

    Anything without one is *foreign*: put here by hand, or left over from
    when mods were downloaded. Nothing builds those any more, and nothing
    deletes them either, so they are counted rather than hidden.
    """
    champs = champions_present()
    total = generated = 0
    for champion_id in champs:
        for mod in available_for_champion(champion_id).values():
            total += 1
            generated += skinsmith.sidecar_path(mod).is_file()
    return {"champions": len(champs), "skins": total, "generated": generated,
            "foreign": total - generated, "path": str(skins_dir())}
