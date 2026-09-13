#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Party skin sharing's client half.

Most of these are a party of machines -- a fake League client apiece and one
in-memory relay between them -- stepped by hand, so nothing depends on
timing. `AgainstRelay` runs the WebSocket transport against a real relay and
only when TIBBERS_LOBBY_TEST_URL names one; scripts/lobby_smoke.sh does that
against `wrangler dev`.
"""

from __future__ import annotations

import os
import queue
import secrets
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tibbers import lobby  # noqa: E402

PARTY = "3f2a1c9e-7b64-4d0e-9a51-2c8e6f0b7d13"
EMPTY = {"c": None, "s": None, "k": None}

try:
    import websocket  # noqa: F401
    HAVE_WEBSOCKET = True
except ImportError:
    HAVE_WEBSOCKET = False


class World:
    """One League party as every client in it sees it."""

    def __init__(self, *players, party_id=PARTY):
        self.party_id = party_id
        self.players = list(players)
        self.phase = "Lobby"
        self.sessions = {}
        self.relay = lobby.MemoryRelay()

    def room(self):
        return lobby.room_id(self.party_id)


class FakeClient:
    """Only the endpoints the lobby reads, answered from the world."""

    def __init__(self, world, me):
        self.world, self.me = world, me
        self.phase_answers = True

    def phase(self):
        return self.world.phase if self.phase_answers else None

    def champ_select(self):
        return self.world.sessions.get(self.me)

    def _roster(self):
        if self.me in self.world.players:
            return self.world.party_id, self.world.players
        return f"alone-{self.me}", [self.me]

    def get(self, endpoint):
        party_id, players = self._roster()
        if endpoint == "/lol-lobby/v1/parties/player":
            return {"puuid": self.me,
                    "currentParty": {"partyId": party_id,
                                     "players": [{"puuid": p} for p in players]}}
        if endpoint == "/lol-lobby/v2/comms/members":
            return {"partyId": party_id,
                    "players": {f"id-{p}": {"puuid": p, "gameName": p.title(),
                                            "tagLine": "EUW"} for p in players}}
        return None


class Machine:
    """One install of tibbers, in the world's party or out of it."""

    def __init__(self, world, me, transport=None):
        self.world, self.me = world, me
        self.client = FakeClient(world, me)
        self.enabled = True
        self.hooked = False
        self.client_open = True
        self.changes = []
        self.lobby = lobby.Lobby(
            transport or lobby.MemoryTransport(world.relay),
            get_lcu=lambda: self.client if self.client_open else None,
            on_change=self.changes.append,
            enabled=lambda: self.enabled,
            hooked=lambda: self.hooked)

    def view(self):
        return self.lobby.snapshot()

    def sees(self, puuid):
        """This machine's row for *puuid*, found by the name the client gives."""
        for member in self.view()["members"]:
            if member["name"] == puuid.title():
                return member
        return None

    def published(self):
        """What the relay holds for this machine."""
        mine = lobby.member_id(self.world.room(), self.me)
        return self.world.relay.rows(self.world.room()).get(mine)


def settle(*machines):
    # Delivery is synchronous, so two rounds carry anything one machine sends
    # into every other machine's snapshot.
    for _ in range(2):
        for machine in machines:
            machine.lobby.step()


def cell(cell_id, puuid, champion, locked, mine=True):
    return {"cellId": cell_id, "puuid": puuid,
            "championId": champion if locked else 0,
            "championPickIntent": champion if mine and not locked else 0}


def select(me, my_team, their_team=(), custom=False, blind=True):
    """Champ select as *me* sees it. Teams are (puuid, champion, locked)."""
    mine = [cell(i, *player) for i, player in enumerate(my_team)]
    theirs = [cell(5 + i, *player, mine=False) for i, player in enumerate(their_team)]
    local = next(c["cellId"] for c in mine if c["puuid"] == me)
    return {"localPlayerCellId": local, "myTeam": mine, "theirTeam": theirs,
            "isCustomGame": custom, "hasSimultaneousPicks": blind}


