# 1.2.0: the lobby

*Plan. Nothing here is built yet. Written 2026-09-09 against the 1.1.0 branch.*

Several people who queue together, each running tibbers, each seeing the
others' chosen skins in game. No skin data ever leaves a machine: every
install already holds every champion's skins and `skinsmith.py` builds any
of them locally. The only thing that travels is *who picked what*, a few
dozen bytes per player.

**The party is the unit.** A room is your League party, nothing else. You
share with the people you grouped with and with nobody else. Randoms never
see your skin, and you never see theirs. That is the whole competitive
story: no information crosses a boundary the client has not already
crossed. A custom lobby is a party too, which is where both teams appear
in one room, and that is the one case needing care (below).

## What a viewer sees

- A **Lobby** tab beside Skin and Build. It lists the party: name, a
  tibbers mark on the people running it, the champion once champ select
  starts, and the skin each of them picked. Your own row reads *you*.
- The tab is useful *before* champ select. Sitting in a lobby it already
  says who has tibbers, so the answer to "are we all set up" comes before
  the timer starts rather than during it.
- A switch on that tab, *Share skins with your party*, mirrored in
  Settings. **Off by default.** Off means the tab shows the switch and one
  sentence, and tibbers makes no network request for any of this.
- Solo queue sends nothing. A party of one opens no room at all.

## The room is the party

Verified against a running client on 2026-09-09 by reading the client's
own API schema (`/help?format=Full`) and probing the live endpoints.

`GET /lol-lobby/v1/parties/player` **answers 200 with no lobby, no queue
and no game** (confirmed on an idle client). It carries:

```
{ "puuid": ..., "currentParty": {
    "partyId": "<uuid, 36 chars>", "partyType": "closed", "version": N,
    "players": [ { "puuid": ..., "summonerId": ..., "team": ..., "role": ... } ] } }
```

`GET /lol-lobby/v2/lobby` gives the richer view while an actual lobby
exists (404 otherwise, confirmed): `partyId`, `members` (each a
`LolLobbyLobbyParticipantDto` with `puuid`, `summonerId`, `teamId`,
`isBot`, `isLeader`), and a `gameConfig` carrying `isCustom`, `pickType`,
`queueId`, `mapId` and, for customs, `customTeam100` and `customTeam200`
as full participant lists.

Confirmed live in a three-player ranked flex lobby: both endpoints report
the **same** `partyId`, every occurrence of it in either body is the same
value, and it is a UUID.

**Confirmed across machines.** Two members of one party ran
`scripts/party_id.bat` independently and printed the same room,
`26a673432508`. The second machine also printed its own member id as
`2881cd20fe`, which is the value the *first* machine had already derived
for that person from the party roster alone. So both halves of the scheme
hold in practice: everyone agrees on the room, and each client can compute
every other member's id locally. That is what lets a row identify its
author with nothing on the wire naming anyone.

**Names do not come from the lobby.** `summonerName` on a lobby
participant is an empty string in practice, and so is `displayName`
everywhere. The populated source is `GET /lol-lobby/v2/comms/members`,
which returns `partyId` at the top level and a `players` map whose entries
carry `puuid`, `gameName` and `tagLine`. That is what the tab renders, and
it is joined to a row by `puuid`. A member with no name shows their
champion.

Two consequences shape everything below.

- **`partyId` is a UUID.** It is shared by exactly the party and by nobody
  outside it, and it cannot be enumerated. That makes it a room key with
  no scanning surface, which the game id never was. It is still hashed
  before it leaves the machine (below), so the relay never learns it.
- **The party outlives the lobby.** `parties/player` answers at every
  phase, so tibbers can join its room whenever it is started: in the
  lobby, mid champ select, or in the loading screen. There is no window to
  miss and no rule about when the app had to be open.

Room and member ids:

| id | value |
|---|---|
| room | `sha256("tibbers-party-1" + partyId)` truncated to 32 hex |
| member | `sha256(room + puuid)` truncated to 16 hex |

The raw `partyId` and `puuid` never leave the machine. Every party member
derives the same room id, and each derives every other member's id from
the puuids the party already gave them, so rows attribute themselves with
nothing on the wire naming a person. The member id is per-room, so the
same person in two parties is two unrelated ids.

## What travels

One row per player:

```
{"c": 103, "s": 103015, "k": null}      championId, skinId, chromaId
```

Nothing else. No names, no summoner ids, no raw puuid, no raw party id, no
game id anywhere in the design. `c` is null until champ select. `s` null
means the selection was cleared.

