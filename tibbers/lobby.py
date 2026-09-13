#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Party skin sharing: the room a League party shares, and who in it picked what.

Everyone in a party who runs tibbers joins one room on the relay (`worker/`)
and publishes a row of ids -- champion, skin, chroma -- as they choose. Each
install builds the others' skins from its own game files, so no skin ever
travels. LOBBY.md is the design; this module is the client half of it.

The room is the party. Its id is a hash of the party id the League client
gives every member alike, and a member's id is a hash of the room and that
member's puuid, so each install works out every other member's id from the
roster it already has and nothing on the wire names anyone.

Nothing here waits on the network. `Lobby` keeps a snapshot of the room that
is always ready to read. A relay that cannot be reached marks the snapshot
with the error and leaves it holding whatever it last heard, which before
anyone has picked is exactly today's behaviour: your own skin only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from . import __version__

log = logging.getLogger("tibbers.lobby")

#: Where rooms live. TIBBERS_LOBBY_URL points a dev instance at `wrangler dev`.
DEFAULT_RELAY = "wss://lobby.tibbers.lol"

#: Part of the room hash. Every install in a party has to agree on it, so it
#: changes only with the protocol, and `/v1/` in the relay's path with it.
ROOM_SALT = "tibbers-party-1"

#: How often the party is read when nothing else wakes the lobby. Someone
#: joining or leaving the party changes nothing the phase watcher reports.
PARTY_POLL = 2.0

#: Phases after champ select in which the game is really starting, which is
#: when a pick held back from the other team can go out.
GAME_PHASES = ("GameStart", "InProgress", "Reconnect")

EMPTY: Dict[str, Optional[int]] = {"c": None, "s": None, "k": None}

# When your own row goes out during champ select. Ordered, so that the
# strictest seen in one champ select is simply the largest.
TEAM = 0       # everyone in the room is on your team: on every change
ON_LOCK = 1    # a custom draft with party members opposite: once locked
AT_END = 2     # a custom blind with party members opposite: after champ select

# The relay's refusals (worker/src/index.ts).
BAD_FRAME = 4400
REPLACED = 4409
FULL = 4429


def room_id(party_id: str) -> str:
    """The room a party shares. Every member derives the same one."""
    return hashlib.sha256((ROOM_SALT + party_id).encode()).hexdigest()[:32]


def member_id(room: str, puuid: str) -> str:
    """A member's id in one room.

    Per room, so the same person in two parties is two unrelated ids.
    """
    return hashlib.sha256((room + puuid).encode()).hexdigest()[:16]


def row(champion_id, skin_id=None, chroma_id=None) -> dict:
    """A row the relay will accept.

    A skin or chroma id carries its champion as ``id // 1000``, and the relay
    drops the connection over a row that does not hang together, so any part
    that does not belong to the champion is left out here instead of sent.
    Rows heard from the relay go through this too before anything reads them.
    """
    c = _id(champion_id)
    s = _id(skin_id) if c else None
    if s is not None and s // 1000 != c:
        s = None
    k = _id(chroma_id) if s else None
    if k is not None and k // 1000 != c:
        k = None
    return {"c": c, "s": s, "k": k}


