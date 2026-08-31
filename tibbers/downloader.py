#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Get a champion's skin mods ready, one champion at a time.

Every mod is built here, out of the installed game (`skinsmith`): the skin's
own entries are lifted from the champion's archive, pointed at the base skin's
names and written back out as a `.fantome`. Nothing crosses the wire, it works
offline, and there is no third party whose files could go stale underneath it
-- only the install, and a mod records the archive it came out of so a patch
is noticed.

A skin whose mod cannot be built simply has none, and the log says why. The
picker already renders that as "no mod available" and leaves the skin alone.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Set

from . import library, skinsmith

log = logging.getLogger("tibbers.download")

WORKERS = 8


@dataclass(frozen=True, slots=True)
class Target:
    """One mod to end up with: a skin, or a chroma inside its skin."""
    skin_id: int
    chroma_id: Optional[int] = None

    @property
    def leaf(self) -> int:
        return self.chroma_id if self.chroma_id is not None else self.skin_id

    def __str__(self) -> str:
        return (f"chroma {self.chroma_id} of skin {self.skin_id}"
                if self.chroma_id is not None else f"skin {self.skin_id}")


def targets_from(skins: Iterable) -> List[Target]:
    """Flatten the client's skin list into the mods it implies.

    *skins* may be plain ids, or dicts carrying a "chromas" list. Chromas are
    prepared alongside their skin: they are separate mods, and the picker
    cannot offer one that was never built.
    """
    out: List[Target] = []
    for entry in skins:
        if isinstance(entry, dict):
            skin_id = int(entry["id"])
            # The base skin is what the game shows with no mod at all; a mod
            # for it would be a skin0-to-skin0 swap.
            if entry.get("isBase") or skin_id % 1000 == 0:
                continue
            out.append(Target(skin_id))
            for chroma in entry.get("chromas") or []:
                out.append(Target(skin_id, int(chroma["id"])))
        elif int(entry) % 1000 != 0:
            out.append(Target(int(entry)))
    return out


def mod_path(champion_id: int, target: Target):
    """Where this target's mod belongs, whether or not it is there yet."""
    directory = library.skins_dir() / str(champion_id) / str(target.skin_id)
    if target.chroma_id is not None:
        directory = directory / str(target.chroma_id)
    return directory / f"{target.leaf}.fantome"


def existing(champion_id: int, target: Target):
    if target.chroma_id is None:
        return library.find_mod(champion_id, target.skin_id)
    return library.find_chroma_mod(champion_id, target.skin_id,
                                   target.chroma_id)


def prepare(champion_id: int, target: Target,
            champion_key: Optional[str]) -> Optional[str]:
    """Build one mod. Returns "local", or None when it could not be built.

    None is an ordinary answer rather than an error -- not every skin has a
    mod that can be derived from the install -- so whatever stood in the way
    is named in the log, once, and the skin is simply offered without one.
    """
    if champion_key is None:
        log.info("%s %s: not built -- no archive for this champion",
                 champion_id, target)
        return None
    if not skinsmith.available():
        log.info("%s %s: not built -- xxhash and zstandard are not installed",
                 champion_id, target)
        return None
    if _generate(champion_id, target, champion_key):
        log.info("%s %s: built from the install", champion_id, target)
        return "local"
    return None


def _generate(champion_id: int, target: Target, champion_key: str) -> bool:
    dest = mod_path(champion_id, target)
    try:
        skinsmith.generate(champion_key, target.leaf, dest)
        return True
    except skinsmith.SkinsmithError as exc:
        log.info("%s %s: not built -- %s", champion_id, target, exc)
    except Exception:  # noqa: BLE001 - one skin must not stop the champion
        log.warning("%s %s: not built -- the build raised", champion_id,
                    target, exc_info=True)
    return False


