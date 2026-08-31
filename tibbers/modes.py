#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
What game mode we are in, and what can be said about it.

Skins work everywhere. The patcher hooks file reads in the running game and
neither knows nor cares which map it is on, so the only thing that ever
stopped a mode working was failing to recognise it.

Build data is the part that varies. Read from the client's own queue list
rather than assumed, because the strings are not what you would guess:

* ARAM Mayhem reports its mode as ``KIWI``, not ``ARAM``.
* Swiftplay is its own mode string, on Summoner's Rift.
* Arena is ``CHERRY``, on map 30, and its current queue is 1750.
* ``JADE`` on map 453 appeared without announcement and describes itself as
  Classic, so it is treated as a Rift.

Matching on the mode string and map rather than on a list of queue ids means
a new queue in a mode we already know keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: u.gg queue paths that actually carry data. Measured, not assumed: of the
#: queues u.gg publishes, only these two exist beyond ranked Summoner's Rift.
#: `arena`, `nexus_blitz`, `pick_urf`, `urf` and `quickplay` all 404 on every
#: patch tried.
UGG_RANKED = "ranked_solo_5x5"
UGG_ARAM = "normal_aram"
UGG_SWIFTPLAY = "swiftplay"

#: Queue ids that have their own u.gg data, where the mode alone is too coarse.
UGG_BY_QUEUE = {
    420: "ranked_solo_5x5",
    440: "ranked_flex_sr",
    400: "normal_draft_5x5",
    430: "normal_blind_5x5",
    480: UGG_SWIFTPLAY,
}


#: What each tab is called, in one place. The picker reads the label from the
#: mode rather than holding its own copy, so a tab cannot be renamed in one
#: half of the app and not the other.
TAB_LABELS = {
    "skin": "Skin",
    "build": "Build",
    "counters": "Counters",
    "tiers": "Tier list",
    "augments": "Augments",
    "items": "Items",
}


@dataclass(frozen=True)
class Mode:
    """One game mode, and what this tool can offer in it."""

    key: str
    label: str
    #: Whether a skin can be applied. True everywhere except Teamfight
    #: Tactics, which is a different game with no champion models to swap.
    skins: bool = True
    #: Whether the mode assigns lanes. ARAM and Arena do not, so a build is
    #: read from the one pooled cell rather than a role.
    roles: bool = False
    #: Where build data comes from, if anywhere.
    source: Optional[str] = None
    #: The u.gg queue path, when the source is u.gg.
    queue: Optional[str] = None
    #: True when the numbers are borrowed from another queue because this one
    #: has none. Shown rather than hidden.
    borrowed: bool = False

    @property
    def tabs(self) -> list:
        """The tabs this mode earns, derived from what it actually has.

        Never a fixed list. A tab that can only ever render an empty state
        teaches that tabs are allowed to be dead, so each one has to be paid
        for by data that exists:

        * **Skin** is the app, and is always there.
        * **Build** needs a source. Arena spends its source differently --
          augments are picked three times a match and decide more than items
          do, so they get their own page rather than sharing one.
        * **Counters** needs lanes *and* u.gg. Arena puts all eighteen
          players in ``myTeam``, so there is nobody to counter.
        """
        keys = ["skin"]
        if self.source == "opgg":
            # Arena: a champion tier list to pick with, the augment table to
            # play with, and items in their own right rather than as a footer.
            keys += ["tiers", "augments", "items"]
        elif self.source:
            keys.append("build")
            if self.roles:
                keys.append("counters")
        return [{"key": k, "label": TAB_LABELS[k]} for k in keys]


TFT = Mode("tft", "Teamfight Tactics", skins=False)
UNKNOWN = Mode("unknown", "this mode")


def resolve(game_mode: Optional[str], map_id: Optional[int],
            queue_id: Optional[int] = None) -> Mode:
    """The mode for a client queue, by mode string and map."""
    mode = (game_mode or "").upper()

    if mode == "TFT" or map_id == 22:
        return TFT

    if mode == "CHERRY" or map_id == 30:
        # u.gg publishes nothing for Arena: the URLs its own Arena pages
        # reference return AccessDenied, and no augment endpoint appears in
        # its manifest at all. op.gg serves the same figures as plain JSON
        # and permits automated access.
        return Mode("arena", "Arena", source="opgg")

    if mode == "ARAM":
        return Mode("aram", "ARAM", source="ugg", queue=UGG_ARAM)

    if mode == "KIWI":
        # Mayhem is ARAM with modifiers and has no data of its own, so it
        # borrows ARAM's -- the champions and the map are the same.
        return Mode("mayhem", "ARAM: Mayhem", source="ugg", queue=UGG_ARAM,
                    borrowed=True)

    if mode == "NEXUSBLITZ" or map_id == 21:
        return Mode("nexusblitz", "Nexus Blitz")

    if mode == "SWIFTPLAY":
        return Mode("swiftplay", "Swiftplay", roles=True, source="ugg",
                    queue=UGG_SWIFTPLAY)

    if mode == "URF":
        return Mode("urf", "URF", roles=True, source="ugg", queue=UGG_RANKED,
                    borrowed=True)

    if mode in ("CLASSIC", "PRACTICETOOL", "JADE") or map_id in (11, 453):
        queue = UGG_BY_QUEUE.get(queue_id or -1)
        return Mode("rift", "Summoner's Rift", roles=True, source="ugg",
                    queue=queue or UGG_RANKED, borrowed=queue is None)

    return UNKNOWN


def payload(mode: Mode, queue_id=None, game_mode: str = "",
            map_id=None, description: str = "") -> dict:
    """The queue block the picker reads, from a resolved mode.

    Built here rather than at each call site: the real client and the mock
    both produce this, and a tab set that existed in one and not the other
    would be a mode that behaves differently under test than in a game.
    """
    return {
        "id": queue_id,
        "mode": (game_mode or "").upper(),
        "mapId": map_id,
        "description": description or "",
        "kind": mode.key,
        "label": mode.label,
        "skins": mode.skins,
        "roles": mode.roles,
        "source": mode.source,
        "uggQueue": mode.queue,
        "borrowed": mode.borrowed,
        "tabs": mode.tabs,
    }