def eventually(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


class Ids(unittest.TestCase):

    def test_the_ids_are_the_ones_every_install_derives(self):
        # Pinned: installs of different versions have to agree on them, and
        # this construction is the one scripts/party_id.bat proved across
        # machines before 1.2.0 was planned.
        room = lobby.room_id(PARTY)
        self.assertEqual(room, "4d1a00ab17748a88ccdedce88709a5f5")
        self.assertEqual(lobby.member_id(room, "puuid-of-somebody"),
                         "7ab5051e0ba39ece")

    def test_a_member_is_somebody_else_in_another_room(self):
        other = lobby.room_id("another-party")
        self.assertNotEqual(lobby.member_id(lobby.room_id(PARTY), "ann"),
                            lobby.member_id(other, "ann"))


class Rows(unittest.TestCase):

    def test_a_skin_and_chroma_of_the_champion_are_kept(self):
        self.assertEqual(lobby.row(103, 103015, 103016),
                         {"c": 103, "s": 103015, "k": 103016})

    def test_a_skin_that_is_not_the_champions_is_left_out(self):
        self.assertEqual(lobby.row(103, 64012, 64013),
                         {"c": 103, "s": None, "k": None})

    def test_a_chroma_without_its_skin_is_left_out(self):
        self.assertEqual(lobby.row(103, None, 103016),
                         {"c": 103, "s": None, "k": None})

    def test_no_champion_is_an_empty_row(self):
        self.assertEqual(lobby.row(None, 103015, 103016), EMPTY)
        self.assertEqual(lobby.row(0, 103015), EMPTY)

    def test_only_positive_integers_are_ids(self):
        self.assertEqual(lobby.row(True, 1000), EMPTY)
        self.assertEqual(lobby.row("103", "103015"), EMPTY)
        self.assertEqual(lobby.row(-103), EMPTY)


class Joining(unittest.TestCase):

    def setUp(self):
        self.world = World("ann", "bea", "cat")
        self.ann = Machine(self.world, "ann")
        self.bea = Machine(self.world, "bea")

    def test_a_party_of_one_opens_no_room(self):
        world = World("ann")
        ann = Machine(world, "ann")
        ann.lobby.step()
        self.assertEqual(world.relay.rooms, {})
        self.assertFalse(ann.view()["inRoom"])
        self.assertEqual(ann.view()["members"], [])

    def test_sharing_switched_off_opens_no_room(self):
        self.ann.enabled = False
        self.ann.lobby.step()
        self.assertEqual(self.world.relay.rooms, {})
        self.assertFalse(self.ann.view()["enabled"])

    def test_the_party_lands_in_one_room_and_sees_who_runs_tibbers(self):
        settle(self.ann, self.bea)
        self.assertEqual(list(self.world.relay.rooms), [self.world.room()])
        members = self.ann.view()["members"]
        self.assertEqual([m["name"] for m in members], ["Ann", "Bea", "Cat"])
        self.assertEqual([m["me"] for m in members], [True, False, False])
        self.assertEqual([m["tibbers"] for m in members], [True, True, False])
        self.assertTrue(self.ann.view()["inRoom"])

    def test_a_closed_client_leaves_the_room(self):
        settle(self.ann, self.bea)
        self.ann.client_open = False
        settle(self.ann, self.bea)
        self.assertIsNone(self.ann.published())
        self.assertFalse(self.bea.sees("ann")["tibbers"])

    def test_a_missed_phase_read_changes_nothing(self):
        settle(self.ann, self.bea)
        self.ann.client.phase_answers = False
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), EMPTY)
        self.assertTrue(self.bea.sees("ann")["tibbers"])

    def test_switching_sharing_off_leaves_and_forgets_the_room(self):
        settle(self.ann, self.bea)
        self.ann.enabled = False
        settle(self.ann, self.bea)
        self.assertIsNone(self.ann.published())
        self.assertEqual(self.ann.view(),
                         {"enabled": False, "inRoom": False, "error": None,
                          "members": []})

    def test_a_listener_hears_each_snapshot_once(self):
        settle(self.ann, self.bea)
        heard = len(self.ann.changes)
        settle(self.ann, self.bea)
        self.assertEqual(len(self.ann.changes), heard)