Skin and chroma ids follow League's own `championId * 1000 + n`
convention. The relay checks `s // 1000 == c` and rejects anything else,
so a room cannot be used to store arbitrary data.

**Rows attribute themselves, cells are matched by champion.** A row's
member id already proves its author is in the party, so nothing needs to
be read out of champ select to know who sent it. Placing a row against a
champ select cell is then just `championId`, which is unique within a
team. This matters: Riot anonymises teammates in ranked and the champ
select `puuid` may or may not survive that, and with this rule the feature
does not care either way.

## When a row is published

The room is the party, so in every matchmade queue everyone in the room is
on your team and already watches you hover. There is nothing to withhold,
and rows go out on every selection change.

Customs are the exception, because the enemy team is in the lobby. The
policy is computed once when champ select starts:

- **Team-only room.** No room member's puuid appears in `theirTeam`.
  Publish on every change. Matchmade queues always land here, including
  when `theirTeam` has no puuids to compare against, which is the correct
  answer for the right reason.
- **Mixed room, sequential picks.** A custom draft, where locked picks are
  shown to both teams anyway. Publish on lock.
- **Mixed room, simultaneous picks.** A custom blind, where enemy picks
  are hidden until loading. A skin id names its champion, so publishing
  early would leak the pick. Hold every row until champ select ends, then
  publish. The loading screen is the window to build them, which is the
  same late-pick window the design already accepts.

`gameConfig.pickType` and `isCustom` decide this, so it is one flag rather
than a queue table.

## The relay

A Cloudflare Worker with one Durable Object class, `Room`, in `worker/`
in this repo (`wrangler.toml`, `src/index.ts`, roughly 150 lines), routed
at `lobby.tibbers.lol` on the existing Cloudflare zone.

**A WebSocket per member, on the Hibernation API.** This replaces the long
poll the first draft of this plan specified. Rose reached the same place
and its relay is the evidence that it works at this size (see *Prior art*
below). Hibernation wins on both counts that made long polling attractive:
a pick is pushed rather than waited for, and an idle room costs nothing at
all rather than costing the wall clock of every held request.

```
GET  /v1/room?room=<32 hex>            -> 101, WebSocket
  -> {"t":"join","m":"<16 hex>"}       first frame from the client
  -> {"t":"pick","c":103,"s":103015,"k":null}
  <- {"t":"state","m":{"<member>":{"c":103,"s":103015,"k":null}, ...}}
```

- **Hibernation.** `ctx.acceptWebSocket()` rather than holding the socket
  in a handler, member row stashed in `ws.serializeAttachment()`, and
  `setWebSocketAutoResponse('ping','pong')` so the keepalives never wake
  the object. Between picks the room is not running and not billed.
- **Lifetime.** The room exists while a socket is open and is gone when
  the last one closes. No storage, no alarm, nothing to expire. A party
  that re-queues simply opens the room again.
- **Broadcast.** Any change rebroadcasts the whole member map, so a
  late joiner is caught up by the next message and needs no history.
- **Limits, which Rose's relay has none of.** Room and member ids are
  fixed-length hex, frames are capped at 128 bytes, sixteen members per
  room, `s // 1000 == c` enforced so a row cannot carry arbitrary data,
  and a per-IP rate limit as a zone rule on the upgrade request. There is
  no listing endpoint: a room is reachable only by someone who already
  derived its id.
- **Versioned path** and a `User-Agent: tibbers/<version>` on the upgrade,
  so a later protocol can coexist with an older client.

The operator sees hashes, champion ids and skin ids. Never a name, never
an account id, and nothing that outlives the sockets.

## Cost

Hibernation moves the bill from duration to messages, and there are very
few messages. A room is billed in milliseconds while it handles a pick,
and not at all in between. The auto-answered pings are not requests.

Two properties keep it small before any of that matters.

- **Only parties open rooms.** A party of one is skipped entirely, so the
  many users who queue solo cost nothing.
- **Picks are rare.** A whole champ select is tens of messages for a
  party, not thousands.

Ten thousand daily users, with under a third of them queueing in parties
at four games a day, comes to roughly four million messages a month. The
$5 Workers Paid plan includes ten million requests, and the duration
involved is negligible against its included allowance. The practical
answer at the scale in question is a flat five dollars a month, and the
free plan covers a private group.

Check the figures against Cloudflare's current rates before step 1. They
are the reason to start on hibernation rather than treat it as an escape
hatch: the long-poll design was heading for real money at ten thousand
users, and this one is not.

## Prior art: Rose

Rose (`github.com/Alban1911/Rose`) ships party skin sharing already, and
its `party/` and `relay-worker/` trees are open. Reading it settled three
choices here.

