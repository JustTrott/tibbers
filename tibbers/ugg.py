#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARAM Mayhem's augment rankings, which are u.gg's and nobody else's.

Everything else this app reads -- builds, counters, every mode's items and
runes -- comes from op.gg now. What is left here is the one thing op.gg does
not publish: how Mayhem ranks its augments, which is the half of that mode
that decides the game.

The important thing about these files is *where* they are. u.gg's build CDN
(`stats2.u.gg`) scores the TLS handshake and refuses roughly one request in
five, which is why this module used to carry a spoofed user-agent, four
alternative header sets and a 4 MB downloaded curl that impersonated Chrome
just to be spoken to at all. Mayhem's rankings are not on it. They are static
JSON on `static.bigbrain.gg`, which puts up no challenge and answers plain
urllib, so none of that apparatus is needed and none of it is here.

The patch manifest lives on the same unchallenged host, and is read for one
purpose: Mayhem's files are keyed by patch, and asking for a patch that was
never published is the only way to get nothing when there is something.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, List, Optional

from . import system

log = logging.getLogger("tibbers.ugg")

#: Which patches u.gg has published. On the static host, not the build CDN.
VERSIONS_URL = ("https://static.bigbrain.gg/assets/lol/riot_patch_update/"
                "prod/ugg/ugg-api-versions.json")

#: ARAM Mayhem's augment rankings, keyed by patch.
MAYHEM_BASE = "https://static.bigbrain.gg/custom-aram-mayhem"

#: Honest, because nothing here needs to be anything else.
UA = "tibbers/0.1 (personal League skin and build helper; contact via GitHub)"

#: How long a fetched file is trusted. u.gg regenerates roughly daily.
CACHE_SECONDS = 8 * 3600

#: How many decoded files to hold in memory.
MEMORY_MAX = 16


class Unavailable(Exception):
    """u.gg could not be reached, or has nothing for this champion."""


class UGG:
    """ARAM Mayhem's augment rankings."""

    def __init__(self):
        self._lock = threading.Lock()
        self._versions: Optional[dict] = None
        self._versions_at = 0.0
        self._memory: "OrderedDict[str, Any]" = OrderedDict()

    # -- transport ---------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        safe = key.replace("/", "_")
        return system.data_dir() / "ugg" / f"{safe}.json"

    def _remember(self, key: str, entry: dict) -> None:
        with self._lock:
            self._memory[key] = entry
            self._memory.move_to_end(key)
            while len(self._memory) > MEMORY_MAX:
                self._memory.popitem(last=False)

    @staticmethod
    def _fresh(entry: Optional[dict], ttl: int) -> bool:
        """Whether a cached entry can be served without asking anyone."""
        if not entry:
            return False
        return time.time() - entry.get("at", 0) < ttl

    def _get(self, url: str, key: str, ttl: int = CACHE_SECONDS) -> Any:
        with self._lock:
            hit = self._memory.get(key)
            if hit is not None:
                self._memory.move_to_end(key)
        if self._fresh(hit, ttl):
            return hit["data"]

        path = self._cache_path(key)
        stored = None
        try:
            stored = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        if self._fresh(stored, ttl):
            self._remember(key, stored)
            return stored["data"]

        request = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            # A held copy beats nothing: these rankings move once a patch, so
            # yesterday's is worth far more than an empty table.
            if stored:
                log.debug("serving stale u.gg data for %s: %s", key, exc)
                return stored["data"]
            log.info("u.gg unreachable for %s: %s", key, exc)
            raise Unavailable(str(exc)) from exc

        entry = {"at": time.time(), "data": data}
        self._remember(key, entry)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.part")
            tmp.write_text(json.dumps(entry))
            tmp.replace(path)
        except OSError as exc:
            log.debug("could not cache %s: %s", key, exc)
        return data

    # -- versions ----------------------------------------------------------

    def manifest(self) -> dict:
        """Every patch u.gg publishes."""
        if self._versions and time.time() - self._versions_at < 3600:
            return self._versions
        data = self._get(VERSIONS_URL, "versions", ttl=3600)
        if not isinstance(data, dict) or not data:
            raise Unavailable("could not read u.gg's version manifest")
        self._versions = data
        self._versions_at = time.time()
        return data

    @staticmethod
    def _order(patch: str):
        try:
            return [int(part) for part in patch.split("_")]
        except (AttributeError, ValueError):
            return [0]

    def patches(self, limit: int = 8) -> List[str]:
        """The most recent patches, newest first."""
        return sorted(self.manifest(), key=self._order, reverse=True)[:limit]

    def resolve_patch(self, patch: Optional[str] = None) -> str:
        """The patch to read, defaulting to the newest published."""
        manifest = self.manifest()
        if patch and patch in manifest:
            return patch
        return sorted(manifest, key=self._order)[-1]

    # -- ARAM Mayhem -------------------------------------------------------

    def _mayhem(self, tail: str, key: str, holds: str,
                patch: Optional[str] = None) -> dict:
        """One Mayhem file, from the newest patch that actually has it.

        The fallback is one patch deep and openly reported: the patch that
        answered comes back in the payload and is printed under the table, so
        a ranking read from last patch says so rather than passing as this
        one's. Beyond one step the data is old enough that no data is the
        more honest answer.
        """
        wanted = self.resolve_patch(patch)
        tried = [wanted]
        if not patch:
            recent = self.patches(2)
            if len(recent) > 1 and recent[0] == wanted:
                tried.append(recent[1])

        why = "not published"
        for chosen in tried:
            url = f"{MAYHEM_BASE}/{chosen}/{tail.format(patch=chosen)}"
            try:
                data = self._get(url, f"{key}-{chosen}")
            except Unavailable as exc:
                why = str(exc)
                continue
            if isinstance(data, dict) and data:
                return {"patch": chosen, **data}
            why = "the file was empty"
        raise Unavailable(f"u.gg has no Mayhem {holds} for "
                          f"{' or '.join(tried)}: {why}")

    def mayhem_augments(self, champion_id: int,
                        patch: Optional[str] = None) -> dict:
        """How u.gg ranks every augment for one champion in ARAM Mayhem.

        Letters, not numbers: u.gg publishes the bands and keeps the figures
        behind them to itself, so this is passed on as the ranking it is
        rather than dressed up with rates it does not carry.
        """
        out = self._mayhem(
            f"tierlist-per-champion-augments-{{patch}}/"
            f"tierlist-augments-{int(champion_id)}-{{patch}}.json",
            f"mayhem-augments-{int(champion_id)}", "augment ranking", patch)
        tiers = out.get("tiers")
        if not isinstance(tiers, dict) or not tiers:
            raise Unavailable(
                f"u.gg ranks no Mayhem augments for champion {champion_id}")
        return {"patch": out["patch"], "tiers": tiers,
                "lastUpdated": out.get("lastUpdated")}

    def mayhem_augment_pool(self, patch: Optional[str] = None) -> dict:
        """Every augment the mode offers, ranked across all champions.

        The champion file ranks only what it has enough games for, which is
        roughly two thirds of the pool. This is what fills the rest in: an
        augment you were just offered and cannot find is the one failure the
        page exists to prevent.
        """
        out = self._mayhem(f"tierlist-augments-by-rarity-{{patch}}.json",
                           "mayhem-augment-pool", "augment pool", patch)
        rarities = out.get("rarities")
        if not isinstance(rarities, dict) or not rarities:
            raise Unavailable("u.gg published no Mayhem augment pool")
        return {"patch": out["patch"], "rarities": rarities}