class Picks(unittest.TestCase):

    def setUp(self):
        self.world = World("ann", "bea", "cat")
        self.ann = Machine(self.world, "ann")
        self.bea = Machine(self.world, "bea")
        settle(self.ann, self.bea)

    def champ_select(self, ann=(103, False), bea=(64, False), cat=(99, True)):
        team = [("ann", *ann), ("bea", *bea), ("cat", *cat)]
        self.world.phase = "ChampSelect"
        self.world.sessions = {p: select(p, team) for p in ("ann", "bea", "cat")}

    def test_nothing_goes_out_before_champ_select(self):
        self.ann.lobby.publish(103, 103015, None)
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), EMPTY)

    def test_a_pick_reaches_the_rest_of_the_party(self):
        self.champ_select()
        self.ann.lobby.publish(103, 103015, 103016)
        settle(self.ann, self.bea)
        seen = self.bea.sees("ann")
        self.assertEqual((seen["championId"], seen["skinId"], seen["chromaId"]),
                         (103, 103015, 103016))

    def test_leaving_champ_select_takes_the_pick_back(self):
        self.champ_select()
        self.ann.lobby.publish(103, 103015, None)
        settle(self.ann, self.bea)
        self.world.phase = "Lobby"
        self.world.sessions = {}
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), EMPTY)

    def test_a_row_from_nobody_in_the_party_is_never_read(self):
        self.champ_select()
        room = self.world.room()
        strangers = []
        for member in ("0123456789abcdef", lobby.member_id(room, "dan")):
            stranger = lobby.MemoryTransport(self.world.relay)
            stranger.connect(room, member, lambda rows: None, lambda m: None)
            stranger.send(lobby.row(64, 64012, None))
            strangers.append(stranger)
        settle(self.ann, self.bea)
        members = self.ann.view()["members"]
        self.assertEqual([m["name"] for m in members], ["Ann", "Bea", "Cat"])
        self.assertNotIn(64012, [m["skinId"] for m in members])

    def test_a_member_without_tibbers_shows_their_champion(self):
        self.champ_select(cat=(99, True))
        settle(self.ann, self.bea)
        cat = self.ann.sees("cat")
        self.assertFalse(cat["tibbers"])
        self.assertEqual(cat["championId"], 99)
        self.assertIsNone(cat["skinId"])

    def test_a_changed_pick_replaces_the_last(self):
        self.champ_select()
        self.ann.lobby.publish(103, 103015, 103016)
        settle(self.ann, self.bea)
        self.ann.lobby.publish(103, 103001, None)
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), {"c": 103, "s": 103001, "k": None})


class Customs(unittest.TestCase):
    """Only a custom game can put party members on the other team."""

    def setUp(self):
        self.world = World("ann", "bea")
        self.ann = Machine(self.world, "ann")
        self.bea = Machine(self.world, "bea")
        settle(self.ann, self.bea)

    def custom(self, blind, their_puuid="bea", locked=False, **flags):
        session = select("ann", [("ann", 103, locked)],
                         [(their_puuid, 64, False)], custom=True, blind=blind)
        session.update(flags)
        self.world.phase = "ChampSelect"
        self.world.sessions["ann"] = session

    def pick(self):
        self.ann.lobby.publish(103, 103015, None)
        settle(self.ann, self.bea)
        return self.ann.published()

    def test_no_party_member_opposite_shares_every_change(self):
        self.custom(blind=True, their_puuid="a-stranger")
        self.assertEqual(self.pick(), {"c": 103, "s": 103015, "k": None})

    def test_a_blind_pick_is_held_until_the_game_starts(self):
        self.custom(blind=True, locked=True)
        self.assertEqual(self.pick(), EMPTY)
        self.world.phase = "GameStart"
        self.world.sessions = {}
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), {"c": 103, "s": 103015, "k": None})

    def test_a_draft_pick_goes_out_once_locked(self):
        self.custom(blind=False)
        self.assertEqual(self.pick(), EMPTY)
        self.custom(blind=False, locked=True)
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), {"c": 103, "s": 103015, "k": None})

    def test_an_opponent_with_no_puuid_counts_as_the_party(self):
        self.custom(blind=True, their_puuid="")
        self.assertEqual(self.pick(), EMPTY)

    def test_flags_the_client_left_out_count_as_a_custom_blind(self):
        self.custom(blind=True, locked=True)
        del self.world.sessions["ann"]["isCustomGame"]
        del self.world.sessions["ann"]["hasSimultaneousPicks"]
        self.assertEqual(self.pick(), EMPTY)

    def test_nothing_goes_out_before_the_session_is_read(self):
        self.world.phase = "ChampSelect"
        self.world.sessions = {}
        self.assertEqual(self.pick(), EMPTY)

    def test_a_read_that_misses_the_other_team_does_not_undo_one_that_saw_it(self):
        self.custom(blind=True)
        self.pick()
        self.world.sessions["ann"]["theirTeam"] = []
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), EMPTY)

    def test_the_next_champ_select_starts_from_nothing(self):
        self.custom(blind=True)
        self.pick()
        self.world.phase = "Lobby"
        settle(self.ann, self.bea)
        self.world.phase = "ChampSelect"
        self.world.sessions["ann"] = select("ann", [("ann", 103, False)])
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), {"c": 103, "s": 103015, "k": None})