**Borrowed.** The Hibernation API relay, as above. Rose's is about 130
lines, uses `acceptWebSocket`, `serializeAttachment` and the ping
auto-response, holds no storage, and lets the room end with its last
socket. There is no reason to invent a different shape.

**Deliberately not borrowed: how a room is named.** Rose generates a
random key and encodes a token, `ROSE:<b64(zlib(...))>`, that one person
pastes to everyone else; joining a room means leaving your own. That makes
a de-facto host, a sharing step before every session, an expiry, and a
bearer credential that works for anyone holding it, party or not. Deriving
the room from `partyId` has none of those: no host, no paste, no expiry,
and nothing forwardable to a stranger, because a non-member cannot know
the party id in the first place.

**Deliberately not borrowed: what travels.** Rose sends `summoner_id` and
`summoner_name` in plaintext, and its relay stores and rebroadcasts the
skin field verbatim with no validation beyond a key length check. This
plan sends neither identifier, hashes both, and validates the row. Worth a
line in the README when this ships, because it is the difference a user
would care about and cannot see.

**Confirmed independently.** Rose resolves each peer row to a local mod
and appends the folder names to its own before building the overlay, which
is exactly the *overlay becomes a set* step below. Rose also never moves
skin files, and a peer's custom mod applies only when a byte-identical
file is already on disk.

**Not present in Rose, so genuinely new work here.** Rose publishes on
hover and has no custom-lobby policy, so with tokens pasted across both
teams in a custom, an enemy row is neither withheld nor rejected. The
three-way disclosure policy above has nothing to copy from.

## The client

### `tibbers/lobby.py`

A `Lobby` thread owning one room at a time, constructed with a transport,
a `get_lcu` and an `on_change(snapshot)` callback. It is driven by the
existing `PhaseWatcher` rather than polling the client on its own
schedule.

- **Join** whenever `share_skins` is on and `parties/player` reports a
  `currentParty` with two or more players, at any phase. Derives the room
  and member ids and posts an empty row, which is what makes the tab able
  to say who is running tibbers before champ select.
- **Re-key** when `currentParty.partyId` changes. Members joining and
  leaving do not change the id, so this only fires on a genuinely
  different party. The old room is left to expire.
- **Publish** under the policy above. `on_select` and the champion branch
  of `on_change` in `main.py` call `lobby.publish(champion_id, skin_id,
  chroma_id)`.
- **Merge** each broadcast against the champ select session by champion
  id into the snapshot below, hand it to `on_change`, which stores it and
  calls `arm()`.
- **Stay connected** through `GameStart` into `InProgress` until
  `inject.overlay_in_use()` says the patcher has hooked. That is the real
  deadline; after it nothing can change, so the socket closes and the last
  snapshot stays on screen.
- **Leave** when the party drops below two, or sharing is switched off.
- **Fail soft.** A transport error marks the snapshot and retries with
  backoff to 30 s. `arm()` uses whatever snapshot exists and never waits
  on the network. A dead relay is exactly today's behaviour: your skin
  only.

`Transport` is `connect(room, member)`, `send(row)` and an `on_state`
callback, with reconnect and backoff behind it. `WsTransport` speaks the
WebSocket; the standard library has no client, so this is the one place
the plan needs a dependency, and a minimal RFC 6455 client over `socket`
and `ssl` is a few hundred lines if adding one is unwelcome. Decide at
step 1. `MemoryTransport` is a dict with direct delivery, for the mock and
the tests. `TIBBERS_LOBBY_URL` points a dev instance at `wrangler dev`.

### The overlay becomes a set

Today the overlay is one mod. Each skinsmith mod rewrites one champion's
`skin0` inside that champion's own WAD, so mods for different champions
never touch the same file, and cslol's `mkoverlay` already accepts several
mods in one `--mods:` argument. LTK consumes the same overlay tree.

- `Injector.build_overlay(fantomes)` and `Injector.prepare(fantomes, ...)`
  take a sequence. `_extract` runs per archive into `mods_dir` and the
  names are joined. Existing call sites pass a list of one.
- `arm()` composes the list: the local pick as today, then one entry per
  party row, via `library.find_mod` or, when the mod is not on disk,
  `downloader.prepare` to build it through skinsmith on the armer thread.
  A skin that cannot be built is skipped and its row says so. A chroma
  that cannot be built falls back to its skin.
- **One `skin0` per character.** Two rows can name the same champion in a
  custom blind. Your own pick wins, then party order.
- The *already armed* short circuit in `apply_request` compares a
  signature of the whole set. `LatestOnly` is unchanged: every party
  change re-arms and only the newest request survives a build in flight.
