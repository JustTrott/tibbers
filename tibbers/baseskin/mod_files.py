#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read a mod's files, for the base-skin fix.

Rose's ``injection.mods.mod_files.ModFiles`` reads both a ``.fantome`` archive
and an unpacked WAD folder, and streams a WAD stored uncompressed in the
archive without inflating it. Tibbers's mods are always ``.fantome`` archives
downloaded from the LeagueSkins catalogue, so this handles an archive only; a
WAD is read whole (it is already compressed inside, and a champion WAD is well
under the byte ceiling the fix allows).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class ModFiles:
    """The files of one ``.fantome`` archive, read lazily."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.is_archive = self.path.is_file()
        self._zip: Optional[zipfile.ZipFile] = None
        self._infos: Dict[str, zipfile.ZipInfo] = {}
        self._names: List[str] = []
        if self.is_archive:
            self._zip = zipfile.ZipFile(self.path, "r")
            for info in self._zip.infolist():
                if info.is_dir():
                    continue
                self._infos[info.filename] = info
                self._names.append(info.filename)

    def __enter__(self) -> "ModFiles":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    # -- what the fix asks for ---------------------------------------------

    def files_under(self, folder: Optional[str] = None) -> List[str]:
        """The names of every file, or of those under *folder*."""
        if folder is None:
            return list(self._names)
        prefix = folder.rstrip("/") + "/"
        return [name for name in self._names if name.startswith(prefix)]

    def is_file(self, name: str) -> bool:
        return name in self._infos

    def size(self, name: str) -> Optional[int]:
        info = self._infos.get(name)
        return info.file_size if info is not None else None

    def read(self, name: str) -> bytes:
        if self._zip is None:
            raise OSError(f"{self.path} is not an archive")
        return self._zip.read(name)

    def member(self, name: str) -> Optional[zipfile.ZipInfo]:
        return self._infos.get(name)

    def span(self, name: str) -> Optional[Tuple[Path, int, int]]:
        """Never: a WAD is read whole rather than streamed from the archive."""
        return None
