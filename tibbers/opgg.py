#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build, counters and Arena statistics from op.gg.

Everything the build and counters pages render comes from here: items, runes,
skills, summoners and counters for Summoner's Rift, ARAM and URF, and Arena's
augments, items and tier list. One request per champion, mode and lane carries
all of it.

Why here and not u.gg: u.gg's build CDN scores the TLS handshake and refuses
roughly one request in five, which is what a spoofed user-agent, four
alternative header sets and a 4 MB downloaded curl-impersonate all existed to
get past -- and it published nothing for Arena at all. op.gg serves the same
figures as plain JSON from an API host whose robots.txt is
``User-agent: * / Disallow:`` -- everything permitted -- with no challenge and
no spoofing, so the user-agent here is honest. Keep it that way.

metasrc has the data too and is off limits: its robots.txt names ClaudeBot
with ``Disallow: /``, it puts the paginated augment rows behind
``Disallow: /api/`` for every agent, its terms forbid extraction beyond
indexing, and reaching it at all needs a spoofed browser fingerprint.

Two things op.gg does not publish, and that the pages therefore do not show:
a build against a named lane opponent (its counters carry each matchup's win
rate, but no build for it), and gold at fifteen.

Names and icons still come from the client, as everywhere else in this app;
only the numbers come from here.
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
    """Builds, counters and Arena data, for every mode tibbers supports."""

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

    # -- the laned modes ---------------------------------------------------
    #
    # The same host answers for Summoner's Rift, ARAM and URF, and answers
    # with the whole page in one request: items, runes, skills, summoners and
    # counters together. u.gg needs three separate files for that, each one
    # through a CDN that scores the TLS handshake and refuses roughly one
    # request in five -- which is why a 4 MB curl had to be downloaded on
    # Windows to speak to it at all. None of that applies here.
    #
    # What op.gg does not have is a build against a named lane opponent. Its
    # counters carry the win rate for each matchup but not a build tailored to
    # it, so that page is gone rather than faked from the general build.

    #: tibbers' role names (the client's) to op.gg's path segment.
    POSITIONS = {"top": "top", "jungle": "jungle", "middle": "mid",
                 "bottom": "adc", "utility": "support"}

    #: Mode to op.gg's path segment. A mode that assigns no lane is asked for
    #: with ``none``, which is the pooled row every game in it lands in.
    MODE_PATHS = {"rift": "ranked", "aram": "aram", "urf": "urf"}

    #: Below this many games a figure is noise rather than a recommendation.
    #: Flagged rather than hidden, so the number can be judged on the page.
    MIN_MATCHES = 100

    @staticmethod
    def _won(row: dict) -> dict:
        """Plain wins out of games played -- a Rift win rate, not a placement."""
        play = int(row.get("play") or 0)
        wins = int(row.get("win") or 0)
        return {"matches": play,
                "winRate": round(wins / play * 100, 2) if play else 0.0}

    @classmethod
    def _first(cls, rows: Optional[List[dict]]) -> dict:
        """op.gg orders every block by pick rate, so the first row is the
        recommendation. Returned empty rather than missing so callers can ask
        it for keys without guarding each one."""
        return (rows or [{}])[0] or {}

    #: The modes that insist on a real lane. `ranked` refuses `none` outright
    #: (HTTP 422, "The position must be one of the following types"), where
    #: ARAM and URF assign no lane and answer for the pooled row.
    LANED = ("ranked",)

    #: op.gg's position names back to the client's role names, for saying
    #: which lane a build is actually for.
    ROLES = {v: k for k, v in POSITIONS.items()}

    def roster(self, mode: str = "rift", region: str = "global") -> List[dict]:
        """Every champion's summary for a mode, in one file."""
        path = self.MODE_PATHS.get(mode, "ranked")
        payload = self._get(f"{BASE}/{region}/champions/{path}",
                            f"roster-{path}", ttl=CACHE_SECONDS)
        data = (payload or {}).get("data")
        return data if isinstance(data, list) else []

    def primary_position(self, champion_id: int, mode: str = "rift",
                         region: str = "global") -> str:
        """The lane this champion is actually played in most.

        Champ select does not always say which lane you are in -- blind pick
        never does, and a draft has not assigned one while you are still
        hovering. u.gg had a pooled row for that; op.gg refuses a build
        without a lane, so the lane has to be chosen, and the honest choice is
        the one the champion is played in. Read from the roster file, which is
        one request for all of them and is cached.
        """
        try:
            roster = self.roster(mode, region)
        except Unavailable:
            return "mid"
        for entry in roster:
            if int(entry.get("id") or 0) != int(champion_id):
                continue
            best, rate = "", -1.0
            for pos in entry.get("positions") or []:
                share = float((pos.get("stats") or {}).get("role_rate") or 0)
                if share > rate:
                    best, rate = str(pos.get("name", "")).lower(), share
            if best in self.ROLES:
                return best
        # Every champion is playable mid, and a build is better than a blank
        # page while champ select works out where you are going.
        return "mid"

    def laned(self, champion_id: int, role: Optional[str],
              mode: str = "rift", region: str = "global") -> dict:
        """The whole page op.gg has for one champion, mode and lane."""
        path = self.MODE_PATHS.get(mode)
        if path is None:
            raise Unavailable(f"op.gg has no build data for {mode}")
        position = self.POSITIONS.get((role or "").lower(), "none")
        if position == "none" and path in self.LANED:
            position = self.primary_position(champion_id, mode, region)
        url = f"{BASE}/{region}/champions/{path}/{int(champion_id)}/{position}"
        payload = self._get(url, f"{path}-{champion_id}-{position}")
        data = (payload or {}).get("data")
        if not data:
            raise Unavailable("op.gg has no build for this champion and role")
        # The patch the figures are for is in the payload itself, so there is
        # no separate manifest to resolve and no chance of asking for a patch
        # that has not been published yet.
        meta = (payload or {}).get("meta") or {}
        return {"data": data, "patch": meta.get("version") or "",
                "position": position}

    def build(self, champion_id: int, role: Optional[str],
              mode: str = "rift", region: str = "global") -> dict:
        """The recommended build, in the shape `guide.Guide.build` resolves.

        Deliberately the same raw shape u.gg produced, so the picker, the
        importer and the rune pages did not have to learn a second one. The
        one honest difference is the item slots: u.gg published a separate
        distribution for the fourth, fifth and sixth item, and op.gg publishes
        boots and one pooled list of late items. Those are reported as what
        they are rather than sliced into three to keep an old shape.
        """
        page = self.laned(champion_id, role, mode, region)
        data = page["data"]
        position = page["position"]

        def block(key: str) -> Optional[dict]:
            row = self._first(data.get(key))
            ids = [int(i) for i in (row.get("ids") or [])]
            return {"items": ids, **self._won(row)} if ids else None

        def options(key: str) -> List[dict]:
            out = []
            for row in data.get(key) or []:
                ids = [int(i) for i in (row.get("ids") or [])]
                if ids:
                    out.append({"itemId": ids[0], **self._won(row)})
            return out

        runes = self._first(data.get("runes"))
        primary = [int(i) for i in (runes.get("primary_rune_ids") or [])]
        spells = self._first(data.get("summoner_spells"))
        skills = self._first(data.get("skills"))
        mastery = self._first(data.get("skill_masteries"))

        # The position block carries this lane's games; the champion summary
        # carries every lane's. The lane is what the page is about.
        stats = {}
        for entry in ((data.get("summary") or {}).get("positions") or []):
            if str(entry.get("name", "")).lower() == position:
                stats = entry.get("stats") or {}
                break
        if not stats:
            stats = ((data.get("summary") or {}).get("average_stats")) or {}
        matches = int(stats.get("play") or 0)

        out: dict = {
            "patch": page["patch"],
            # The lane actually read, which is not always the one asked for:
            # champ select may not have assigned one yet, and op.gg has no
            # pooled row to fall back on.
            "role": role or self.ROLES.get(position),
            "matchup": None,
            "matches": matches,
            "thin": matches < self.MIN_MATCHES,
            "overall": {"matches": matches,
                        "winRate": round(float(stats.get("win_rate") or 0) * 100, 2)},
            "general": None,
            "start": block("starter_items"),
            "core": block("core_items"),
            "boots": options("boots"),
            "late": options("last_items"),
        }
        if primary:
            out["runes"] = {
                "keystone": primary[0],
                "primary": primary[1:],
                "primaryTree": int(runes.get("primary_page_id") or 0),
                "secondary": [int(i) for i in
                              (runes.get("secondary_rune_ids") or [])],
                "secondaryTree": int(runes.get("secondary_page_id") or 0),
                **self._won(runes),
            }
            out["shards"] = {"ids": [int(i) for i in
                                     (runes.get("stat_mod_ids") or [])]}
        if spells.get("ids"):
            out["spells"] = {"ids": [int(i) for i in spells["ids"]],
                             **self._won(spells)}
        if skills.get("order"):
            out["skills"] = {"order": [str(s) for s in skills["order"]],
                             "priority": [str(s) for s in
                                          (mastery.get("ids") or [])],
                             **self._won(skills)}
        return out

    def counters(self, champion_id: int, role: Optional[str],
                 mode: str = "rift", region: str = "global") -> List[dict]:
        """Every opponent met in this lane, worst first.

        The same request the build came from, so the counters page costs
        nothing extra once the build has been fetched.
        """
        data = self.laned(champion_id, role, mode, region)["data"]
        out = []
        for row in data.get("counters") or []:
            cid = int(row.get("champion_id") or 0)
            play = int(row.get("play") or 0)
            if not cid or not play:
                continue
            out.append({"championId": cid, **self._won(row)})
        out.sort(key=lambda r: r["winRate"])
        return out

    def skills(self, champion_id: int, region: str = "global") -> dict:
        data = self.arena(champion_id, region)["data"]
        mastery = (data.get("skill_masteries") or [{}])[0]
        order = (data.get("skills") or [{}])[0]
        return {"priority": [str(s) for s in (mastery.get("ids") or [])],
                "order": [str(s) for s in (order.get("order") or [])],
                **self._rate(mastery if mastery else {})}