- Rebuilding under a *waiting* patcher is today's path;
  `overlay_in_use()` still refuses it once the game is hooked. A row that
  arrives after that shows as *seen too late* rather than being pretended
  into the game.

### State

`ServerState` grows one field, `lobby`, on `/api/state`:

```json
{"enabled": true, "inRoom": true, "error": null,
 "members": [
   {"me": true, "name": "you", "tibbers": true,
    "championId": 103, "championName": "Ahri",
    "skinId": 103015, "skinName": "Spirit Blossom Ahri",
    "chromaId": null, "status": "armed"}]}
```

`status` is `armed`, `building`, `unavailable`, `late`, or `none`. Skin
names come from `lcu.champion_skins(championId)`, cached per champion for
the session. `armed` keeps its shape for the local pick and gains
`others`, the skin ids built alongside it.

`modes.Mode.tabs` sends `lobby` after `skin` when sharing is on, never for
TFT. The rule that a tab must be paid for by data holds, with the one
allowance that a tab holding the switch is what teaches the feature
exists, and it says so in a line rather than rendering an empty list.

### Preferences

`share_skins` in `prefs.DEFAULTS`, default `False`, through
`/api/settings` like the rest, with a row in `settings.html`. Switching it
off mid champ select leaves the room and drops the others from the next
build.

### The Lobby tab

`view-lobby` in `index.html`, rendered from `s.lobby` behind the same
signature guard the other views use. At most five rows, so it fits 620x470
without scrolling. Each row is the champion icon from `/api/art/`, the
name, the tibbers mark, the skin tile and name, and a status word in muted
ink. Before champ select the champion and skin columns are empty and the
row is just a name and a mark. DESIGN.md gains the two new components: the
tibbers mark and the status words.

### Mock

`MockClient` gains `party` and `party_pick` actions writing into a
`MemoryTransport`, so the tab is designable with no client and no second
machine:

```
curl -s 127.0.0.1:7778/api/mock -d '{"action":"party","value":3}'
curl -s 127.0.0.1:7778/api/mock -d '{"action":"party_pick","value":{"slot":2,"championId":64,"skinId":64012}}'
```

Two `dev.sh --mock` instances on different ports and `TIBBERS_HOME`s,
pointed at `wrangler dev` through `TIBBERS_LOBBY_URL` with the same forced
party id, run the real protocol end to end with no client and no
injection.

## Order of work

Each step is one commit and leaves the app working.

1. **Relay.** `worker/` with the `Room` object, the upgrade route,
   validation, hibernation, and `scripts/lobby_smoke.sh` driving two
   members through a pick and a broadcast against `wrangler dev`.
2. **`lobby.py`.** Ids, join and re-key, the loop, the merge, both
   transports, backoff. Tests on `MemoryTransport` with a fake party and
   champ select cover matching by champion, a row from nobody, the
   solo-party skip, and leave and rejoin.
3. **Injector set.** `build_overlay` and `prepare` over a sequence, a test
   asserting the `mkoverlay` command line for two mods, one real build to
   `dist/`.
4. **Composition.** `arm()` over the set, the conflict rule, building
   missing mods on the armer thread, the set signature, `armed.others`.
5. **State and settings.** `state.lobby`, `share_skins`, the settings row,
   the tab in `modes`.
6. **The tab.** `view-lobby`, the mock actions, the DESIGN.md components.
7. **Words.** README on what leaves the machine and that it is off by
   default. ARCHITECTURE.md on the relay. The v1.2.0 draft release notes.

## Rules that still hold

- The injection command, its arguments and the hook do not change. The
  overlay gains WADs; the patcher starts exactly as it does today.
- The relay carries ids, never files. The skin path stays offline.
- A `TIBBERS_HOME` instance has injection off and can still join a room,
  which is how this gets developed while someone is playing.
- Tests never bind 7777 and never open a window.

## To verify on first contact

Each of these is cheap to check and none of them changes the shape.

- **A custom lobby's `partyId` is the same for every player in it.** The
  whole custom case rests on this. Two accounts in one custom lobby,
  comparing `/lol-lobby/v2/lobby`. If it turns out each side keeps its own
  party id, customs fall back to team-only sharing and lose nothing else.
- **`parties/player` still answers during champ select and in game.**
  Verified idle and in a lobby; the party is not the lobby, so it should,
  but joining at any time depends on it. If it stops mid game, the join simply happens
  earlier.
- **Arena subteams.** `subteamIndex` and `intraSubteamPosition` are on the
  participant, so a party in Arena is still just a party. Only the tab
  layout may want to group by subteam.