def _id(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


# -- what the League client says ----------------------------------------------


@dataclass(frozen=True)
class Party:
    """The party as this client sees it; `players` are puuids in party order."""
    party_id: str
    me: str
    players: Tuple[str, ...]


def read_party(lcu) -> Optional[Party]:
    """The current party, or None when the client did not answer.

    `/lol-lobby/v1/parties/player` answers at every phase, in a lobby or not,
    which is what lets a room be joined whenever tibbers happens to start. A
    player is always in a party of at least themselves.
    """
    body = lcu.get("/lol-lobby/v1/parties/player")
    if not isinstance(body, dict):
        return None
    party = body.get("currentParty") or {}
    players = tuple(p["puuid"] for p in party.get("players") or []
                    if isinstance(p, dict) and p.get("puuid"))
    return Party(party.get("partyId") or "", body.get("puuid") or "", players)


def read_names(lcu) -> Optional[Dict[str, str]]:
    """Riot ids by puuid, or None when the client did not answer.

    Lobby participants carry empty names in practice. The comms roster is
    where they are filled in.
    """
    body = lcu.get("/lol-lobby/v2/comms/members")
    if not isinstance(body, dict):
        return None
    names = {}
    for player in (body.get("players") or {}).values():
        if isinstance(player, dict) and player.get("puuid") and player.get("gameName"):
            names[player["puuid"]] = player["gameName"]
    return names


def policy(session: dict, party: Party) -> int:
    """When this champ select lets your pick go out.

    A matchmade queue puts the whole party on one team, which already watches
    you hover. Only a custom game can put party members on the other side. A
    custom draft shows locked picks to both teams anyway, but a blind pick
    hides them until loading, and a skin id names its champion.

    Every doubt is read the cautious way. A cell on the other team with no
    puuid cannot be told apart from a party member, so it counts as one, and
    a flag the client left out counts as a custom game and a blind pick.
    """
    if session.get("isCustomGame") is False:
        return TEAM
    members = set(party.players)
    if not any(not p.get("puuid") or p["puuid"] in members
               for p in session.get("theirTeam") or []):
        return TEAM
    return ON_LOCK if session.get("hasSimultaneousPicks") is False else AT_END


def _locked(session: dict) -> bool:
    cell = session.get("localPlayerCellId")
    return any(p.get("cellId") == cell and p.get("championId")
               for p in session.get("myTeam") or [])


def _champions(session: dict) -> Dict[str, int]:
    """Champions by puuid, for the members who have no row to say so."""
    out = {}
    for p in (session.get("myTeam") or []) + (session.get("theirTeam") or []):
        champion = p.get("championId") or p.get("championPickIntent")
        if p.get("puuid") and champion:
            out[p["puuid"]] = champion
    return out


# -- transports ---------------------------------------------------------------


class Transport:
    """How rows reach the relay, one room at a time.

    `connect` joins a room as a member, and from then on the transport keeps
    it joined -- reconnecting, and sending the last row it was given again --
    until `close`. Each state of the room arrives through `on_state` as
    ``{member id: row}``. Trouble arrives through `on_error`: a message while
    the room cannot be reached, then None once it can.
    """

    def connect(self, room: str, member: str,
                on_state: Callable[[Dict[str, dict]], None],
                on_error: Callable[[Optional[str]], None]) -> None:
        raise NotImplementedError

    def send(self, row: dict) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class MemoryRelay:
    """The relay over a dict, delivering on the caller's thread.

    Several `MemoryTransport`s on one relay are several machines in one
    party, which is how the tests and the mock run a room with no network.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.rooms: Dict[str, Dict[str, "MemoryTransport"]] = {}
        self._waiting: list = []
        self.reachable = True

    def rows(self, room: str) -> Dict[str, dict]:
        with self._lock:
            return {m: dict(t.row) for m, t in self.rooms.get(room, {}).items()}

    def enter(self, transport: "MemoryTransport") -> bool:
        with self._lock:
            if not self.reachable:
                self._waiting.append(transport)
                return False
            self.rooms.setdefault(transport.room, {})[transport.member] = transport
            self._broadcast(transport.room)
            return True

    def changed(self, transport: "MemoryTransport") -> None:
        with self._lock:
            if self.rooms.get(transport.room, {}).get(transport.member) is transport:
                self._broadcast(transport.room)

    def leave(self, transport: "MemoryTransport") -> None:
        with self._lock:
            if transport in self._waiting:
                self._waiting.remove(transport)
            members = self.rooms.get(transport.room) or {}
            if members.get(transport.member) is not transport:
                return
            del members[transport.member]
            if members:
                self._broadcast(transport.room)
            else:
                del self.rooms[transport.room]

    def drop(self) -> None:
        """Every connection fails, as it would with the relay gone."""
        with self._lock:
            self.reachable = False
            dropped = [t for members in self.rooms.values() for t in members.values()]
            self.rooms.clear()
            self._waiting.extend(dropped)
        for transport in dropped:
            transport.on_error("the relay cannot be reached")

    def restore(self) -> None:
        """The relay is back, and whoever was cut off rejoins with their row."""
        with self._lock:
            self.reachable = True
            waiting, self._waiting = self._waiting, []
        for transport in waiting:
            if transport.room is not None and self.enter(transport):
                transport.on_error(None)

    def _broadcast(self, room: str) -> None:
        members = self.rooms.get(room) or {}
        state = {m: dict(t.row) for m, t in members.items()}
        for transport in list(members.values()):
            transport.on_state(dict(state))


class MemoryTransport(Transport):

    def __init__(self, relay: MemoryRelay):
        self.relay = relay
        self.room: Optional[str] = None
        self.member: Optional[str] = None
        self.row = dict(EMPTY)
        self.on_state: Callable = lambda rows: None
        self.on_error: Callable = lambda message: None

    def connect(self, room, member, on_state, on_error):
        self.close()
        self.room, self.member, self.row = room, member, dict(EMPTY)
        self.on_state, self.on_error = on_state, on_error
        if not self.relay.enter(self):
            on_error("the relay cannot be reached")

    def send(self, row):
        self.row = dict(row)
        if self.room is not None:
            self.relay.changed(self)

    def close(self):
        if self.room is not None:
            self.relay.leave(self)
        self.room = self.member = None


class WsTransport(Transport):
    """The relay over a WebSocket, with a thread of its own for each room."""

    def __init__(self, url: Optional[str] = None):
        self.url = (url or os.environ.get("TIBBERS_LOBBY_URL")
                    or DEFAULT_RELAY).rstrip("/")
        self._connection: Optional[_Connection] = None

    def connect(self, room, member, on_state, on_error):
        self.close()
        self._connection = _Connection(f"{self.url}/v1/room?room={room}",
                                       member, on_state, on_error)
        self._connection.start()

    def send(self, row):
        if self._connection is not None:
            self._connection.send(row)

    def close(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.stop()


class _Connection(threading.Thread):
    """One room's socket, reopened with backoff until it is stopped."""

    #: Protocol pings, which the relay's runtime answers without waking the
    #: room. They are what notices a connection that died without a close.
    PING_INTERVAL = 25
    PING_TIMEOUT = 10
    MAX_BACKOFF = 30.0

    def __init__(self, url, member, on_state, on_error):
        super().__init__(daemon=True, name="tibbers-lobby-socket")
        self.url, self.member = url, member
        self.on_state, self.on_error = on_state, on_error
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._app = None
        self._open = False
        self._opened = False
        self._row: Optional[dict] = None
        self._code: Optional[int] = None
        self._failure: Optional[str] = None

    def send(self, row: dict) -> None:
        with self._lock:
            self._row = dict(row)
            app = self._app if self._open else None
        if app is not None:
            self._send(app, {"t": "pick", **row})

    def stop(self) -> None:
        self._halt.set()
        with self._lock:
            app = self._app
        if app is not None:
            app.close()

    def run(self) -> None:
        import websocket  # websocket-client

        delay = 1.0
        while not self._halt.is_set():
            app = websocket.WebSocketApp(
                self.url, header={"User-Agent": f"tibbers/{__version__}"},
                on_open=self._on_open, on_message=self._on_message,
                on_error=self._on_error, on_close=self._on_close)
            with self._lock:
                self._app, self._open, self._opened = app, False, False
                self._code = self._failure = None
            if self._halt.is_set():
                break
            try:
                app.run_forever(ping_interval=self.PING_INTERVAL,
                                ping_timeout=self.PING_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 -- reported, then retried
                self._failure = str(exc)
            with self._lock:
                self._app, self._open = None, False
                opened, code = self._opened, self._code
            if self._halt.is_set():
                break
            if code == REPLACED:
                # Another connection joined as this member, which is another
                # copy of tibbers signed in as you. Reconnecting would only
                # push it out in turn, back and forth for good.
                self._report("another copy of tibbers joined this party as you")
                break
            if opened:
                delay = 1.0
            elif code in (BAD_FRAME, FULL):
                delay = self.MAX_BACKOFF
            self._report(self._failure
                         or (f"the relay closed the connection ({code})" if code
                             else "the relay cannot be reached"))
            self._halt.wait(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, self.MAX_BACKOFF)

    def _on_open(self, app) -> None:
        if self._halt.is_set():
            app.close()
            return
        # The join goes first and the socket only counts as open after it: a
        # pick that reached the relay ahead of its join would be refused.
        self._send(app, {"t": "join", "m": self.member})
        with self._lock:
            self._open = self._opened = True
            row = self._row
        if row is not None:
            self._send(app, {"t": "pick", **row})
        self._report(None)

    def _on_message(self, _app, text) -> None:
        try:
            frame = json.loads(text)
        except (TypeError, ValueError):
            return
        if (isinstance(frame, dict) and frame.get("t") == "state"
                and isinstance(frame.get("m"), dict)):
            try:
                self.on_state(frame["m"])
            except Exception:  # noqa: BLE001
                log.exception("lobby state listener failed")

    def _on_error(self, _app, exc) -> None:
        self._failure = str(exc) or type(exc).__name__

    def _on_close(self, _app, code, _reason) -> None:
        self._code = code

    def _report(self, message: Optional[str]) -> None:
        try:
            self.on_error(message)
        except Exception:  # noqa: BLE001
            log.exception("lobby error listener failed")

    @staticmethod
    def _send(app, frame: dict) -> None:
        try:
            app.send(json.dumps(frame, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001 -- the next open sends it again
            log.debug("lobby send failed: %s", exc)


# -- the lobby ----------------------------------------------------------------


class Lobby(threading.Thread):
    """Your party's room, kept joined while sharing is on.

    `get_lcu` is the phase watcher's client (None while League is closed),
    `enabled` reads the share_skins preference, and `hooked` is true once a
    patcher has hooked a running game. `publish` takes your own selection as
    it changes. `nudge` is for whenever the phase watcher reports something,
    so a phase change is acted on at once rather than at the next party poll.

    Every decision is made in `step`, on this thread; the transport's
    callbacks only hand over what they heard. The tests call `step` directly.
    """

    def __init__(self, transport: Transport, get_lcu: Callable,
                 on_change: Callable[[dict], None], enabled: Callable[[], bool],
                 hooked: Callable[[], bool] = lambda: False):
        super().__init__(daemon=True, name="tibbers-lobby")
        self.transport = transport
        self.get_lcu = get_lcu
        self.on_change = on_change
        self.enabled = enabled
        self.hooked = hooked
        self._wake = threading.Event()
        self._halt = threading.Event()

        # Written by other threads, always under the lock.
        self._lock = threading.Lock()
        self._mine = dict(EMPTY)
        self._room: Optional[str] = None
        self._rows: Dict[str, dict] = {}
        self._rows_room: Optional[str] = None
        self._error: Optional[str] = None
        self._snapshot = {"enabled": False, "inRoom": False, "error": None,
                          "members": []}

        # Only ever touched by `step`.
        self._enabled = False
        self._phase: Optional[str] = None
        self._party: Optional[Party] = None
        self._sent: Optional[dict] = None
        self._names: Dict[str, str] = {}
        self._named: Optional[Tuple[str, ...]] = None
        self._champions: Dict[str, int] = {}
        self._policy: Optional[int] = None
        self._locked = False
        self._done_for_game = False

    # -- called from anywhere -------------------------------------------------

    def publish(self, champion_id, skin_id=None, chroma_id=None) -> None:
        """Your own selection. When it goes out is `step`'s decision."""
        with self._lock:
            self._mine = row(champion_id, skin_id, chroma_id)
        self.nudge()

    def nudge(self) -> None:
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            snap = self._snapshot
            return {**snap, "members": [dict(m) for m in snap["members"]]}

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    def run(self) -> None:
        while not self._halt.is_set():
            self._wake.clear()
            try:
                self.step()
            except Exception:  # noqa: BLE001 -- logged; the next poll retries
                log.exception("lobby step failed")
            self._wake.wait(PARTY_POLL)
        self._leave()

    # -- the decisions --------------------------------------------------------

    def step(self) -> None:
        self._decide()
        self._emit()

    def _decide(self) -> None:
        self._enabled = bool(self.enabled())
        lcu = self.get_lcu() if self._enabled else None
        if lcu is None:
            self._party = None
            self._leave()
            return
        phase = lcu.phase()
        if phase is None:
            # A missed read. The watcher decides when the client has gone,
            # and `get_lcu` says so on the next pass.
            return
        entering = phase == "ChampSelect" and self._phase != "ChampSelect"
        self._phase = phase

        party = read_party(lcu)
        if party is not None:
            self._party = party
        party = self._party
        if (party is None or not party.party_id or not party.me
                or len(party.players) < 2):
            # A party of one opens no room at all.
            self._leave()
            return

        if phase in GAME_PHASES:
            if not self._done_for_game and self.hooked():
                # The game has read its files. Nothing a row says can change
                # what it loads now, so the socket goes and the room as last
                # heard stays on screen.
                self._leave(keep_rows=True)
                self._done_for_game = True
        else:
            self._done_for_game = False
        if self._done_for_game:
            return

        room = room_id(party.party_id)
        if room != self._room:
            self._leave()
            self._join(room, member_id(room, party.me))

        if party.players != self._named:
            names = read_names(lcu)
            if names is not None:
                self._names, self._named = names, party.players

        if entering or (phase != "ChampSelect" and phase not in GAME_PHASES):
            self._policy, self._locked, self._champions = None, False, {}
        if phase == "ChampSelect":
            session = lcu.champ_select()
            if session:
                seen = policy(session, party)
                # Only ever stricter within one champ select: a read that
                # missed the other team's puuids must not undo one that saw them.
                self._policy = seen if self._policy is None else max(self._policy, seen)
                self._locked = _locked(session)
                self._champions = _champions(session)

        with self._lock:
            mine = dict(self._mine)
        outgoing = self._outgoing(phase, mine)
        if outgoing != self._sent:
            self.transport.send(outgoing)
            self._sent = outgoing

    def _outgoing(self, phase: str, mine: dict) -> dict:
        if phase in GAME_PHASES:
            return mine
        if phase != "ChampSelect":
            return dict(EMPTY)
        if self._policy == TEAM or (self._policy == ON_LOCK and self._locked):
            return mine
        # Held back, including before the session has been read at all and
        # nobody yet knows who is on the other team.
        return dict(EMPTY)

    def _join(self, room: str, member: str) -> None:
        with self._lock:
            self._room, self._rows_room = room, room
            self._rows, self._error = {}, None
        self._sent = None
        self.transport.connect(
            room, member,
            on_state=lambda rows: self._heard(room, rows),
            on_error=lambda message: self._failed(room, message))

    def _leave(self, keep_rows: bool = False) -> None:
        with self._lock:
            room, self._room = self._room, None
            self._error = None
            if not keep_rows:
                self._rows, self._rows_room = {}, None
        self._sent = None
        if room is not None:
            self.transport.close()

    def _heard(self, room: str, rows: Dict[str, dict]) -> None:
        with self._lock:
            if room != self._room:
                return
            self._rows = {m: r for m, r in rows.items() if isinstance(r, dict)}
        self.nudge()

    def _failed(self, room: str, message: Optional[str]) -> None:
        with self._lock:
            if room != self._room:
                return
            self._error = message
        self.nudge()

    def _emit(self) -> None:
        snap = self._build()
        with self._lock:
            if snap == self._snapshot:
                return
            self._snapshot = snap
        try:
            self.on_change(snap)
        except Exception:  # noqa: BLE001
            log.exception("lobby listener failed")

    def _build(self) -> dict:
        with self._lock:
            room, rows_room = self._room, self._rows_room
            rows, error = dict(self._rows), self._error
        party = self._party
        members = []
        if self._enabled and party is not None and rows_room is not None:
            # A row is attributed by its member id alone, which only a member
            # of this party could have derived. Rows under any other id are
            # from nobody in the party and are never looked at.
            for puuid in party.players:
                heard = rows.get(member_id(rows_room, puuid))
                picked = (row(heard.get("c"), heard.get("s"), heard.get("k"))
                          if heard is not None else EMPTY)
                members.append({
                    "me": puuid == party.me,
                    "name": self._names.get(puuid, ""),
                    "tibbers": heard is not None,
                    "championId": picked["c"] or self._champions.get(puuid),
                    "skinId": picked["s"],
                    "chromaId": picked["k"],
                })
        return {"enabled": self._enabled, "inRoom": room is not None,
                "error": error, "members": members}
