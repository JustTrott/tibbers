#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remembered choices and settings.

Two kinds of memory, because they are asked at different moments:

* **skin per champion** -- lock Jhin and you almost always want the same skin
  you picked last time.
* **chroma per skin** -- a chroma belongs to its skin, not to the champion, so
  it has to be keyed separately or switching skins would carry the wrong one.

Written to one JSON file, saved on every change. It is a few hundred bytes and
the alternative is losing the memory whenever the app is killed rather than
quit.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from . import system

log = logging.getLogger("tibbers.prefs")

DEFAULTS: Dict[str, Any] = {
    # Raise the picker by itself when a champion is locked in.
    "auto_show": True,
    # Close it when champ select ends. Off by default: once the game starts
    # the picker still has the build worth reading, so it stops floating
    # rather than disappearing.
    "auto_hide": False,
    # Re-apply the remembered skin as soon as a champion is locked.
    "remember_selections": True,
    # Build a champion's mods on hover rather than waiting for a lock. The
    # key is named for what this used to do, when mods were downloaded; it is
    # kept so an existing preferences.json still says what the user chose,
    # and the switch means what it always meant -- do it while they are
    # still deciding.
    "download_on_hover": True,
    # Keep the picker above the client. It is small and movable, so floating
    # costs nothing; the earlier objection was to a window that sat over
    # everything permanently, which this still is not -- it comes and goes
    # with champ select.
    "always_on_top": True,
    # Which patch to read build data from. None follows the newest published,
    # which is also the one with the least data behind it for a week or two.
    "patch": None,
    # Set the build's summoner spells when importing. On, because a build
    # whose spells are not the ones you took is only most of a build -- but a
    # switch, because spells are the one thing people are opinionated about.
    "import_spells": True,
    # Import the build by itself when you lock in. Off: it writes over a rune
    # page and your spells, and doing that unasked on the first game after an
    # update would be a nasty surprise.
    "auto_import": False,
    # Download a new release and swap it in by itself, once League is idle.
    # On: the alternative is a "you are up to date" that goes stale for
    # anyone who never opens Settings. Never in champ select or a game.
    "auto_update": True,
    # The one-time answer to "how should injection get permission": None until
    # the user is first asked, then "auto" (a helper is installed, so no
    # prompt) or "prompt" (ask each time). Not a switch on the settings page --
    # it records the first-run choice so the question is asked only once. The
    # Elevation section governs it after that.
    "elevation_choice": None,
}

#: How long a geometry change waits before it is written. The picker is
#: frameless and dragged by its body, so `moved` arrives continuously for as
#: long as the drag lasts -- and each one rewrote the whole file. Settings and
#: remembered picks are still written the moment they change: those are
#: decisions, and there is one of them, not sixty a second.
GEOMETRY_SAVE_DELAY = 1.0


