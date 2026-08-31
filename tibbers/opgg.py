#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arena statistics from op.gg.

u.gg publishes nothing for Arena -- the URLs its own Arena pages reference
return AccessDenied on every patch tried, and augments appear nowhere in its
endpoint manifest. metasrc has the data but its robots.txt names ClaudeBot
with ``Disallow: /``, puts the paginated augment rows behind ``Disallow:
/api/`` for every agent, and its terms forbid extraction beyond indexing;
reaching it at all needs a spoofed browser fingerprint to pass a Cloudflare
challenge. So neither is used here.

op.gg serves the same figures as plain JSON from an API host whose robots.txt
is ``User-agent: * / Disallow:`` -- everything permitted -- with no challenge
and no spoofing required.

Augment and item names and icons still come from the client, as everywhere
else in this app; only the numbers come from here.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from . import system

log = logging.getLogger("tibbers.opgg")

BASE = "https://lol-api-champion.op.gg/api"

#: Honest rather than disguised. op.gg permits automated access, so there is
#: no reason to pretend to be a browser.
UA = "tibbers/0.1 (personal League skin and build helper; contact via GitHub)"

CACHE_SECONDS = 60 * 60

#: op.gg's rarity codes for augment tiers, checked against the names the
#: client gives them rather than assumed from their order.
RARITY = {1: "silver", 4: "gold", 8: "prismatic"}


class Unavailable(Exception):
    """op.gg could not be reached, or has nothing for this champion."""


