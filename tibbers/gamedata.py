#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The client's own reference data: runes, items, summoner spells, abilities.

A build guide is mostly names and icons, and the client already has every one
of them. Reading them from the local client rather than a web API means the
guide renders from the same install the game runs from -- right patch, right
art, no image ever fetched over the network, and it keeps working offline once
cached. Community Dragon serves byte-identical copies and stands in when the
client is not running.

Everything here is keyed by patch, because that is exactly when it goes stale:
an item renamed or a rune reworked changes these files and nothing else.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import system

log = logging.getLogger("tibbers.gamedata")

#: Bulk reference files, by the name this module knows them by.
FILES = {
    "perks": "/lol-game-data/assets/v1/perks.json",
    "perkstyles": "/lol-game-data/assets/v1/perkstyles.json",
    "items": "/lol-game-data/assets/v1/items.json",
    "spells": "/lol-game-data/assets/v1/summoner-spells.json",
    "champions": "/lol-game-data/assets/v1/champion-summary.json",
    # Arena augments. 657 of them, complete: Community Dragon's separate
    # arena file carries only 225 and is missing the newer ones entirely.
    "augments": "/lol-game-data/assets/v1/cherry-augments.json",
}

CDRAGON = "https://raw.communitydragon.org/latest"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: How long a file that could not be read is left alone before it is tried
#: again. A failure used to be cached like a success, so one blip with the
#: client down and Community Dragon unreachable blanked every name and icon
#: for the rest of the session. Retrying on every lookup instead would put a
#: network call inside the guide's own rendering, so it is neither.
RETRY_SECONDS = 60.0

#: Stands in for the patch while no client has been seen. Not a patch: it is
#: also Community Dragon's own name for "whatever is current", which is what
#: the fallback fetches, and the cache directory it names is shared by every
#: run that started before League did.
PLACEHOLDER_PATCH = "latest"

def cdragon_url(asset_path: str) -> Optional[str]:
    """Community Dragon's URL for an LCU asset path.

    The three prefixes map to different roots, and guessing wrong is a 404
    rather than a redirect, so each is spelled out.
    """
    p = asset_path.lstrip("/")
    if p.startswith("lol-game-data/assets/"):
        p = p[len("lol-game-data/assets/"):]
    if p.startswith("v1/"):
        return f"{CDRAGON}/plugins/rcp-be-lol-game-data/global/default/{p.lower()}"
    if p.upper().startswith("ASSETS/"):
        return f"{CDRAGON}/game/assets/{p[len('ASSETS/'):].lower()}"
    if p.upper().startswith("DATA/"):
        return f"{CDRAGON}/game/data/{p[len('DATA/'):].lower()}"
    return None