class ChampionDownloader:
    """Gets a champion's skin mods ready in the background, once per champion.

    The name is older than the behaviour: mods are built here now, not
    fetched. It is kept because this is still the one thing that stands
    between hovering a champion and having its skins ready.
    """

    def __init__(self, on_progress: Optional[Callable[[int, dict], None]] = None):
        self.on_progress = on_progress
        self._lock = threading.Lock()
        self._done: Set[int] = set()
        self._active: Optional[int] = None
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- control -----------------------------------------------------------

    def cancel(self) -> None:
        """Stop the in-flight run. Files already written are kept."""
        self._cancel.set()

    def forget(self, champion_id: Optional[int] = None) -> None:
        """Let a champion be looked at again, after the library changed."""
        with self._lock:
            if champion_id is None:
                self._done.clear()
            else:
                self._done.discard(champion_id)

    def ensure(self, champion_id: int, skins: Iterable,
               champion_key: Optional[str] = None) -> None:
        """Start preparing this champion's mods unless already done or running.

        *champion_key* is the champion's internal name -- the client's alias --
        without which the archive cannot be found and nothing can be built.

        Safe to call repeatedly -- on every hover, for instance.
        """
        # Asked before the target list is built: this is called on every
        # change the watcher reports, and walking every skin and chroma of a
        # champion that is already prepared is work for an answer that was
        # settled by the first line.
        with self._lock:
            if champion_id in self._done or self._active == champion_id:
                return
            self._active = champion_id

        targets = targets_from(skins)
        key = skinsmith.champion_key(champion_key) if champion_key else None
        if champion_key and key is None:
            log.info("%s: no archive for %r, so none of its mods can be built",
                     champion_id, champion_key)

        # A new champion supersedes whatever was in flight.
        self._cancel.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._cancel = threading.Event()

        self._thread = threading.Thread(
            target=self._run, args=(champion_id, targets, key, self._cancel),
            daemon=True)
        self._thread.start()

    # -- work --------------------------------------------------------------

    def _run(self, champion_id: int, targets: List[Target],
             champion_key: Optional[str], cancel: threading.Event) -> None:
        wanted = [t for t in targets if _outstanding(champion_id, t)]

        if not wanted:
            with self._lock:
                self._done.add(champion_id)
                self._active = None
            self._report(champion_id, {"state": "complete", "fetched": 0,
                                       "missing": 0, "total": len(targets)})
            return

        self._report(champion_id, {"state": "downloading", "fetched": 0,
                                   "missing": 0, "total": len(wanted)})

        tally = {"made": 0, "missing": 0}

        def one(target: Target) -> Optional[str]:
            if cancel.is_set():
                return None
            return prepare(champion_id, target, champion_key)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for how in pool.map(one, wanted):
                if cancel.is_set():
                    break
                tally["made" if how else "missing"] += 1
                self._report(champion_id, {
                    "state": "downloading", "total": len(wanted),
                    "fetched": tally["made"], "missing": tally["missing"],
                })

        cancelled = cancel.is_set()
        with self._lock:
            if not cancelled:
                self._done.add(champion_id)
            if self._active == champion_id:
                self._active = None

        self._report(champion_id, {
            "state": "cancelled" if cancelled else "complete",
            "fetched": tally["made"], "missing": tally["missing"],
            "total": len(wanted),
        })

    def _report(self, champion_id: int, progress: dict) -> None:
        if self.on_progress:
            try:
                self.on_progress(champion_id, progress)
            except Exception:  # noqa: BLE001 - a UI callback must never break a run
                log.debug("progress callback failed", exc_info=True)


def _outstanding(champion_id: int, target: Target) -> bool:
    """True when this mod is missing, or was built from an older install.

    A mod with no sidecar was not built here -- imported by hand, or left over
    from before -- and is left exactly where it is: nothing local knows what it
    should look like. A generated one carries the size and mtime of the archive
    it came out of, so a patch is visible.
    """
    found = existing(champion_id, target)
    if found is None:
        return True
    if skinsmith.is_stale(found):
        log.info("%s %s: the install changed, so it is built again",
                 champion_id, target)
        return True
    return False


# ---------------------------------------------------------------------------
# The whole library at once
# ---------------------------------------------------------------------------

def rebuild(champions: Iterable[dict],
            skins_for: Callable[[int], Iterable],
            on_progress: Optional[Callable[[dict], None]] = None,
            cancel: Optional[threading.Event] = None) -> dict:
    """Rebuild every mod, for every champion given.

    *champions* are ``{"id": .., "alias": ..}`` from the client's own list and
    *skins_for* answers with that champion's skins. Everything that can be
    built is built again; a champion whose archive cannot be found is counted
    and passed over.

    Runs on the caller's thread; give it one of its own.
    """
    cancel = cancel or threading.Event()
    champions = [c for c in champions if c.get("id")]
    done = {"state": "rebuilding", "champions": 0,
            "total_champions": len(champions), "made": 0, "failed": 0,
            "champion": ""}

    def say() -> None:
        if on_progress:
            try:
                on_progress(dict(done))
            except Exception:  # noqa: BLE001
                log.debug("rebuild progress callback failed", exc_info=True)

    say()
    for champion in champions:
        if cancel.is_set():
            break
        champion_id = int(champion["id"])
        done["champion"] = champion.get("name") or str(champion_id)
        key = skinsmith.champion_key(champion.get("alias") or "")
        if key is None:
            log.info("rebuild: no archive for %s, skipped", done["champion"])
            done["champions"] += 1
            say()
            continue

        try:
            targets = targets_from(skins_for(champion_id) or [])
        except Exception as exc:  # noqa: BLE001 - one champion must not stop the rest
            log.warning("rebuild: no skin list for %s: %s",
                        done["champion"], exc)
            done["champions"] += 1
            say()
            continue

        for target in targets:
            if cancel.is_set():
                break
            if _generate(champion_id, target, key):
                done["made"] += 1
            else:
                done["failed"] += 1
        done["champions"] += 1
        say()

    done["state"] = "cancelled" if cancel.is_set() else "complete"
    say()
    return done
