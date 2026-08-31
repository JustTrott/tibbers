#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The build guide: u.gg's numbers, wearing the client's own names and icons.

Everything is resolved here rather than in the browser. The picker would
otherwise need the item, rune and champion dictionaries to render a single
build -- 700KB of JSON to put six icons on screen -- and would have to repeat
the same joins on every poll.

Runes come back as a flat list of six perk ids with no indication of where
they sit, so the tree is rebuilt around them: a rune's meaning is partly its
row, and a page that lists them without their tree is a page that cannot be
read at a glance.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional

from .ugg import UGG, Unavailable

log = logging.getLogger("tibbers.guide")

#: The public pages behind the numbers, so every table can say where it came
#: from and open the page it is a rendering of. Slugs are the champion's
#: alias, lowercased -- both sites accept exactly that form (``monkeyking``,
#: ``nunu``, ``renata``), where slugs derived from display names 404.
UGG_PAGE = "https://u.gg/lol/champions"
OPGG_ARENA_PAGE = "https://op.gg/lol/modes/arena"

#: u.gg spells roles differently from the LCU; unmapped roles are left off
#: the URL and the page falls back to the champion's recommended role.
UGG_ROLE = {"top": "top", "jungle": "jungle", "middle": "mid",
            "bottom": "adc", "utility": "support"}

#: Standard deviation of a uniform finish, used as the spread for one
#: augment's average finish. op.gg publishes no per-augment variance, so this
#: stands in for it.
#:
#: 2.29 is the standard deviation of a uniform draw over positions 1 to 8.
#: The live mode measures as six teams of three -- pooled ``total_place/play``
#: over every champion is exactly 3.50 and ``first_place/play`` exactly one
#: sixth -- for which the true figure is 1.71. Keeping 2.29 makes the standard
#: error about a third too wide, which pulls every z-score toward zero and so
#: hands out fewer letters at both ends. That is the conservative direction
#: and it is the constant the tier bands were reviewed against, so it stands
#: until the bands are re-cut with it.
FINISH_SD = 2.29

#: Below this many games on one augment, a letter would be a claim the sample
#: cannot support, so the count is shown instead.
TIER_GATE = 50

#: z-score floors, best first. Above 2.5 is S+, down to C for everything left.
TIER_BANDS = ((2.5, "S+"), (1.25, "S"), (0.0, "A"), (-1.25, "B"))