class GameData:
    """Reference data for one patch, cached on disk."""

    def __init__(self, get_lcu: Callable[[], Any]):
        self.get_lcu = get_lcu
        self._lock = threading.Lock()
        self._memory: Dict[str, Any] = {}
        self._index: Dict[str, Dict[int, dict]] = {}
        self._patch: Optional[str] = None
        #: When each name was last given up on, so a failure is retried
        #: rather than remembered as though it were an answer.
        self._failed: Dict[str, float] = {}

    # -- patch -------------------------------------------------------------

    def patch(self) -> str:
        """The installed patch. Every cached file is filed under it.

        Without a client there is no way to know it, so the cache is shared
        under one name rather than silently split per run -- but PLACEHOLDER
        is an admission, not an answer, and it used to stick for the rest of
        the session. This app usually starts before League does (it runs at
        login and watches for champ select), so that meant a whole evening of
        reference data filed under a patch nobody is on.

        A real answer is settled for good. The placeholder is revisited, but
        only once a client has actually turned up: get_lcu() is an attribute
        read on the watcher, so every call after the first costs nothing.
        """
        client = self.get_lcu()
        with self._lock:
            settled = self._patch
        if settled and (settled != PLACEHOLDER_PATCH or client is None):
            return settled

        version = None
        if client is not None:
            raw = client.get("/lol-patch/v1/game-version")
            if isinstance(raw, str):
                version = raw.split("+")[0]
        version = version or PLACEHOLDER_PATCH

        with self._lock:
            moved = settled is not None and version != settled
            self._patch = version
            if moved:
                # What was read under the placeholder was read without a
                # client and was never checked against this patch. Drop the
                # in-memory copies so the next lookup re-reads under the real
                # name, rather than serving a patch's data from another's
                # directory for the rest of the session.
                self._memory.clear()
                self._index.clear()
                self._failed.clear()
        if moved:
            log.info("patch resolved to %s; reference data re-read from the "
                     "client", version)
        return version

    def _cache_dir(self) -> Path:
        return system.data_dir() / "gamedata" / self.patch()

    # -- loading -----------------------------------------------------------

    def _load(self, name: str) -> Any:
        # Before the memory check, not after: resolving the patch is what
        # drops copies read under the placeholder, and a reader that answered
        # from memory first would never let that happen.
        self.patch()
        with self._lock:
            if name in self._memory:
                return self._memory[name]
            if time.time() - self._failed.get(name, 0.0) < RETRY_SECONDS:
                # Given up on a moment ago. Answer the same way without
                # reaching for the network again on every single lookup.
                return None

        path = self._cache_dir() / f"{name}.json"
        data = None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass

        if data is None:
            data = self._download(FILES[name])
            if data is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".json.part")
                    tmp.write_text(json.dumps(data))
                    tmp.replace(path)
                except OSError as exc:
                    log.debug("could not cache %s: %s", name, exc)

        with self._lock:
            if data is None:
                self._failed[name] = time.time()
                return None
            self._memory[name] = data
            self._failed.pop(name, None)
        return data

    def _download(self, asset_path: str) -> Any:
        client = self.get_lcu()
        if client is not None:
            data = client.get(asset_path)
            if data is not None:
                return data

        url = cdragon_url(asset_path)
        if url is None:
            return None
        try:
            # Community Dragon refuses urllib's default user-agent outright,
            # which would silently empty every name and icon in exactly the
            # case this fallback exists for -- the client not running.
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            log.debug("could not fetch %s: %s", url, exc)
            return None

    # -- lookups -----------------------------------------------------------

    def _by_id(self, name: str, rows: Optional[list] = None) -> Dict[int, dict]:
        # The index is the fast path every name and icon goes through, so it
        # is also the one that has to notice the patch moving. Settled, this
        # is an attribute read and a string compare.
        self.patch()
        with self._lock:
            if name in self._index:
                return self._index[name]
        if rows is None:
            rows = self._load(name)
            if rows is None:
                # Nothing to index yet. Pinning an empty index here is what
                # turned one failed load into a session with no names on it.
                return {}
        index = {int(r["id"]): r for r in rows if isinstance(r, dict) and "id" in r}
        with self._lock:
            self._index[name] = index
        return index

    @staticmethod
    def _entry(row: Optional[dict], icon_field: str = "iconPath") -> Optional[dict]:
        if not row:
            return None
        return {"id": row.get("id"), "name": row.get("name") or "",
                "icon": row.get(icon_field) or ""}

    def rune(self, perk_id: int) -> Optional[dict]:
        return self._entry(self._by_id("perks").get(int(perk_id)))

    def item(self, item_id: int) -> Optional[dict]:
        return self._entry(self._by_id("items").get(int(item_id)))

    def spell(self, spell_id: int) -> Optional[dict]:
        return self._entry(self._by_id("spells").get(int(spell_id)))

    def champion(self, champion_id: int) -> Optional[dict]:
        row = self._by_id("champions").get(int(champion_id))
        return self._entry(row, "squarePortraitPath")

    def champion_alias(self, champion_id: int) -> str:
        """Riot's internal name: ``MonkeyKing`` for Wukong, ``Nunu`` for
        Nunu & Willump. Lowercased it is the URL slug both stats sites use."""
        row = self._by_id("champions").get(int(champion_id)) or {}
        return row.get("alias") or ""

    def augment(self, augment_id: int) -> Optional[dict]:
        """One Arena augment, by Riot's id."""
        row = self._by_id("augments").get(int(augment_id))
        if not row:
            return None
        return {"id": row.get("id"),
                "name": row.get("nameTRA") or row.get("augmentNameId") or "",
                "icon": row.get("augmentSmallIconPath") or "",
                # kPrismatic, kGold, kSilver -- lowered so the UI does not
                # have to know Riot's prefix.
                "rarity": str(row.get("rarity") or "").removeprefix("k").lower()}

    def tree(self, style_id: int) -> Optional[dict]:
        styles = (self._load("perkstyles") or {}).get("styles") or []
        for style in styles:
            if int(style.get("id", -1)) == int(style_id):
                return self._entry(style)
        return None

    def tree_rows(self, style_id: int) -> list:
        """Perk ids per row for a tree: keystone first, then the three rows.

        The picker draws the tree the way the client does, so it needs the
        shape as well as the selection: a rune's meaning is its position.
        """
        styles = (self._load("perkstyles") or {}).get("styles") or []
        for style in styles:
            if int(style.get("id", -1)) != int(style_id):
                continue
            rows = []
            for slot in style.get("slots") or []:
                kind = slot.get("type") or ""
                if kind == "kStatMod":
                    continue
                rows.append([int(p) for p in (slot.get("perks") or [])])
            return rows
        return []

    def stat_rows(self) -> list:
        """The three stat shard rows, in the order a rune page stores them.

        Skipped by `tree_rows`, which draws the trees, and needed by the
        importer, which has to put three shards on three rows that share
        entries -- health scaling appears in two of them, so the row cannot be
        worked out from the shard alone.
        """
        styles = (self._load("perkstyles") or {}).get("styles") or []
        for style in styles:
            rows = [[int(p) for p in (slot.get("perks") or [])]
                    for slot in style.get("slots") or []
                    if (slot.get("type") or "") == "kStatMod"]
            if len(rows) == 3:
                return rows
        return []

    def abilities(self, champion_id: int) -> Dict[str, dict]:
        """``{"p": …, "q": …, "w": …, "e": …, "r": …}`` for one champion."""
        key = f"champion:{champion_id}"
        self.patch()
        with self._lock:
            cached = self._memory.get(key)
            if cached is None and (time.time() - self._failed.get(key, 0.0)
                                   < RETRY_SECONDS):
                return {}
        if cached is not None:
            return cached

        path = self._cache_dir() / f"champion-{int(champion_id)}.json"
        data = None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = self._download(
                f"/lol-game-data/assets/v1/champions/{int(champion_id)}.json")
            if data is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(data))
                except OSError as exc:
                    log.debug("could not cache champion %s: %s",
                              champion_id, exc)

        if data is None:
            # Same rule as _load: a champion whose file could not be read
            # keeps its empty skill rows for a minute, not for the session.
            with self._lock:
                self._failed[key] = time.time()
            return {}

        out: Dict[str, dict] = {}
        if isinstance(data, dict):
            passive = data.get("passive") or {}
            if passive:
                out["p"] = {"name": passive.get("name") or "",
                            "icon": passive.get("abilityIconPath") or ""}
            for spell in data.get("spells") or []:
                key_letter = (spell.get("spellKey") or "").lower()
                if key_letter in ("q", "w", "e", "r"):
                    out[key_letter] = {
                        "name": spell.get("name") or "",
                        "icon": spell.get("abilityIconPath") or "",
                    }
        with self._lock:
            self._memory[key] = out
            self._failed.pop(key, None)
        return out