class OPGG:
    """Arena data: augments, prismatic items, item builds and skills."""

    def __init__(self):
        self._lock = threading.Lock()
        self._memory: Dict[str, Any] = {}

    def _get(self, url: str, key: str, ttl: int = CACHE_SECONDS) -> Any:
        with self._lock:
            hit = self._memory.get(key)
        if hit and time.time() - hit["at"] < ttl:
            return hit["data"]

        path = system.data_dir() / "opgg" / f"{key.replace('/', '_')}.json"
        stored = None
        try:
            stored = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        if stored and time.time() - stored.get("at", 0) < ttl:
            with self._lock:
                self._memory[key] = stored
            return stored["data"]

        request = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            if stored:
                log.debug("serving stale op.gg data for %s: %s", key, exc)
                return stored["data"]
            raise Unavailable(str(exc)) from exc

        entry = {"at": time.time(), "data": data}
        with self._lock:
            self._memory[key] = entry
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.part")
            tmp.write_text(json.dumps(entry))
            tmp.replace(path)
        except OSError as exc:
            log.debug("could not cache %s: %s", key, exc)
        return data

    def arena(self, champion_id: int, region: str = "global") -> dict:
        """Everything op.gg has for one champion in Arena."""
        payload = self._get(f"{BASE}/{region}/champions/arena/{int(champion_id)}",
                            f"arena-{region}-{int(champion_id)}")
        data = (payload or {}).get("data")
        if not data:
            raise Unavailable(f"op.gg has no Arena data for champion {champion_id}")
        return {"data": data, "patch": ((payload.get("meta") or {}).get("version"))}

    # -- shaping -----------------------------------------------------------

    @staticmethod
    def _rate(row: dict) -> dict:
        """Arena is placement, not wins, so every figure here is a placement.

        What op.gg's ``win`` means was measured against the whole population
        rather than assumed, because the mode's shape has changed. Pooled
        across all 173 played champions the all-champions file gives exactly
        ``win/play = 0.50000``, ``first_place/play = 0.16667`` (one sixth),
        ``total_place/play = 3.50000`` and ``sum(pick_rate) = 18.0000``.
        Those four numbers only agree on one reading: **six teams of three,
        finishing positions 1 to 6**, mean finish 3.5, and a ``win`` is a
        **top-three** finish -- the top half of the lobby, which is what
        Riot itself scores as a win. It is not an outright victory (that is
        ``first_place``) and it is not top four.

        ``avgFinish`` is the honest measure and the one the tier rests on:
        first place alone throws away five sixths of every game's outcome.
        Lower is better.
        """
        play = int(row.get("play") or 0)
        first = int(row.get("first_place") or 0)
        wins = int(row.get("win") or 0)
        places = int(row.get("total_place") or 0)
        return {
            "matches": play,
            "pickRate": round(float(row.get("pick_rate") or 0) * 100, 1),
            "firstRate": round(first / play * 100, 1) if play else 0.0,
            # Kept under its own name rather than "winRate": in this mode a
            # win is a placement band, and calling it a win rate beside a
            # Rift page that means something else by it would be a lie.
            "topRate": round(wins / play * 100, 1) if play else 0.0,
            "avgFinish": round(places / play, 2) if play else None,
        }

    def champions(self, region: str = "global") -> dict:
        """Every champion's Arena summary, in one file.

        One ~57KB fetch stands in for 170 per-champion ones, which is what
        makes a champion tier list affordable during champ select. Cached per
        patch, because that is the only thing that moves it.
        """
        payload = self._get(f"{BASE}/{region}/champions/arena",
                            f"arena-champions-{region}")
        rows = (payload or {}).get("data")
        if not rows:
            raise Unavailable("op.gg returned no Arena champion list")
        patch = ((payload.get("meta") or {}).get("version"))
        out = []
        for entry in rows:
            stats = entry.get("average_stats") or {}
            if not stats.get("play"):
                # Never played this patch: a rank would be an invention.
                continue
            out.append({
                "championId": int(entry["id"]),
                # op.gg publishes an integer tier, 1 (best) to 5, alongside
                # a global rank. Nothing in the payload maps those onto
                # letters, so they are shown as op.gg publishes them rather
                # than dressed up as an S/A/B scale this data never claimed.
                "tier": stats.get("tier"),
                "rank": stats.get("rank"),
                "banRate": round(float(stats.get("ban_rate") or 0) * 100, 1),
                **self._rate(stats),
            })
        out.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0))
        return {"patch": patch, "champions": out}

    def augments(self, champion_id: int, region: str = "global") -> List[dict]:
        """Every augment, grouped by rarity, most picked first within each.

        All fifteen of each rarity, not a top few: the page that shows six
        cannot answer "is the one I was offered any good", which is the only
        question anybody opens it with.
        """
        data = self.arena(champion_id, region)["data"]
        groups = []
        for group in data.get("augment_group") or []:
            rows = [{"id": int(a["id"]), **self._rate(a)}
                    for a in (group.get("augments") or []) if a.get("id")]
            rows.sort(key=lambda r: -r["pickRate"])
            groups.append({"rarity": RARITY.get(int(group.get("rarity") or 0), "other"),
                           "rarityCode": group.get("rarity"), "augments": rows})
        order = {"prismatic": 0, "gold": 1, "silver": 2, "other": 3}
        groups.sort(key=lambda g: order.get(g["rarity"], 9))
        return groups

    def items(self, champion_id: int, region: str = "global") -> dict:
        """Prismatic picks, core builds, boots and late items, in full.

        Nothing is truncated here. The page decides what it has room for;
        this decides what is true.
        """
        data = self.arena(champion_id, region)["data"]

        def rows(key: str) -> List[dict]:
            out = []
            for row in data.get(key) or []:
                ids = [int(i) for i in (row.get("ids") or [])]
                if ids:
                    out.append({"ids": ids, **self._rate(row)})
            return out

        return {"prismatic": rows("prism_items"),
                "core": rows("core_items"),
                "boots": rows("boots"),
                "late": rows("last_items")}

    def skills(self, champion_id: int, region: str = "global") -> dict:
        data = self.arena(champion_id, region)["data"]
        mastery = (data.get("skill_masteries") or [{}])[0]
        order = (data.get("skills") or [{}])[0]
        return {"priority": [str(s) for s in (mastery.get("ids") or [])],
                "order": [str(s) for s in (order.get("order") or [])],
                **self._rate(mastery if mastery else {})}