class Lifecycle(unittest.TestCase):

    def setUp(self):
        self.world = World("ann", "bea", "cat")
        self.ann = Machine(self.world, "ann")
        self.bea = Machine(self.world, "bea")
        settle(self.ann, self.bea)

    def test_leaving_the_party_leaves_the_room_and_rejoining_returns(self):
        self.world.players.remove("ann")
        settle(self.ann, self.bea)
        self.assertIsNone(self.ann.published())
        self.assertFalse(self.ann.view()["inRoom"])
        self.assertEqual([m["name"] for m in self.bea.view()["members"]],
                         ["Bea", "Cat"])
        self.world.players.append("ann")
        settle(self.ann, self.bea)
        self.assertTrue(self.bea.sees("ann")["tibbers"])

    def test_a_new_party_is_a_new_room(self):
        old = self.world.room()
        self.world.party_id = "a-whole-new-party"
        settle(self.ann, self.bea)
        self.assertEqual(list(self.world.relay.rooms), [self.world.room()])
        self.assertNotIn(old, self.world.relay.rooms)
        self.assertTrue(self.bea.sees("ann")["tibbers"])

    def test_the_room_is_kept_through_loading_until_the_game_is_hooked(self):
        self.world.phase = "GameStart"
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), EMPTY)

    def test_once_hooked_the_socket_goes_and_the_room_stays_on_screen(self):
        self.world.phase = "InProgress"
        self.ann.hooked = True
        settle(self.ann, self.bea)
        self.assertIsNone(self.ann.published())
        view = self.ann.view()
        self.assertFalse(view["inRoom"])
        self.assertTrue(self.ann.sees("bea")["tibbers"])

        self.world.phase = "EndOfGame"
        self.ann.hooked = False
        settle(self.ann, self.bea)
        self.assertEqual(self.ann.published(), EMPTY)

    def test_a_relay_that_drops_is_reported_and_what_was_heard_is_kept(self):
        self.world.phase = "ChampSelect"
        team = [("ann", 103, True), ("bea", 64, True)]
        self.world.sessions = {p: select(p, team) for p in ("ann", "bea")}
        self.ann.lobby.publish(103, 103015, None)
        self.bea.lobby.publish(64, 64012, None)
        settle(self.ann, self.bea)

        self.world.relay.drop()
        settle(self.ann, self.bea)
        self.assertTrue(self.ann.view()["error"])
        self.assertEqual(self.ann.sees("bea")["skinId"], 64012)

        self.world.relay.restore()
        settle(self.ann, self.bea)
        self.assertIsNone(self.ann.view()["error"])
        self.assertEqual(self.ann.published(), {"c": 103, "s": 103015, "k": None})

    def test_the_thread_leaves_the_room_when_stopped(self):
        world = World("ann", "bea")
        ann = Machine(world, "ann")
        ann.lobby.start()
        eventually(lambda: world.room() in world.relay.rooms)
        ann.lobby.stop()
        ann.lobby.join(timeout=5)
        self.assertFalse(ann.lobby.is_alive())
        self.assertEqual(world.relay.rooms, {})


class Composition(unittest.TestCase):
    """What the app builds from a room, and the word it shows for each pick."""

    @staticmethod
    def member(me=False, tibbers=True, c=None, s=None, k=None):
        return {"me": me, "name": "", "tibbers": tibbers,
                "championId": c, "skinId": s, "chromaId": k}

    def status(self, member, champion=103, armed=None, unavailable=(),
               building=False, hooked=False):
        picks = lobby.party_picks({"members": [member]}, champion)
        return lobby.status(member, picks, armed or {}, set(unavailable),
                            building, hooked)

    def test_the_picks_are_everyone_elses_skins_in_party_order(self):
        snap = {"members": [self.member(me=True, c=103, s=103015),
                            self.member(c=64, s=64012),
                            self.member(c=99, s=99007, k=99008)]}
        self.assertEqual(lobby.party_picks(snap, 103),
                         ((64, 64012, None), (99, 99007, 99008)))

    def test_a_pick_for_your_champion_is_dropped_whatever_you_chose(self):
        snap = {"members": [self.member(c=103, s=103001)]}
        self.assertEqual(lobby.party_picks(snap, 103), ())

    def test_the_first_member_in_the_party_keeps_a_champion(self):
        snap = {"members": [self.member(c=64, s=64012), self.member(c=64, s=64001)]}
        self.assertEqual(lobby.party_picks(snap, None), ((64, 64012, None),))

    def test_no_skin_a_base_skin_or_no_tibbers_is_nothing_to_build(self):
        snap = {"members": [self.member(c=64), self.member(c=99, s=99000),
                            self.member(tibbers=False, c=12, s=12001)]}
        self.assertEqual(lobby.party_picks(snap, None), ())

    def test_a_pick_in_the_armed_overlay_is_armed(self):
        pick = self.member(c=64, s=64012)
        self.assertEqual(self.status(pick, armed={"party": [[64, 64012, None]]}),
                         "armed")

    def test_a_pick_on_its_way_is_building(self):
        self.assertEqual(self.status(self.member(c=64, s=64012), building=True),
                         "building")

    def test_a_pick_that_could_not_be_built_is_unavailable(self):
        self.assertEqual(self.status(self.member(c=64, s=64012),
                                     unavailable={(64, 64012, None)}),
                         "unavailable")

    def test_a_pick_for_your_own_champion_is_unavailable(self):
        self.assertEqual(self.status(self.member(c=103, s=103001)), "unavailable")

    def test_a_pick_after_the_game_is_hooked_is_late(self):
        self.assertEqual(self.status(self.member(c=64, s=64012), hooked=True),
                         "late")

    def test_your_own_row_is_armed_when_the_patcher_holds_it(self):
        mine = self.member(me=True, c=103, s=103015)
        self.assertEqual(self.status(mine, armed={"skinId": 103015,
                                                  "chromaId": None}), "armed")
        self.assertEqual(self.status(mine, armed={"skinId": 103001}), "none")

    def test_no_pick_is_none(self):
        self.assertEqual(self.status(self.member(tibbers=False, c=64, s=64012)),
                         "none")
        self.assertEqual(self.status(self.member(c=64)), "none")


