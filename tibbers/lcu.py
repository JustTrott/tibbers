#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
League Client (LCU) API access.

The client runs a local REST/WebSocket server and writes its port and password
into a `lockfile` next to itself. Reading champ select from here is what lets
this tool know which champion you locked without modifying the client at all.
"""

from __future__ import annotations

import base64
import json
import ssl
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import system

# The LCU serves a self-signed certificate. Verification is disabled for this
# connection only -- it never leaves localhost, and the password from the
# lockfile is what actually authenticates us.
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


log = logging.getLogger("tibbers.lcu")


@dataclass(frozen=True)
class Lockfile:
    name: str
    pid: int
    port: int
    password: str
    protocol: str

    @classmethod
    def read(cls, path: Path) -> "Lockfile":
        parts = Path(path).read_text().strip().split(":")
        if len(parts) < 5:
            raise ValueError(f"malformed lockfile: {path}")
        return cls(parts[0], int(parts[1]), int(parts[2]), parts[3], parts[4])


class LCU:
    """Minimal LCU REST client."""

    def __init__(self, lockfile: Lockfile):
        self.lock = lockfile
        token = base64.b64encode(f"riot:{lockfile.password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._base = f"https://127.0.0.1:{lockfile.port}"

    @classmethod
    def connect(cls) -> Optional["LCU"]:
        path = system.lockfile_path()
        if path is None:
            return None
        try:
            return cls(Lockfile.read(path))
        except (OSError, ValueError):
            return None

    def get(self, endpoint: str, timeout: float = 5.0):
        """GET *endpoint*; returns parsed JSON, or None on any failure."""
        req = urllib.request.Request(
            self._base + endpoint,
            headers={"Authorization": self._auth, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def get_bytes(self, endpoint: str, timeout: float = 10.0) -> Optional[bytes]:
        """GET raw bytes -- used to proxy splash art to the browser."""
        req = urllib.request.Request(
            self._base + endpoint, headers={"Authorization": self._auth}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            return None

    def send(self, method: str, endpoint: str, body=None,
             timeout: float = 8.0) -> tuple:
        """Write to the client. Returns ``(status, parsed body)``.

        Every other call in this file swallows failure and returns None,
        because a missing read is answered by asking again a moment later. A
        write is not: the caller has to be able to say *why* nothing was
        written, so the status and the client's own error body both come back.
        A status of 0 means the request never reached the client at all.
        """
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self._base + endpoint, data=data, method=method.upper(),
            headers={"Authorization": self._auth, "Accept": "application/json",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except OSError:
                raw = b""
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return 0, {"message": str(exc)}
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace")

    # -- Things this tool actually asks for -------------------------------

    def phase(self) -> Optional[str]:
        """Gameflow phase: Lobby, ChampSelect, InProgress, ..."""
        return self.get("/lol-gameflow/v1/gameflow-phase")

    def champ_select(self) -> Optional[dict]:
        return self.get("/lol-champ-select/v1/session")

    def current_summoner(self) -> Optional[dict]:
        return self.get("/lol-summoner/v1/current-summoner")

    def champion_intent(self, session: Optional[dict] = None) -> tuple:
        """``(champion_id, locked)`` for your own cell during champ select.

        Returns the champion as soon as you *hover* it, not just once you lock
        in, so skins can be listed and fetched while you are still deciding.

        Three sources, in order of reliability:
          * ``myTeam[].championId``   -- non-zero only after lock-in
          * ``myTeam[].championPickIntent`` -- the hover
          * the uncompleted pick action in ``actions`` -- the hover, for modes
            where pick intent is not published
        """
        session = self.champ_select() if session is None else session
        if not session:
            return None, False

        cell_id = session.get("localPlayerCellId")

        locked = None
        intent = None
        for player in session.get("myTeam") or []:
            if player.get("cellId") == cell_id:
                locked = player.get("championId") or None
                intent = player.get("championPickIntent") or None
                break

        if locked:
            return locked, True

        if not intent:
            for group in session.get("actions") or []:
                for action in group or []:
                    if (action.get("actorCellId") == cell_id
                            and action.get("type") == "pick"
                            and not action.get("completed")):
                        intent = action.get("championId") or None
                        if intent:
                            break
                if intent:
                    break

        return (intent, False) if intent else (None, False)

    # -- the rest of champ select -----------------------------------------
    #
    # What the client will and will not tell you here shapes the whole build
    # guide. Riot deliberately hides two things to stop counter-picking:
    #
    #   * an enemy's hover. `championPickIntent` exists on their entries and
    #     is never filled in, so a locked pick is the earliest an opponent is
    #     ever visible.
    #   * every enemy's role. `assignedPosition` is only meaningful for your
    #     own team, so which opponent is in your lane cannot be worked out
    #     during champ select at all -- it is only exposed once the match
    #     starts, by a different API. Picking the lane opponent has to be a
    #     manual choice; that is Riot's design, not a shortcut.

    #: The client uses lowercase here and uppercase elsewhere for the same
    #: idea, so every comparison in this file is case-insensitive.
    POSITIONS = ("top", "jungle", "middle", "bottom", "utility")

    def _my_cell(self, session: dict) -> Optional[dict]:
        cell_id = session.get("localPlayerCellId")
        for player in session.get("myTeam") or []:
            if player.get("cellId") == cell_id:
                return player
        return None

    def my_position(self, session: Optional[dict] = None) -> Optional[str]:
        """Your assigned lane, or None in queues that do not assign one."""
        session = self.champ_select() if session is None else session
        if not session:
            return None
        mine = self._my_cell(session) or {}
        position = (mine.get("assignedPosition") or "").strip().lower()
        return position if position in self.POSITIONS else None

    def enemy_team(self, session: Optional[dict] = None) -> list:
        """Enemy champions, in pick order, as far as they have locked in.

        `locked` is the only honest field: an enemy who has not picked shows
        as championId 0, and there is no hover to fall back on.
        """
        session = self.champ_select() if session is None else session
        if not session:
            return []
        out = []
        for player in session.get("theirTeam") or []:
            champion = player.get("championId") or 0
            out.append({
                "cellId": player.get("cellId"),
                "championId": champion or None,
                "locked": bool(champion),
            })
        return out

    def bans(self, session: Optional[dict] = None) -> dict:
        """Both teams' bans, falling back to the action list.

        Some formats leave the summary block empty and only record bans as
        completed actions, so the actions are read when it is.
        """
        session = self.champ_select() if session is None else session
        if not session:
            return {"mine": [], "theirs": []}

        block = session.get("bans") or {}
        mine = [c for c in (block.get("myTeamBans") or []) if c]
        theirs = [c for c in (block.get("theirTeamBans") or []) if c]
        if mine or theirs:
            return {"mine": mine, "theirs": theirs}

        my_cells = {p.get("cellId") for p in (session.get("myTeam") or [])}
        for group in session.get("actions") or []:
            for action in group or []:
                if action.get("type") != "ban" or not action.get("completed"):
                    continue
                champion = action.get("championId") or 0
                if not champion:
                    continue
                side = mine if action.get("actorCellId") in my_cells else theirs
                side.append(champion)
        return {"mine": mine, "theirs": theirs}

    def queue(self) -> dict:
        """Queue, map and what this tool can offer there.

        Read live rather than assumed: the mode strings are not what you
        would guess -- ARAM Mayhem calls itself KIWI, Swiftplay is its own
        mode on Summoner's Rift, and Arena is CHERRY on map 30.
        """
        from . import modes

        session = self.get("/lol-gameflow/v1/session") or {}
        data = (session.get("gameData") or {}).get("queue") or {}
        mode = (data.get("gameMode") or "").upper()
        map_id = data.get("mapId")
        resolved = modes.resolve(mode, map_id, data.get("id"))
        return modes.payload(resolved, data.get("id"), mode, map_id,
                             data.get("description") or "")

    def champ_select_state(self) -> dict:
        """Everything champ select can tell us, from a single fetch.

        The watcher runs several times a second and every reader here starts
        from the same session, so they share one request rather than each
        making their own.
        """
        session = self.champ_select()
        if not session:
            return {"championId": None, "locked": False, "role": None,
                    "enemies": [], "bans": {"mine": [], "theirs": []}}
        champion, locked = self.champion_intent(session)
        return {
            "championId": champion,
            "locked": locked,
            "role": self.my_position(session),
            "enemies": self.enemy_team(session),
            "bans": self.bans(session),
        }

    def champion_skins(self, champion_id: int) -> list:
        """All skins for a champion, owned or not.

        `/lol-game-data/assets` is the client's own static data, so this lists
        every skin in the game rather than just the ones on the account.
        """
        data = self.get(f"/lol-game-data/assets/v1/champions/{champion_id}.json")
        if not data:
            return []

        skins = []
        for skin in data.get("skins") or []:
            # The client's own tier label, carried through as it comes.
            # Nothing here acts on it -- every mod is built the same way --
            # but it is what the client calls the skin, and the picker has it
            # if it ever wants to say so.
            rarity = skin.get("rarity") or ""
            skins.append({
                "id": skin.get("id"),
                "name": skin.get("name"),
                "isBase": skin.get("isBase", False),
                "owned": (skin.get("ownership") or {}).get("owned", False),
                "rarity": rarity,
                "splash": skin.get("splashPath") or "",
                "tile": skin.get("tilePath") or "",
                # A skin that has chromas also ships its own chroma-style
                # icon: the same Rift-background treatment, for the base
                # variant. That is what lets the base swatch match the
                # chromas beside it instead of being a splash crop.
                "icon": skin.get("chromaPath") or "",
                # Skins without chromas ship no model render at all. The
                # load-screen card is the closest the client has to how the
                # skin looks once you are in, so the preview falls back to it.
                "loadScreen": skin.get("loadScreenPath") or "",
                # The client renders chromas as colour discs over the
                # chroma's own in-game icon; both come straight from here.
                "chromas": [
                    {
                        "id": c.get("id"),
                        "name": c.get("name"),
                        "colors": c.get("colors") or [],
                        "icon": c.get("chromaPath") or "",
                        # A chroma carries no rarity of its own; it is a
                        # recolour of its skin and is authored the same way.
                        "rarity": rarity,
                    }
                    for c in (skin.get("chromas") or [])
                ],
            })
        return skins

    def champion_name(self, champion_id: int) -> str:
        return self.champion_info(champion_id)["name"]

    def champion_info(self, champion_id: int) -> dict:
        """Name, title and square icon, for the picker header."""
        data = self.get(
            f"/lol-game-data/assets/v1/champions/{champion_id}.json") or {}
        return {
            "name": data.get("name", f"Champion {champion_id}"),
            # The internal name -- MonkeyKing for Wukong -- which is what the
            # game's own files are called and what `skinsmith` needs to find
            # the champion's archive.
            "alias": data.get("alias") or "",
            "title": data.get("title", ""),
            "icon": data.get("squarePortraitPath")
                    or f"/lol-game-data/assets/v1/champion-icons/{champion_id}.png",
        }


class PhaseWatcher(threading.Thread):
    """Polls the gameflow phase and champion lock, reporting changes.

    Polling rather than the LCU event socket: the two facts this needs are
    cheap to read, and polling avoids a websocket dependency and its
    reconnection handling for a tool this small.
    """

    def __init__(self, on_change: Callable[[dict], None], interval: float = 0.4):
        super().__init__(daemon=True)
        self.on_change = on_change
        self.interval = interval
        self._stop = threading.Event()
        self._last = None
        self.lcu: Optional[LCU] = None

    def stop(self) -> None:
        self._stop.set()

    def resync(self) -> None:
        """Report the current state again on the next poll.

        Changes are only reported when they happen, so anything that comes up
        after a change has passed -- the window shell, most obviously -- never
        hears about the state it missed.
        """
        self._last = None

    def snapshot(self) -> dict:
        if self.lcu is None:
            self.lcu = LCU.connect()
        if self.lcu is None:
            return {"connected": False, "phase": None,
                    "championId": None, "locked": False}

        phase = self.lcu.phase()
        if phase is None:
            # Client went away; force a reconnect next tick.
            self.lcu = None
            return {"connected": False, "phase": None,
                    "championId": None, "locked": False}

        if phase != "ChampSelect":
            return {"connected": True, "phase": phase, "championId": None,
                    "locked": False, "role": None, "enemies": [],
                    "bans": {"mine": [], "theirs": []}}

        select = self.lcu.champ_select_state()
        return {"connected": True, "phase": phase,
                "championId": select["championId"], "locked": select["locked"],
                "role": select["role"], "enemies": select["enemies"],
                "bans": select["bans"]}

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                state = self.snapshot()
                # Enemies are part of the key because they keep locking in
                # after you do -- the last one can be nearly a minute later,
                # while the picker is already open -- and each arrival
                # changes what the guide should show.
                key = (state["connected"], state["phase"],
                       state["championId"], state["locked"],
                       tuple(e.get("championId") for e in state.get("enemies") or []))
                if key != self._last:
                    self._last = key
                    self.on_change(state)
            except Exception:
                # Logged rather than swallowed. A handler that throws here
                # stops every downstream reaction -- the picker never opens,
                # no skin is queued -- and silence made that indistinguishable
                # from a client that simply had nothing to report.
                log.exception("champ select watcher failed")
            self._stop.wait(self.interval)
