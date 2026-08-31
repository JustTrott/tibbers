#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A scriptable stand-in for the League client, for working on the interface.

Every state the picker can be in is reachable only by actually playing: you
cannot see the champ-select empty state without entering champ select, or the
patcher-error state without a failure. That makes the rarely-seen states the
least designed ones, which is backwards.

This drives the same `State` object the real watcher writes to, so the UI is
exercised through its real code path rather than a parallel fake. Art still
comes from the live client when one is running; with the disk cache, a champion
seen once keeps working after the client closes.

Enabled only by `--mock`. Nothing here runs otherwise.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("tibbers.mock")

#: The gameflow phases the picker distinguishes.
PHASES = ("None", "Lobby", "Matchmaking", "ReadyCheck", "ChampSelect",
          "InProgress", "Reconnect", "WaitingForStats")

#: Patcher stages, matching cslol's own status strings.
PATCHER_STAGES = {
    "idle":     {},
    "watching": {"watching": True, "found": False, "patched": False, "error": None},
    "found":    {"watching": True, "found": True,  "patched": False, "error": None},
    "patching": {"watching": True, "found": True,  "patched": True,  "error": None},
    "error":    {"watching": True, "found": True,  "patched": False,
                 "error": "Failed to find fopen stub"},
}


class MockClient:
    """Applies scripted transitions to the shared UI state."""

    def __init__(self, state, load_champion: Callable[[int], Optional[dict]],
                 on_applied: Optional[Callable[[], None]] = None,
                 describe_champion: Optional[Callable[[int], Optional[dict]]] = None):
        self.state = state
        self.load_champion = load_champion
        # Skins can only come from the running client, but the build, counter
        # and Arena pages need nothing but a champion id and a name -- and
        # those are in the local game data, which outlives the client. Without
        # this the mock could only replay champ select while League was
        # already running, which is the one situation it exists to avoid.
        self.describe_champion = describe_champion
        # Called after every action so the app reacts exactly as it would to a
        # real client. Without it the mock writes state that nothing observes,
        # and the behaviour under test (auto-show, remembered picks) never runs.
        self.on_applied = on_applied
        self._lock = threading.Lock()
        self._champion: Optional[int] = None

    # -- helpers ----------------------------------------------------------

    def _clear_champion(self) -> None:
        with self.state.lock:
            self.state.champion_id = None
            self.state.champion_name = None
            self.state.champion_title = ""
            self.state.champion_icon = ""
            self.state.skins = []
            self.state.selected_skin_id = None
            self.state.selected_chroma_id = None
            self.state.locked = False
            self.state.enemies = []
            self.state.opponent_id = None
            self.state.bans = {}
            self.state.guide = {}
        self._champion = None

    # -- actions ----------------------------------------------------------

    def apply(self, action: str, value=None) -> dict:
        """Run one action. Returns a short result for the caller to echo."""
        with self._lock:
            handler = getattr(self, f"_do_{action}", None)
            if handler is None:
                return {"ok": False, "error": f"unknown action: {action}"}
            try:
                message = handler(value)
            except Exception as exc:  # noqa: BLE001
                log.exception("mock action failed")
                return {"ok": False, "error": str(exc)}
            self.state.say(f"[mock] {message}")

        if self.on_applied is not None:
            try:
                self.on_applied()
            except Exception:  # noqa: BLE001
                log.exception("mock listener failed")
        return {"ok": True, "message": message}

    def _do_elevation_ask(self, value) -> str:
        """Show or hide the one-time passwordless card, for looking at it.

        Injection is off in a dev instance, so the real first-apply path never
        raises this card; this is the only way to see it rendered.
        """
        on = value not in (0, "0", "off", False, None)
        with self.state.lock:
            self.state.ask_elevation = on
        return f"elevation card {'shown' if on else 'hidden'}"

    def _do_launch_client(self, _value) -> str:
        with self.state.lock:
            self.state.connected = True
            self.state.phase = "None"
        self._clear_champion()
        return "client launched"

    def _do_close_client(self, _value) -> str:
        with self.state.lock:
            self.state.connected = False
            self.state.phase = None
            self.state.patcher = {}
            self.state.download = {}
        self._clear_champion()
        return "client closed"

    def _do_phase(self, value) -> str:
        phase = str(value or "None")
        if phase not in PHASES:
            raise ValueError(f"unknown phase: {phase}")
        with self.state.lock:
            self.state.connected = True
            self.state.phase = phase
        # A fresh champ select starts with nothing hovered and nothing locked.
        # Leaving the previous lock in place made the picker look like it
        # popped up on entering champ select rather than on lock-in.
        if phase in ("None", "Lobby", "ChampSelect"):
            self._clear_champion()
        return f"phase {phase}"

    def _do_hover(self, value) -> str:
        champion_id = int(value)
        data = self.load_champion(champion_id)
        if data is None and self.describe_champion is not None:
            # No client. The skin grid cannot be filled, but every data page
            # can, so the champion is hovered with an empty skin list rather
            # than the whole action failing.
            known = self.describe_champion(champion_id)
            if known:
                data = {"name": known.get("name") or f"Champion {champion_id}",
                        "title": known.get("title") or "",
                        "icon": known.get("icon") or "", "skins": []}
        if data is None:
            raise ValueError(f"no data for champion {champion_id} "
                             f"(no client, and nothing cached for it)")
        with self.state.lock:
            self.state.connected = True
            self.state.phase = "ChampSelect"
            self.state.champion_id = champion_id
            self.state.champion_name = data["name"]
            self.state.champion_title = data["title"]
            self.state.champion_icon = data["icon"]
            self.state.skins = data["skins"]
            self.state.locked = False
            self.state.selected_skin_id = None
            self.state.selected_chroma_id = None
        self._champion = champion_id
        return f"hovering {data['name']}"

    def _do_lock(self, _value) -> str:
        if self._champion is None:
            raise ValueError("hover a champion first")
        with self.state.lock:
            self.state.locked = True
        return "locked in"

    def _do_start_game(self, _value) -> str:
        with self.state.lock:
            self.state.phase = "InProgress"
        return "game started"

    def _do_end_game(self, _value) -> str:
        with self.state.lock:
            self.state.phase = "Lobby"
            self.state.patcher = {}
        self._clear_champion()
        return "game ended"

    def _do_download(self, value) -> str:
        """value: 'idle' | 'complete' | an int percentage."""
        if value in ("idle", None):
            with self.state.lock:
                self.state.download = {}
            return "download idle"
        if value == "complete":
            with self.state.lock:
                self.state.download = {"state": "complete", "fetched": 18,
                                       "missing": 2, "total": 20}
            return "download complete"
        pct = max(0, min(100, int(value)))
        total = 20
        done = round(total * pct / 100)
        with self.state.lock:
            self.state.download = {"state": "downloading", "fetched": done,
                                   "missing": 0, "total": total}
        return f"downloading {pct}%"

    def _do_patcher(self, value) -> str:
        stage = str(value or "idle")
        if stage not in PATCHER_STAGES:
            raise ValueError(f"unknown patcher stage: {stage}")
        with self.state.lock:
            self.state.patcher = dict(PATCHER_STAGES[stage])
        return f"patcher {stage}"

    # -- champ select context ---------------------------------------------
    #
    # Replays a real champ select captured from the client: enemies lock one
    # at a time, and the last of them arrived 49 seconds after the player had
    # already locked. That ordering is the whole reason the guide has to keep
    # updating while the picker is open, so the mock reproduces it rather than
    # handing over a finished lobby.
    CAPTURED_ENEMIES = [122, 18, 55, 238, 133]   # Darius Tristana Katarina Zed Quinn

    def _do_role(self, value) -> str:
        role = str(value or "").lower() or None
        with self.state.lock:
            self.state.role = role
        return f"role {role or 'unassigned'}"

    def _do_enemy(self, value) -> str:
        """Lock in one more enemy, or a specific champion id."""
        with self.state.lock:
            locked = [e for e in self.state.enemies if e.get("championId")]
            if value in (None, "", "next"):
                remaining = [c for c in self.CAPTURED_ENEMIES
                             if c not in {e["championId"] for e in locked}]
                if not remaining:
                    return "every enemy has locked"
                champion = remaining[0]
            else:
                champion = int(value)
            enemies = list(self.state.enemies)
            if not enemies:
                enemies = [{"cellId": 5 + i, "championId": None, "locked": False}
                           for i in range(5)]
            for slot in enemies:
                if not slot.get("championId"):
                    slot["championId"] = champion
                    slot["locked"] = True
                    break
            self.state.enemies = enemies
        return f"enemy locked {champion}"

    def _do_bans(self, _value) -> str:
        with self.state.lock:
            self.state.bans = {"mine": [8, 555, 12, 267, 58],
                               "theirs": [6, 157, 164, 236, 51]}
        return "bans in"

    #: Real queues, taken from the client's own list, so every mode the app
    #: claims to handle can actually be exercised without queueing for one.
    QUEUES = {
        "rift":      (420, "CLASSIC", 11, "Ranked Solo/Duo"),
        "draft":     (400, "CLASSIC", 11, "Draft Pick"),
        "swiftplay": (480, "SWIFTPLAY", 11, "Swiftplay"),
        "urf":       (1900, "URF", 11, "Ultra Rapid Fire"),
        "aram":      (450, "ARAM", 12, "ARAM"),
        "mayhem":    (3270, "KIWI", 12, "ARAM: Mayhem"),
        "arena":     (1750, "CHERRY", 30, "Arena 3x6"),
        "nexus":     (1300, "NEXUSBLITZ", 21, "Nexus Blitz"),
    }

    def _do_queue(self, value) -> str:
        """Any of the real modes, by name -- see QUEUES."""
        from . import modes

        name = str(value or "rift").lower()
        if name not in self.QUEUES:
            raise ValueError(f"unknown queue: {name} "
                             f"(try {', '.join(self.QUEUES)})")
        qid, mode, map_id, label = self.QUEUES[name]
        resolved = modes.resolve(mode, map_id, qid)
        with self.state.lock:
            self.state.queue = modes.payload(resolved, qid, mode, map_id, label)
        return f"queue {name}"

    def _do_availability(self, value) -> str:
        """Force how many skins have mods: 'all', 'some', or 'none'."""
        mode = str(value or "all")
        with self.state.lock:
            for i, skin in enumerate(self.state.skins):
                has = {"all": True, "none": False}.get(mode, i % 3 != 0)
                skin["available"] = has
                for chroma in skin.get("chromas") or []:
                    chroma["available"] = has
        return f"availability {mode}"

    def _do_script(self, _value) -> str:
        """Walk the whole happy path, with pauses, in a background thread."""
        def run():
            steps = [
                ("launch_client", None, 0.8),
                ("phase", "Lobby", 0.8),
                ("phase", "ChampSelect", 0.6),
                ("hover", self._champion or 202, 0.4),
                ("download", 30, 0.5),
                ("download", 70, 0.5),
                ("download", "complete", 0.6),
                ("lock", None, 0.8),
                ("patcher", "watching", 1.2),
                ("start_game", None, 0.6),
                ("patcher", "found", 0.8),
                ("patcher", "patching", 0),
            ]
            for action, value, pause in steps:
                self.apply(action, value)
                time.sleep(pause)

        threading.Thread(target=run, daemon=True).start()
        return "running the full sequence"