@unittest.skipUnless(HAVE_WEBSOCKET, "needs websocket-client")
class Unreachable(unittest.TestCase):

    def test_a_relay_that_cannot_be_reached_is_reported(self):
        errors = queue.Queue()
        transport = lobby.WsTransport("ws://127.0.0.1:9")
        transport.connect(secrets.token_hex(16), secrets.token_hex(8),
                          lambda rows: None, errors.put)
        try:
            self.assertTrue(errors.get(timeout=10))
        finally:
            transport.close()


@unittest.skipUnless(HAVE_WEBSOCKET and os.environ.get("TIBBERS_LOBBY_TEST_URL"),
                     "needs a relay: scripts/lobby_smoke.sh runs these")
class AgainstRelay(unittest.TestCase):

    def setUp(self):
        self.url = os.environ["TIBBERS_LOBBY_TEST_URL"]
        self.room = secrets.token_hex(16)
        self.transports = []

    def tearDown(self):
        for transport in self.transports:
            transport.close()

    def open(self, member, on_state=lambda rows: None, on_error=lambda m: None):
        transport = lobby.WsTransport(self.url)
        transport.connect(self.room, member, on_state, on_error)
        self.transports.append(transport)
        return transport

    @staticmethod
    def until(states, check, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            rows = states.get(timeout=max(0.01, deadline - time.monotonic()))
            if check(rows):
                return rows

    def test_a_row_sent_straight_away_crosses_the_relay(self):
        ann, bea = secrets.token_hex(8), secrets.token_hex(8)
        heard = queue.Queue()
        self.open(bea, on_state=heard.put)
        self.open(ann).send(lobby.row(103, 103015, 103016))
        rows = self.until(heard, lambda rows: (rows.get(ann) or {}).get("k"))
        self.assertEqual(rows[ann], {"c": 103, "s": 103015, "k": 103016})
        self.transports[1].close()
        self.until(heard, lambda rows: ann not in rows)

    def test_a_second_connection_as_the_same_member_replaces_the_first(self):
        ann = secrets.token_hex(8)
        errors, heard = queue.Queue(), queue.Queue()
        self.open(ann, on_state=heard.put, on_error=errors.put)
        self.until(heard, lambda rows: ann in rows)
        self.open(ann)
        deadline = time.monotonic() + 10
        while True:
            message = errors.get(timeout=max(0.01, deadline - time.monotonic()))
            if message:
                break
        self.assertIn("as you", message)

    def test_two_lobbies_in_one_party_see_each_others_picks(self):
        world = World("ann", "bea", party_id=secrets.token_hex(18))
        world.phase = "ChampSelect"
        team = [("ann", 103, True), ("bea", 64, True)]
        world.sessions = {p: select(p, team) for p in ("ann", "bea")}
        ann = Machine(world, "ann", lobby.WsTransport(self.url))
        bea = Machine(world, "bea", lobby.WsTransport(self.url))
        self.transports += [ann.lobby.transport, bea.lobby.transport]
        ann.lobby.publish(103, 103015, None)
        bea.lobby.publish(64, 64012, None)

        def both_see():
            settle(ann, bea)
            return ((ann.sees("bea") or {}).get("skinId") == 64012
                    and (bea.sees("ann") or {}).get("skinId") == 103015)
        eventually(both_see, timeout=10)


if __name__ == "__main__":
    unittest.main()