class Guide:
    """Builds a fully resolved guide for one champion, role and matchup."""

    def __init__(self, gamedata, ugg: Optional[UGG] = None):
        self.gamedata = gamedata
        self.ugg = ugg or UGG()
        self._trees: Optional[Dict[int, int]] = None
        #: Built on first use, and only in Arena. Everything else in the app
        #: reaches u.gg, so a session that never opens Arena never makes one.
        self._opgg = None

    def opgg(self):
        """The op.gg client, made once.

        Its cache lives on the instance, so a second one would refetch the
        whole roster file the tier list is drawn from.
        """
        if self._opgg is None:
            from .opgg import OPGG
            self._opgg = OPGG()
        return self._opgg

    # -- resolution --------------------------------------------------------

    def trees(self) -> Dict[int, int]:
        """Which tree each rune belongs to, from the client's own perk data.

        u.gg gives six runes in one list; only this tells primary from
        secondary.
        """
        if self._trees is not None:
            return self._trees
        trees: Dict[int, int] = {}
        styles = (self.gamedata._load("perkstyles") or {}).get("styles") or []
        for style in styles:
            for slot in style.get("slots") or []:
                for perk in slot.get("perks") or []:
                    trees[int(perk)] = int(style["id"])
        self._trees = trees
        return trees

    def _rune(self, perk_id: int, picked: bool = False) -> dict:
        entry = self.gamedata.rune(perk_id) or {}
        return {"id": perk_id, "name": entry.get("name") or "",
                "icon": entry.get("icon") or "", "picked": picked}

    def _item(self, item_id: int) -> dict:
        entry = self.gamedata.item(item_id) or {}
        return {"id": item_id, "name": entry.get("name") or "",
                "icon": entry.get("icon") or ""}

    def _tree_block(self, style_id: int, chosen: List[int]) -> dict:
        """A tree with its real shape, and the picks marked within it."""
        tree = self.gamedata.tree(style_id) or {}
        picked = set(chosen)
        rows = [[self._rune(p, p in picked) for p in row]
                for row in self.gamedata.tree_rows(style_id)]
        return {"id": style_id, "name": tree.get("name") or "",
                "icon": tree.get("icon") or "", "rows": rows}

    def _slug(self, champion_id: int) -> str:
        return self.gamedata.champion_alias(champion_id).lower()

    def _ugg_source(self, champion_id: int, page: str, role: Optional[str],
                    queue: str) -> Optional[dict]:
        """The u.gg page these numbers are a rendering of."""
        slug = self._slug(champion_id)
        if not slug:
            return None
        from .modes import UGG_ARAM
        if queue == UGG_ARAM:
            url = f"{UGG_PAGE}/aram/{slug}-aram"
        else:
            url = f"{UGG_PAGE}/{slug}/{page}"
            mapped = UGG_ROLE.get(role or "")
            if mapped:
                url += f"?role={mapped}"
        return {"name": "u.gg", "url": url}

    def _slot(self, block: Optional[dict], key: str = "items") -> Optional[dict]:
        if not block:
            return None
        return {"winRate": block.get("winRate", 0.0),
                "matches": block.get("matches", 0),
                "items": [self._item(i) for i in block.get(key) or []]}

    def _options(self, rows: Optional[List[dict]]) -> List[dict]:
        out = []
        for row in rows or []:
            entry = self._item(row["itemId"])
            entry.update(winRate=row["winRate"], matches=row["matches"])
            out.append(entry)
        return out

    # -- public ------------------------------------------------------------

    def build(self, champion_id: int, role: Optional[str],
              opponent_id: Optional[int] = None,
              queue: str = "ranked_solo_5x5",
              patch: Optional[str] = None) -> dict:
        raw = self.ugg.build_with_fallback(champion_id, role, opponent_id,
                                           queue, self.trees(), patch)
        out: dict = {
            "patch": raw.get("patch"),
            "role": role,
            "opponentId": opponent_id,
            "thin": raw.get("thin", False),
            "matches": raw.get("matches", 0),
            "overall": raw.get("overall") or {},
            "general": raw.get("general"),
        }

        runes = raw.get("runes") or {}
        if runes:
            primary_picks = ([runes["keystone"]] if runes.get("keystone") else []) \
                + list(runes.get("primary") or [])
            out["runes"] = {
                "winRate": runes.get("winRate", 0.0),
                "matches": runes.get("matches", 0),
                "primary": self._tree_block(runes["primaryTree"], primary_picks),
                "secondary": self._tree_block(runes["secondaryTree"],
                                              runes.get("secondary") or []),
                "keystoneId": runes.get("keystone"),
            }

        shards = raw.get("shards") or {}
        if shards:
            out["shards"] = [self._rune(s, True) for s in shards.get("ids") or []]

        spells = raw.get("spells") or {}
        if spells:
            resolved = []
            for spell_id in spells.get("ids") or []:
                entry = self.gamedata.spell(spell_id) or {}
                resolved.append({"id": spell_id, "name": entry.get("name") or "",
                                 "icon": entry.get("icon") or ""})
            out["spells"] = {"winRate": spells.get("winRate", 0.0),
                             "matches": spells.get("matches", 0),
                             "spells": resolved}

        skills = raw.get("skills") or {}
        if skills:
            abilities = self.gamedata.abilities(champion_id)
            order = skills.get("order") or []
            out["skills"] = {
                "winRate": skills.get("winRate", 0.0),
                "matches": skills.get("matches", 0),
                "priority": list(skills.get("priority") or ""),
                "order": order,
                # Rows in the client's own order, each carrying the levels it
                # is taken at -- the shape u.gg draws, and the one that can be
                # read without counting along a line of letters.
                "rows": [{
                    "key": key.upper(),
                    "name": (abilities.get(key) or {}).get("name", ""),
                    "icon": (abilities.get(key) or {}).get("icon", ""),
                    "levels": [i + 1 for i, s in enumerate(order)
                               if s.upper() == key.upper()],
                } for key in ("q", "w", "e", "r")],
            }

        out["start"] = self._slot(raw.get("start"))
        out["core"] = self._slot(raw.get("core"))
        for slot in ("fourth", "fifth", "sixth"):
            out[slot] = self._options(raw.get(slot))
        return out

    def pair(self, champion_id: int, role: Optional[str],
             opponent_id: Optional[int] = None,
             queue: str = "ranked_solo_5x5",
             patch: Optional[str] = None) -> dict:
        """Both builds at once: the general one, and the one for this matchup.

        Sent together so the picker can switch between them without waiting
        on anything. They answer different questions -- the general build is
        what the champion wants, the matchup is what it wants against this
        opponent -- and which to trust is decided by the sample sizes, which
        only makes sense with both in front of you.
        """
        general = self.build(champion_id, role, None, queue, patch)
        out = {"general": general, "matchup": None,
               "patch": general.get("patch"), "role": role,
               "opponentId": opponent_id,
               "source": self._ugg_source(champion_id, "build", role, queue)}
        if not opponent_id:
            return out
        try:
            out["matchup"] = self.build(champion_id, role, opponent_id,
                                        queue, patch)
        except Unavailable as exc:
            out["matchupError"] = str(exc)
        return out

    @staticmethod
    def rate_augments(rows: List[dict], rarity: str) -> List[dict]:
        """Score one rarity's augments against their own pool.

        The comparison that matters is the one the game actually offers: when
        three prismatics appear, the question is which of *those* to take, not
        how they rank against silvers. So the baseline is the pick-weighted
        mean average finish of this rarity for this champion -- the pool the
        choice is drawn from -- and a letter says "best among prismatics for
        this champion", never "best in the game".

        Measured by average finish rather than first place: six teams finish
        somewhere, and counting only the winner throws five sixths of every
        game away. Lower is better, so the sign is flipped into the z-score.
        """
        played = [r for r in rows if (r.get("matches") or 0) > 0
                  and r.get("avgFinish") is not None]
        total = sum(r["matches"] for r in played)
        places = sum(r["avgFinish"] * r["matches"] for r in played)
        baseline = places / total if total else None

        out = []
        for row in rows:
            matches = row.get("matches") or 0
            finish = row.get("avgFinish")
            tier = None
            if baseline is not None and finish is not None and matches >= TIER_GATE:
                spread = FINISH_SD / math.sqrt(matches)
                score = (baseline - finish) / spread if spread else 0.0
                tier = "C"
                for floor, letter in TIER_BANDS:
                    if score >= floor:
                        tier = letter
                        break
            out.append({**row, "rarity": rarity, "tier": tier,
                        "baseline": round(baseline, 2) if baseline is not None else None})
        # Tier first, then average finish, which is what breaks ties inside a
        # band and is the measure the band was cut from.
        rank = {t: i for i, (_, t) in enumerate(TIER_BANDS)}
        out.sort(key=lambda r: (r["tier"] is None,
                                rank.get(r["tier"], len(TIER_BANDS)),
                                r["avgFinish"] if r["avgFinish"] is not None else 9))
        return out

    def arena(self, champion_id: int) -> dict:
        """The Arena page: augments first, then items and skills.

        Ordered by what actually decides an Arena game. Augments are picked
        three times a match and change how a champion works; items are the
        smaller half of the decision, which is the reverse of every other
        mode.
        """
        from .opgg import Unavailable as OPGGUnavailable

        opgg = self.opgg()
        try:
            meta = opgg.arena(champion_id)
            groups = opgg.augments(champion_id)
            items = opgg.items(champion_id)
            skills = opgg.skills(champion_id)
        except OPGGUnavailable as exc:
            raise Unavailable(str(exc)) from exc

        def dress(rows):
            out = []
            for row in rows:
                first = self._item(row["ids"][0])
                out.append({**row, "items": [self._item(i) for i in row["ids"]],
                            "name": first["name"], "icon": first["icon"]})
            return out

        # One flat table rather than a section per rarity: the rarity is a
        # ring on the icon, which is what lets all 45 share a single ranking.
        augments = []
        for group in groups:
            named = [{**a, **(self.gamedata.augment(a["id"]) or
                              {"name": "", "icon": ""})}
                     for a in group["augments"]]
            augments.extend(self.rate_augments(named, group["rarity"]))

        slug = self._slug(champion_id)
        return {
            "state": "ready", "mode": "arena", "patch": meta.get("patch"),
            "augments": augments,
            "arenaItems": {k: dress(v) for k, v in items.items()},
            "arenaSkills": skills,
            "source": {"name": "op.gg",
                       "url": f"{OPGG_ARENA_PAGE}/{slug}/build"} if slug else None,
        }

    def arena_tiers(self, hovering: Optional[int] = None) -> dict:
        """Every champion in Arena, as op.gg ranks them.

        The champ-select question in Arena is not "how do I build this", it
        is "is this worth picking at all", and that is a whole-roster
        question. One file answers it.
        """
        from .opgg import Unavailable as OPGGUnavailable

        try:
            listing = self.opgg().champions()
        except OPGGUnavailable as exc:
            raise Unavailable(str(exc)) from exc

        rows = []
        for row in listing["champions"]:
            champ = self.gamedata.champion(row["championId"]) or {}
            rows.append({**row, "name": champ.get("name") or "",
                         "icon": champ.get("icon") or ""})
        return {"patch": listing.get("patch"), "champions": rows,
                "hovering": hovering, "total": len(rows),
                "source": {"name": "op.gg", "url": OPGG_ARENA_PAGE}}

    def counter_table(self, subject_id: int, role: Optional[str],
                      mine: Optional[int] = None,
                      queue: str = "ranked_solo_5x5", limit: int = 60,
                      patch: Optional[str] = None) -> Optional[dict]:
        """The champions in YOUR role that beat `subject_id`, best first.

        One shape answers both directions the page offers. Asked about the
        lane opponent it says what to pick; asked about your own champion it
        says what to fear. Only the subject changes, which is why they are one
        table with a switch above it rather than two designs stacked.

        The role is yours, not theirs. Looking an enemy up under your lane
        gives the champions who actually face them there -- ask about an enemy
        ADC as an ADC and the answer is other ADCs, which is the only answer
        worth having. Champ select never reveals an enemy's role anyway, so
        theirs could only ever be assumed.

        Every figure is the *listed* champion's, not the subject's. u.gg
        stores the subject's, so the win rate and the gold line are both
        turned around here rather than in the page.

        `pickShare` is the column that makes the list safe to act on. Without
        it the honest top of any ADC's counter list is a row of mages played
        bottom in under two percent of games, recommended with total
        confidence.
        """
        try:
            table = self.ugg.matchup_table(subject_id, role, queue, patch=patch)
        except Unavailable:
            return None
        if not table:
            return None

        usable = [r for r in table if r["share"] >= 0.005]

        def dress(row: dict) -> dict:
            champ = self.gamedata.champion(row["championId"]) or {}
            out = {"championId": row["championId"],
                   "name": champ.get("name") or "",
                   "icon": champ.get("icon") or "",
                   "winRate": round(100 - row["winRate"], 2),
                   "matches": row["matches"],
                   "pickShare": round(row["share"] * 100, 2)}
            if row.get("goldAt15") is not None:
                out["goldAt15"] = round(-row["goldAt15"], 1)
            return out

        ranked = sorted(usable, key=lambda r: r["winRate"])
        rows = [dress(r) for r in ranked[:limit]]

        me = None
        if mine:
            at = next((i for i, r in enumerate(ranked)
                       if r["championId"] == mine), None)
            if at is not None:
                # Inserted at its true rank rather than exiled to a widget of
                # its own: "where do I actually sit" is a question about
                # position, and a number beside the list cannot answer it.
                me = {**dress(ranked[at]), "rank": at + 1, "of": len(ranked)}

        subject = self.gamedata.champion(subject_id) or {}
        return {"championId": subject_id, "name": subject.get("name") or "",
                "icon": subject.get("icon") or "", "role": role,
                "rows": rows, "mine": me, "total": len(ranked),
                "source": self._ugg_source(subject_id, "counter", role, queue)}

    def against(self, champion_id: int, role: Optional[str],
                enemies: List[int], queue: str = "ranked_solo_5x5",
                patch: Optional[str] = None) -> List[dict]:
        """How this champion fares against each enemy actually in the game.

        The general counter list answers "what beats me"; this answers "how am
        I doing against the five people I am about to play", which is the
        question champ select is actually asking. Enemies with no record are
        still listed -- a missing number is information too, and dropping them
        would make the row disagree with the team on screen.
        """
        if not enemies:
            return []
        try:
            table = {r["championId"]: r for r in self.ugg.matchup_table(
                champion_id, role, queue, patch=patch)}
        except Unavailable:
            return []
        out = []
        for enemy in enemies:
            champ = self.gamedata.champion(enemy) or {}
            row = table.get(enemy)
            entry = {"championId": enemy,
                     "name": champ.get("name") or "",
                     "icon": champ.get("icon") or "",
                     "winRate": row["winRate"] if row else None,
                     "matches": row["matches"] if row else 0}
            # The lane row's headline reads off this: gold at fifteen is the
            # one number that says how the lane will actually feel, and it is
            # already in the payload the win rate came from.
            if row and row.get("goldAt15") is not None:
                entry["goldAt15"] = row["goldAt15"]
            out.append(entry)
        out.sort(key=lambda r: (r["winRate"] is None, r["winRate"] or 0))
        return out

    def suggest_opponent(self, champion_id: int, role: Optional[str],
                         enemies: List[int], queue: str = "ranked_solo_5x5",
                         patch: Optional[str] = None) -> Optional[int]:
        """Which locked enemy most often meets this champion in this role."""
        if not enemies:
            return None
        try:
            samples = self.ugg.opponent_samples(champion_id, role, queue, patch)
        except Unavailable:
            return None
        ranked = sorted(enemies, key=lambda e: samples.get(e, 0), reverse=True)
        return ranked[0] if samples.get(ranked[0], 0) else None
