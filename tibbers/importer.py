#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Writing the build that is on screen into the League client.

Reading the client is harmless; writing to it is not. Three things are
written, and each one overwrites something the player owns:

* a **rune page**, of which an account has three,
* the **summoner spells** for the champ select you are in,
* an **item set**, which is what puts the build in the in-game shop.

So the rules here are about what is *not* touched. Tibbers owns exactly one
rune page, named ``Tibbers: <Champion>``, and exactly one item set. Every
other page and every other set is left alone -- the item set list is written
back whole, with the other sets carried across unchanged, because the endpoint
takes the whole list and a partial write would delete them.

An account with all three rune slots full cannot be given a fourth, and the
only way to make room is to delete one of the player's own pages. That is
never done on our own initiative: the caller is told ``needsSlot`` and has to
come back naming the page to replace. Auto-import stops there and never asks
again, because a prompt nobody is looking at is not consent.

The payload builders are plain functions over the resolved guide -- the same
dict the picker renders -- so what gets sent can be tested without a client.
"""

from __future__ import annotations

import itertools
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("tibbers.importer")

#: Rune pages are named for the champion they were built for. The client's
#: own editor stops at 25 characters; the API itself takes more (Porofessor
#: writes 31), so this is the conservative bound rather than the real one.
#: "Tibbers: Nunu & Willump" is the longest name this can produce, at 23.
NAME_LIMIT = 25
NAME_PREFIX = "Tibbers: "

#: Flash is the one spell people have a key for. Wherever it is, it stays.
FLASH = 4

#: The item set Tibbers owns. A fixed uid means re-importing replaces it
#: rather than adding another one every game.
SET_UID = "71bbe125-0000-4000-8000-000000000001"
SET_PREFIX = "Tibbers: "

#: Sets are listed by sortrank, highest first -- the client's own recommended
#: pages sit in the tens of thousands. High enough to be the first tab, not so
#: high that it looks like a sentinel.
SET_SORTRANK = 100000

#: Maps an item set may be associated with. Anything else (Nexus Blitz, the
#: rotating 453) has no set support, so those fall back to the Rift.
KNOWN_MAPS = (11, 12, 30)

#: Short mode names for the item set title. The queue's own label is right
#: for a status line and too long for a shop tab.
MODE_TITLES = {"rift": "Rift", "swiftplay": "Swiftplay", "urf": "URF",
               "aram": "ARAM", "mayhem": "Mayhem", "arena": "Arena"}


class Incomplete(Exception):
    """The build on screen does not carry what this write needs."""


# ── rune page ────────────────────────────────────────────────────────────

def page_name(champion_name: str, limit: int = NAME_LIMIT) -> str:
    """``Tibbers: <Champion>``, short enough for the client to accept."""
    name = f"{NAME_PREFIX}{(champion_name or 'build').strip()}"
    return name if len(name) <= limit else name[:limit].rstrip()


def owns_page(page: dict) -> bool:
    return str((page or {}).get("name") or "").startswith(NAME_PREFIX)


def _picked(tree: Optional[dict]) -> List[int]:
    """The chosen perks of one tree, in the order its rows are drawn.

    The order is the whole point. ``selectedPerkIds`` is positional -- the
    client reads it as keystone, three primary rows, two secondary rows, three
    shards -- and u.gg hands the runes over as one flat unordered list. The
    resolved guide has already rebuilt the tree, so walking its rows puts them
    back in the order the client expects without a second source of truth.
    """
    out = []
    for row in (tree or {}).get("rows") or []:
        for perk in row or []:
            if perk.get("picked") and perk.get("id"):
                out.append(int(perk["id"]))
    return out


def order_shards(ids: Sequence[int],
                 stat_rows: Optional[Sequence[Sequence[int]]] = None) -> List[int]:
    """Stat shards in offense / flex / defence order.

    ``selectedPerkIds`` ends with one shard per row, in row order, and u.gg
    hands them over as an unordered three. The rows overlap -- health scaling
    is in two of them, adaptive force in two others -- so no shard can be
    placed by looking at it alone: taking rows in turn and giving each the
    first shard that fits gets [health, adaptive, movement] wrong, because the
    health shard fits the flex row that the movement shard needs.

    Three shards over three rows is six arrangements, so the right one is
    found rather than approximated. A set that fits none of them is left
    exactly as it arrived rather than reordered on a guess.
    """
    ids = [int(i) for i in ids]
    if not stat_rows or len(ids) != len(stat_rows):
        return ids
    rows = [{int(p) for p in row or []} for row in stat_rows]
    for arrangement in itertools.permutations(ids):
        if all(shard in row for shard, row in zip(arrangement, rows)):
            return list(arrangement)
    return ids


def perk_selection(build: dict,
                   stat_rows: Optional[Sequence[Sequence[int]]] = None) -> dict:
    """The three fields a rune page is: two style ids and nine perk ids."""
    runes = (build or {}).get("runes") or {}
    primary, secondary = runes.get("primary") or {}, runes.get("secondary") or {}
    if not primary.get("id") or not secondary.get("id"):
        raise Incomplete("this build has no runes")

    keystone_and_primary = _picked(primary)
    secondary_picks = _picked(secondary)
    shards = order_shards([s["id"] for s in (build.get("shards") or [])
                           if s.get("id")], stat_rows)

    if len(keystone_and_primary) != 4 or len(secondary_picks) != 2:
        raise Incomplete(
            f"expected 4 primary and 2 secondary runes, "
            f"got {len(keystone_and_primary)} and {len(secondary_picks)}")
    if len(shards) != 3:
        raise Incomplete(f"expected 3 stat shards, got {len(shards)}")

    return {
        "primaryStyleId": int(primary["id"]),
        "subStyleId": int(secondary["id"]),
        "selectedPerkIds": keystone_and_primary + secondary_picks + shards,
    }


def rune_page(build: dict, champion_name: str,
              stat_rows: Optional[Sequence[Sequence[int]]] = None) -> dict:
    """The whole page body, as the client's own pages are shaped.

    Only the fields the client actually reads on a write. Everything else it
    returns on a GET -- the icon paths, ``uiPerks``, the tooltip background --
    is derived from these and filled in by the client itself.
    """
    page = {"name": page_name(champion_name), "current": True}
    page.update(perk_selection(build, stat_rows))
    return page


# ── summoner spells ──────────────────────────────────────────────────────

def spell_pair(build: dict) -> Tuple[int, int]:
    """The two spells the build recommends."""
    ids = [int(s["id"]) for s in ((build or {}).get("spells") or {}).get("spells") or []
           if s.get("id")]
    if len(ids) < 2:
        raise Incomplete("this build has no summoner spells")
    return ids[0], ids[1]


def place_spells(wanted: Tuple[int, int],
                 current: Tuple[Optional[int], Optional[int]]
                 ) -> Optional[Tuple[int, int]]:
    """Which key each spell goes on, keeping the habit intact.

    Muscle memory is on the key, not on the spell: someone who flashes with D
    should still flash with D after an import. So Flash keeps the key it is
    already on, and failing that any spell the player already has keeps its
    key. Returns None when the pair is already set, in either order, so an
    import that changes nothing sends nothing.
    """
    first, second = int(wanted[0]), int(wanted[1])
    one, two = current

    if {first, second} == {one, two}:
        return None

    def split(keep: int) -> int:
        return second if keep == first else first

    if FLASH in (first, second) and FLASH in (one, two):
        other = split(FLASH)
        return (FLASH, other) if one == FLASH else (other, FLASH)

    for held in (one, two):
        if held in (first, second):
            other = split(held)
            return (held, other) if held == one else (other, held)

    return first, second


# ── item set ─────────────────────────────────────────────────────────────

def owns_set(item_set: dict) -> bool:
    item_set = item_set or {}
    return (item_set.get("uid") == SET_UID
            or str(item_set.get("title") or "").startswith(SET_PREFIX))


def _items(ids: Sequence[Any]) -> List[dict]:
    """Item ids as the client stores them: strings, with runs counted.

    Two health potions in a starting block are one entry with a count of two,
    which is what the client writes itself and what the shop draws.
    """
    out: List[dict] = []
    for raw in ids:
        item_id = str(int(raw))
        if out and out[-1]["id"] == item_id:
            out[-1]["count"] += 1
        else:
            out.append({"count": 1, "id": item_id})
    return out


def _block(title: str, ids: Sequence[Any]) -> Optional[dict]:
    items = _items(ids)
    if not items:
        return None
    return {"hideIfSummonerSpell": "", "items": items,
            "showIfSummonerSpell": "", "type": title}


def _rate(block: Optional[dict]) -> str:
    rate = (block or {}).get("winRate") or 0
    return f" · {rate:.1f}% win" if rate else ""


def build_blocks(build: dict) -> List[dict]:
    """The shop's tabs for a Rift, Swiftplay, URF or ARAM build.

    No boots tab: u.gg publishes no boots slot for these modes, so the block
    that looked for one could never fill. Boots come back in Arena, where
    op.gg does publish them, and that goes through `arena_blocks`.
    """
    build = build or {}
    blocks = []
    start, core = build.get("start") or {}, build.get("core") or {}

    for title, block in (("Starting items" + _rate(start), start),
                         ("Core build" + _rate(core), core)):
        made = _block(title, [i["id"] for i in block.get("items") or []])
        if made:
            blocks.append(made)

    for label, key in (("4th item", "fourth"), ("5th item", "fifth"),
                       ("6th item", "sixth")):
        made = _block(label, [o["id"] for o in (build.get(key) or [])[:3]])
        if made:
            blocks.append(made)
    return blocks


def arena_blocks(build: dict) -> List[dict]:
    """Arena has no runes; the items are all there is to import.

    op.gg publishes item *combinations* rather than a single build, so each
    tab is the distinct items across the best few rows -- the shop shows a
    tab, not an argument, and a flattened shortlist is what is useful there.
    """
    groups = (build or {}).get("arenaItems") or {}
    blocks = []
    for label, key, rows in (("Prismatic", "prismatic", 3),
                             ("Core build", "core", 3),
                             ("Boots", "boots", 2)):
        seen: List[int] = []
        for row in (groups.get(key) or [])[:rows]:
            for item in row.get("items") or []:
                if item.get("id") and item["id"] not in seen:
                    seen.append(int(item["id"]))
        made = _block(label, seen[:8])
        if made:
            blocks.append(made)
    return blocks


def set_title(champion_name: str, kind: Optional[str]) -> str:
    mode = MODE_TITLES.get(str(kind or "").lower())
    return f"{SET_PREFIX}{champion_name}" + (f" {mode}" if mode else "")


def item_set(build: dict, champion_id: int, champion_name: str,
             kind: Optional[str] = None, map_id: Optional[int] = None,
             arena: bool = False) -> dict:
    """One item set, shaped exactly like the ones already on the account."""
    blocks = arena_blocks(build) if arena else build_blocks(build)
    if not blocks:
        raise Incomplete("this build has no items")
    return {
        "associatedChampions": [int(champion_id)],
        "associatedMaps": [int(map_id) if map_id in KNOWN_MAPS else 11],
        "blocks": blocks,
        # The legacy pair. Every set on the account carries them as "any"
        # while `associatedMaps` does the real work, so they are written the
        # same way rather than left out.
        "map": "any",
        "mode": "any",
        "preferredItemSlots": [],
        "sortrank": SET_SORTRANK,
        "startedFrom": "blank",
        "title": set_title(champion_name, kind),
        "type": "custom",
        "uid": SET_UID,
    }


def merge_sets(document: Optional[dict], new_set: dict) -> dict:
    """The whole list back, with only Tibbers' own set replaced.

    The endpoint takes the document, not a diff, so anything dropped here is
    deleted from the account. Every foreign set is carried across untouched
    and any stray Tibbers set from an earlier scheme is swept up with it.
    """
    document = document or {}
    kept = [s for s in (document.get("itemSets") or []) if not owns_set(s)]
    return {**document, "itemSets": kept + [new_set],
            "timestamp": int(time.time() * 1000)}


# ── the writes themselves ────────────────────────────────────────────────

def _ok(status: int) -> bool:
    return 200 <= status < 300


def _why(status: int, body: Any) -> str:
    if isinstance(body, dict):
        detail = body.get("message") or body.get("errorCode")
        if detail:
            return f"{detail} ({status})" if status else str(detail)
    if isinstance(body, str) and body.strip():
        return f"{body.strip()[:120]} ({status})"
    return f"the client refused it ({status})" if status else "the client did not answer"


class Importer:
    """Sends the three writes, and reports exactly what each one did."""

    def __init__(self, get_lcu: Callable[[], Any],
                 say: Optional[Callable[[str], None]] = None,
                 stat_rows: Optional[Callable[[], Any]] = None,
                 dry_run: bool = False):
        self.get_lcu = get_lcu
        self.say = say or (lambda message: None)
        self.stat_rows = stat_rows
        #: A dev instance talks to the same client the real one does. Nothing
        #: separates them except this, so it defaults to the safe side and is
        #: turned off only by the instance that owns the account.
        self.dry_run = dry_run

    # -- transport ---------------------------------------------------------

    def _send(self, client, method: str, endpoint: str, body=None):
        if self.dry_run:
            log.info("dry run: %s %s %s", method, endpoint,
                     _summarise(body))
            return 200, {"dryRun": True}
        log.info("%s %s %s", method, endpoint, _summarise(body))
        return client.send(method, endpoint, body)

    # -- runes -------------------------------------------------------------

    def import_runes(self, build: dict, champion_name: str,
                     replace_page_id: Optional[int] = None) -> dict:
        client = self.get_lcu()
        if client is None:
            return {"ok": False, "error": "the League client is not running"}

        try:
            stat_rows = self.stat_rows() if self.stat_rows else None
        except Exception as exc:  # noqa: BLE001
            # Without the rows the shards go in the order they arrived, which
            # can be the wrong one -- and silently, since a rune page with
            # three shards on it looks right either way.
            log.warning("could not read the stat shard rows: %s", exc)
            stat_rows = None
        try:
            page = rune_page(build, champion_name, stat_rows)
        except Incomplete as exc:
            return {"ok": False, "error": str(exc)}

        pages = client.get("/lol-perks/v1/pages")
        if not isinstance(pages, list):
            return {"ok": False, "error": "could not read your rune pages"}

        mine = next((p for p in pages if owns_page(p)), None)
        if mine is not None:
            status, body = self._send(client, "PUT",
                                      f"/lol-perks/v1/pages/{int(mine['id'])}", page)
            if not _ok(status):
                return {"ok": False, "error": _why(status, body)}
            return self._make_current(client, int(mine["id"]), page["name"], "updated")

        inventory = client.get("/lol-perks/v1/inventory") or {}
        if replace_page_id is None and not inventory.get("canAddCustomPage"):
            # Every slot is the player's. Which one to lose is their call and
            # nobody else's, so the answer is a question, not a deletion.
            return {"ok": False, "needsSlot": True,
                    "error": "all three rune pages are in use",
                    "pages": [describe_page(p) for p in pages if p.get("isDeletable")]}

        how = "created"
        if replace_page_id is not None:
            target = next((p for p in pages
                           if int(p.get("id", 0)) == int(replace_page_id)), None)
            if target is None:
                return {"ok": False, "error": "that rune page is gone"}
            if not target.get("isDeletable"):
                return {"ok": False, "error": f"{target.get('name')} cannot be deleted"}
            status, body = self._send(
                client, "DELETE", f"/lol-perks/v1/pages/{int(replace_page_id)}")
            if not _ok(status):
                return {"ok": False, "error": _why(status, body)}
            self.say(f"replaced rune page {target.get('name')!r}")
            how = "replaced"

        status, body = self._send(client, "POST", "/lol-perks/v1/pages", page)
        if not _ok(status):
            return {"ok": False, "error": _why(status, body)}
        new_id = (body or {}).get("id") if isinstance(body, dict) else None
        if not new_id and not self.dry_run:
            # The create normally answers with the page it made. If it ever
            # answers with nothing, the page still exists and can be found by
            # its name -- which is the one thing about it we chose.
            made = client.get("/lol-perks/v1/pages") or []
            new_id = next((p.get("id") for p in made if owns_page(p)), None)
        return self._make_current(client, new_id, page["name"], how)

    def _make_current(self, client, page_id, name: str, how: str) -> dict:
        result = {"ok": True, "name": name, "how": how, "id": page_id}
        if not page_id:
            return result
        status, body = self._send(client, "PUT", "/lol-perks/v1/currentpage", page_id)
        if not _ok(status):
            # The page is written either way; only the selection failed.
            result["note"] = "written, but not selected: " + _why(status, body)
        return result

    # -- spells ------------------------------------------------------------

    def import_spells(self, build: dict) -> dict:
        client = self.get_lcu()
        if client is None:
            return {"ok": False, "error": "the League client is not running"}

        session = client.champ_select()
        if not session:
            return {"ok": False, "error": "not in champ select"}
        cell_id = session.get("localPlayerCellId")
        mine = next((p for p in session.get("myTeam") or []
                     if p.get("cellId") == cell_id), None)
        if mine is None:
            return {"ok": False, "error": "not in champ select"}

        try:
            wanted = spell_pair(build)
        except Incomplete as exc:
            return {"ok": False, "error": str(exc)}

        current = (mine.get("spell1Id") or None, mine.get("spell2Id") or None)
        placed = place_spells(wanted, current)
        if placed is None:
            return {"ok": True, "unchanged": True, "spell1Id": current[0],
                    "spell2Id": current[1]}

        status, body = self._send(
            client, "PATCH", "/lol-champ-select/v1/session/my-selection",
            {"spell1Id": placed[0], "spell2Id": placed[1]})
        if not _ok(status):
            return {"ok": False, "error": _why(status, body)}
        return {"ok": True, "spell1Id": placed[0], "spell2Id": placed[1]}

    # -- item set ----------------------------------------------------------

    def import_items(self, build: dict, champion_id: int, champion_name: str,
                     kind: Optional[str] = None, map_id: Optional[int] = None,
                     arena: bool = False) -> dict:
        client = self.get_lcu()
        if client is None:
            return {"ok": False, "error": "the League client is not running"}

        summoner = client.current_summoner() or {}
        summoner_id = summoner.get("summonerId") or summoner.get("accountId")
        if not summoner_id:
            return {"ok": False, "error": "could not read your summoner id"}

        try:
            new_set = item_set(build, champion_id, champion_name, kind, map_id, arena)
        except Incomplete as exc:
            return {"ok": False, "error": str(exc)}

        endpoint = f"/lol-item-sets/v1/item-sets/{int(summoner_id)}/sets"
        existing = client.get(endpoint)
        if not isinstance(existing, dict):
            return {"ok": False, "error": "could not read your item sets"}
        # The account id belongs to the document, not to us: written back as
        # it came, so a client that keys on it still recognises the list.
        document = merge_sets(existing, new_set)

        status, body = self._send(client, "PUT", endpoint, document)
        if not _ok(status):
            return {"ok": False, "error": _why(status, body)}
        return {"ok": True, "title": new_set["title"],
                "blocks": [b["type"] for b in new_set["blocks"]],
                "kept": len(document["itemSets"]) - 1}

    # -- all three ---------------------------------------------------------

    def run(self, build: dict, champion_id: int, champion_name: str,
            what: str = "all", kind: Optional[str] = None,
            map_id: Optional[int] = None, arena: bool = False,
            spells: bool = True,
            replace_page_id: Optional[int] = None) -> dict:
        """Import the build, and say in one result what reached the client."""
        wants = {"runes": what in ("all", "runes"),
                 "spells": what in ("all", "spells") and spells and not arena,
                 "items": what in ("all", "items")}
        # Arena pages carry no runes at all, so asking is not a failure there.
        if arena:
            wants["runes"] = False

        out: Dict[str, Any] = {"ok": True, "dryRun": self.dry_run, "done": [],
                               "champion": champion_name, "mode": kind}
        failed: List[str] = []

        if wants["runes"]:
            out["runes"] = self.import_runes(build, champion_name, replace_page_id)
            if out["runes"].get("ok"):
                out["done"].append(out["runes"]["name"])
            else:
                failed.append("runes")
                if out["runes"].get("needsSlot"):
                    out["needsSlot"] = True
                    out["pages"] = out["runes"]["pages"]

        if wants["spells"]:
            out["spells"] = self.import_spells(build)
            if out["spells"].get("ok"):
                if not out["spells"].get("unchanged"):
                    out["done"].append("spells")
            elif out["spells"].get("error") == "not in champ select":
                # Reading a build after the game has started is the ordinary
                # case, not a failure: there is no selection left to set, and
                # the runes and the item set still went in.
                out["spells"]["skipped"] = True
            else:
                failed.append("spells")

        if wants["items"]:
            out["items"] = self.import_items(build, champion_id, champion_name,
                                             kind, map_id, arena)
            if out["items"].get("ok"):
                out["done"].append("items")
            else:
                failed.append("items")

        out["ok"] = not failed
        out["error"] = out[failed[0]].get("error") if failed else None
        summary = ", ".join(out["done"]) or "nothing"
        self.say(("would import " if self.dry_run else "imported ") + summary
                 + (f" -- {out['error']}" if out.get("error") else ""))
        return out


def describe_page(page: dict) -> dict:
    """One rune page, as little of it as the chooser needs to be read."""
    keystone = page.get("pageKeystone") or {}
    return {"id": page.get("id"), "name": page.get("name") or "",
            "current": bool(page.get("current")),
            "keystone": keystone.get("name") or "",
            "championId": page.get("recommendationChampionId") or None}


def _summarise(body: Any) -> str:
    """What went out, short enough for a log line to stay one line."""
    if body is None:
        return ""
    if isinstance(body, int):
        return f"page {body}"
    if isinstance(body, dict):
        if "selectedPerkIds" in body:
            return (f"{body.get('name')!r} {body.get('primaryStyleId')}/"
                    f"{body.get('subStyleId')} {body.get('selectedPerkIds')}")
        if "spell1Id" in body:
            return f"spells {body.get('spell1Id')}/{body.get('spell2Id')}"
        if "itemSets" in body:
            sets = body.get("itemSets") or []
            mine = next((s for s in sets if owns_set(s)), {})
            return (f"{len(sets)} item sets, ours {mine.get('title')!r} "
                    f"({len(mine.get('blocks') or [])} blocks)")
    return str(body)[:160]