class Prefs:
    """Settings plus remembered selections, persisted as one file."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else system.data_dir() / "preferences.json"
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "settings": dict(DEFAULTS),
            "skin_by_champion": {},
            "chroma_by_skin": {},
            # Where each window was, and whether it was on screen. Not a
            # setting -- nothing in the UI edits it -- so it is kept out of
            # DEFAULTS and cannot be reached through /api/settings.
            "geometry": {},
        }
        #: A pending coalesced write, if one is on its way.
        self._save_timer: Optional[threading.Timer] = None
        #: True when there was no file to read, i.e. this is the first launch.
        #: The settings window is shown once, then never again on its own.
        self.first_run = not self.path.is_file()
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            # Merge rather than replace: a file written by an older version is
            # missing keys added since, and should not wipe the defaults.
            self._data["settings"] = {**DEFAULTS, **(raw.get("settings") or {})}
            stored_geometry = raw.get("geometry")
            if isinstance(stored_geometry, dict):
                self._data["geometry"] = stored_geometry
            for key in ("skin_by_champion", "chroma_by_skin"):
                stored = raw.get(key) or {}
                self._data[key] = {str(k): int(v) for k, v in stored.items()
                                   if str(v).lstrip("-").isdigit()}

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.part")
            tmp.write_text(payload)
            tmp.replace(self.path)
        except OSError as exc:
            log.debug("could not save preferences: %s", exc)

    def save_soon(self, delay: float = GEOMETRY_SAVE_DELAY) -> None:
        """Write within *delay*, folding in everything that arrives meanwhile."""
        with self._lock:
            if self._save_timer is not None:
                return                      # a write is already on its way
            timer = self._save_timer = threading.Timer(delay, self._save_now)
            timer.daemon = True
        timer.start()

    def _save_now(self) -> None:
        with self._lock:
            self._save_timer = None
        self.save()

    def flush(self) -> None:
        """Write a pending change out now, rather than on its own schedule."""
        with self._lock:
            timer, self._save_timer = self._save_timer, None
        if timer is not None:
            timer.cancel()
            self.save()

    # -- settings ----------------------------------------------------------

    def get(self, name: str) -> Any:
        with self._lock:
            return self._data["settings"].get(name, DEFAULTS.get(name))

    def set(self, name: str, value: Any) -> None:
        if name not in DEFAULTS:
            raise KeyError(f"unknown setting: {name}")
        with self._lock:
            self._data["settings"][name] = value
        self.save()

    def settings(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data["settings"])

    # -- remembered selections --------------------------------------------

    def skin_for(self, champion_id: int) -> Optional[int]:
        with self._lock:
            return self._data["skin_by_champion"].get(str(champion_id))

    def chroma_for(self, skin_id: int) -> Optional[int]:
        with self._lock:
            return self._data["chroma_by_skin"].get(str(skin_id))

    def remember(self, champion_id: int, skin_id: Optional[int],
                 chroma_id: Optional[int]) -> None:
        """Record a choice. Clearing a skin forgets it rather than storing null."""
        with self._lock:
            skins = self._data["skin_by_champion"]
            chromas = self._data["chroma_by_skin"]

            if skin_id is None:
                skins.pop(str(champion_id), None)
            else:
                skins[str(champion_id)] = int(skin_id)
                # A chroma is meaningless without its skin, so it is keyed by
                # the skin and cleared when that skin is set to no chroma.
                if chroma_id is None:
                    chromas.pop(str(skin_id), None)
                else:
                    chromas[str(skin_id)] = int(chroma_id)
        self.save()

    # -- window geometry ---------------------------------------------------
    #
    # So that restarting the app -- which is now something that happens while
    # a game is running -- puts the picker back where it was rather than
    # centring it over whatever the player is looking at.

    def geometry(self, name: str) -> Dict[str, Any]:
        with self._lock:
            box = self._data.get("geometry", {}).get(name)
        return dict(box) if isinstance(box, dict) else {}

    def remember_geometry(self, name: str, **fields: Any) -> None:
        """Merge *fields* into the stored box for *name*.

        Merged rather than replaced: position, size and visibility are
        reported by three different events, each of which knows only its own
        half.
        """
        with self._lock:
            boxes = self._data.setdefault("geometry", {})
            box = boxes.setdefault(name, {})
            box.update({k: v for k, v in fields.items() if v is not None})
        self.save_soon()

    def forget_all(self) -> None:
        """Drop every remembered skin and chroma, in one write.

        Settings asks for this as a single action, and the caller used to do
        it by reaching into the private dict and calling `remember` per
        champion -- which rewrote the whole preferences file once for each
        champion the user had ever picked a skin for.
        """
        with self._lock:
            self._data["skin_by_champion"] = {}
            self._data["chroma_by_skin"] = {}
        self.save()

    def stats(self) -> dict:
        with self._lock:
            return {
                "champions": len(self._data["skin_by_champion"]),
                "chromas": len(self._data["chroma_by_skin"]),
            }
