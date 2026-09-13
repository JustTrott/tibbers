# How tibbers works

This is the long version of the README's "How it works" paragraph — the
overlay mechanism, why the game is never frozen, how a skin mod is built out of
your own game, the passwordless helper's design, party skin sharing, and why the Windows approach
cannot work on macOS.

For the operational rules (safe moments to restart, the dev scripts) see
[`CLAUDE.md`](CLAUDE.md); for visual decisions see [`DESIGN.md`](DESIGN.md);
for who it is for see [`PRODUCT.md`](PRODUCT.md).

## The overlay, in two stages

```
League client ──(LCU REST)──> tibbers ──> web UI  :7777   you hover / pick
                                  │
                   you pick a skin│
                                  ▼
                    mod-tools mkoverlay    builds a replacement WAD
                    mod-tools runoverlay   starts, waits for the game  [root]
                                  │
                     game starts  │
                                  ▼
                    patcher finds it and hooks fopen, on its own
```

Both stages come from [cslol](https://github.com/LeagueToolkit/cslol-manager)'s
`mod-tools`, built from a pinned commit with our own copy of its macOS
patcher -- see `tools/patcher/README.md` for what that copy changes and why
League 16.18 made it necessary:

- **`mkoverlay`** — pure local file work. Reads the game's WADs, merges your
  chosen mod, writes a replacement WAD into tibbers' own directory. Touches
  nothing in the League install, needs no privileges.
- **`runoverlay`** — attaches to the *running game process* and hooks `fopen`
  so reads of `.wad.client` are redirected into that overlay. Needs root
  (`task_for_pid`), and is the only elevated step. It writes the hook into a
  function of the game's it overwrites anyway, and allocates nothing: the
  game's anti-cheat kills it when an external process allocates memory in it.

The function it overwrites is Riot's, found by scanning for Riot's code, so a
League patch can move it. `patchcheck.py` reads the installed binary and
confirms both — one match, and a function with room for the payload — before
any patcher is started, so a build that has moved on turns skins off with a
reason instead of leaving a game that will not open.

## The game is never suspended

`runoverlay` has its own loop — it polls for the game every 10 ms, then scans
and patches it when it is ready:

```c
for (;;) {
    pid = FindPid("/LeagueofLegends");
    if (!pid) { M_WAIT_START; sleep_ms(10); continue; }
    M_FOUND;  scan(process);  M_PATCH;  patch(process);
}
```

So the patcher is started when you pick a skin, and then left alone. Freezing
the game at spawn — which is what the Windows tools do, using a different
injection mechanism — makes the patcher scan a process that has not finished
loading, compute wrong offsets, and write the hook over the wrong addresses.
That crashes the game the moment it resumes.

The patcher's status output is captured to `runoverlay.log`, so "did it work"
is answered by what it actually reported rather than assumed:

```
Status: Waiting for league match to start
Status: Found League
Status: Scanning
Status: Patching
Status: Waiting for exit
```

## Where the mods come from

**Mods are built out of your own install. Nothing is downloaded, and no skin
repository is contacted at any point.** A skin mod is a few kilobytes: it
rewrites the base skin's asset pointers to reference the requested skin's files,
which already ship with the game. Nothing is added and nothing crosses the wire.

`skinsmith.py` takes the champion's `skin<N>.bin` out of
`DATA/FINAL/Champions/<Champ>.wad.client`, keeps the two entries that describe
the skin, points them at the base skin's names, and writes them back out as a
`.fantome`:

```
skins/<championId>/<skinId>/<skinId>.fantome
```

It happens **when you hover a champion** in champ select — about a second for a
whole champion — so by the time you lock in the mods are already there. A skin
the recipe cannot express simply has no mod: it stays dimmed in the picker, and
the log says what stood in the way.

Downloading from a community repository used to be the fallback, and the recipe
was worked out against its files: 1,526 of the 1,546 mods in a full library
came back byte-identical. The remaining twenty were not built from *this*
install — a hand-merged audio bank, artwork and animation graphs from an older
patch, asset paths spelled out in a casing that exists only in an external hash
list — so ours differ from theirs. Those twenty are named, with what stands in
the way of each, in `tests/test_skinsmith.py`.

A generated mod carries a `.source.json` beside it naming the archive it came
out of, so a patch that rewrites that archive is noticed and the mod is rebuilt
the next time you hover the champion. **Rebuild library** in settings does the
lot in one pass.

## The build & counter data

The build and counters pages render statistics fetched from public sources —
nothing tibbers computes or claims as its own:

- **op.gg** (`opgg.py`) — everything: builds, runes, skills, summoners and
  counters for every mode, plus Arena's augments, items and tier list. One
  request per champion and lane carries the build and the counters together,
  over plain `urllib` with an honest user-agent — the API host's `robots.txt`
  is `Disallow:`, permitting all of it. The payload names its own patch, so
  there is no manifest to resolve. What it does not publish is a build against
  a named lane opponent, or gold at fifteen; counters carry each matchup's win
  rate, and that is what the pages show.
- **u.gg** (`ugg.py`) — ARAM Mayhem's augment rankings, and nothing else.
  They sit on a static host (`MAYHEM_BASE`) that serves them without a
  challenge, so this too is plain `urllib`. Mayhem has no build file of its
  own, so its items are ARAM's and are stamped as borrowed; its augments are
  its own and are fetched whether or not the build arrived. u.gg's *build* CDN
  is no longer used: it scores the TLS handshake and refuses roughly one
  request in five, which is what the spoofed user-agent, the alternative
  header sets and a downloaded browser-impersonating curl all existed to get
  past. All of that is gone.

Names and icons for everything on those pages come from the League client's own
data (`gamedata.py`), never from the stats sites. Every page in the app links
back to the exact op.gg page its numbers came from, and importing a
build writes it back into the client as a rune page, spell pair, and item set
(`importer.py`).

## Skipping the password prompt

`main.py --install-helper` installs a small root-owned wrapper plus a `sudoers`
rule scoped to it, so injection stops prompting. The design is deliberately
narrow, because the obvious version is a local root exploit:

A `NOPASSWD` rule pointed straight at `mod-tools` would be a local root
escalation for **any** process on the machine — the same binary implements
`mkoverlay`, which writes to a destination given on the command line. So the
rule targets a wrapper that:

- runs only `runoverlay`, never a subcommand that writes
- execs a *fixed*, root-owned copy of mod-tools, not whatever is on your `PATH`
- refuses any path outside your own `~/Library/Application Support/tibbers/`
- refuses paths containing `..`

The wrapper and its mod-tools copies are installed `0755 root:wheel` in
`/Library/PrivilegedHelperTools` — if you could rewrite them, the rule would
hand you root — so the ownership and mode of the files *and their parent
directories* are re-verified before **every** privileged run, falling back to
prompting if anything has drifted.

That location is not arbitrary: it contains no spaces. sudoers treats an
unescaped space as an argument separator, so a rule naming
`/Library/Application Support/...` parses as the command `/Library/Application`
with arguments — which `visudo -c` accepts as valid, and which then simply
never matches. Installation *exercises* the rule before reporting success, so a
mismatch fails immediately instead of mid-champ-select.

One further bound: cslol's patcher hardcodes the process it attaches to
(`FindPid("/LeagueofLegends")`), so the wrapper cannot be aimed at any other
process. The full implementation is in
[`tibbers/privileged.py`](tibbers/privileged.py).

## Why not Rose / Pengu

[Rose](https://github.com/Alban1911/Rose) on Windows loads JavaScript plugins
into the League client's UI (via Pengu Loader) so you can hover locked skins in
champ select. That relies on IFEO — a *registry* hijack that touches no file in
the game install.

macOS has no IFEO. Every route to loading those plugins requires modifying the
client bundle, and **Riot's client verifies its own files**: patching
`libEGL.dylib` flipped the launcher's button to "Repair", and pressing it
reverted the change and deleted the backup. `DYLD_INSERT_LIBRARIES` is blocked
too — `RiotClientServices` sets the hardened runtime, so dyld strips `DYLD_*`
from the launch chain.

So tibbers drops the client-UI half entirely. You pick the skin in its own
window instead of in champ select, which needs no client modification at all —
and is why the Repair button never appears.

**Architecture note.** Pengu's macOS core is x86_64-only, which forced the
client under Rosetta. Without Pengu that constraint is gone, so League runs
natively. `mod-tools` still has to match the *game process* architecture, since
cslol's patcher writes arch-specific shellcode — both builds are installed and
the right one is chosen at injection time by reading the process's Rosetta flag.

## Party skin sharing

Off until the player turns it on. [`LOBBY.md`](LOBBY.md) is the design and the
reasoning behind it; this is the shape.

- **The room is the League party.** `/lol-lobby/v1/parties/player` gives every
  member the same `partyId`, at every phase. The room is
  `sha256("tibbers-party-1" + partyId)` cut to 32 hex, and a member is
  `sha256(room + puuid)` cut to 16, so each install works out every other
  member's id from the roster it already has. Neither raw id leaves the machine.
- **Only ids travel.** A row is `{c, s, k}`: champion, skin, chroma. Each
  install builds the others' skins out of its own game with `skinsmith`, as it
  builds its own, and `mkoverlay` merges them into the one overlay with yours.
  A pick for your own champion is dropped, because a mod dresses everyone
  playing that champion.
- **The relay** is `worker/`: a Cloudflare Worker with a Durable Object per
  room, holding a hibernating WebSocket per member. It checks every row (a skin
  id must name its own champion), stores nothing, and a room ends with its last
  socket. `scripts/lobby_smoke.sh` runs it, and the Python client against it,
  on `wrangler dev`.
- **When a row goes out** is `tibbers/lobby.py`'s decision: on every change in
  matchmade queues, where the whole party is on your team; in a custom game with
  party members opposite, once locked in a draft and only after champ select in
  a blind pick.
- **Nothing waits on it.** The lobby keeps a snapshot that arming reads, and a
  relay that cannot be reached leaves your own skin exactly as it would be
  without the feature. The socket closes once the patcher has hooked the game,
  since nothing reaches the game after that.

## The code

```
main.py                 orchestration: watch phase, apply on game start
tibbers/system.py       install discovery, process/arch detection, elevation
tibbers/lcu.py          League Client API client + phase watcher
tibbers/gamedata.py     the client's own runes, items, spells and abilities
tibbers/library.py      local mod library  (skins/<champId>/<skinId>/…)
tibbers/downloader.py   on-demand per-champion mods, built on hover
tibbers/skinsmith.py    builds a mod out of the installed game
tibbers/wad.py          Riot's archive format, read and written
tibbers/injector.py     mkoverlay + runoverlay + the patcher's lifetime
tibbers/patchcheck.py   whether this League build can still be patched
tibbers/privileged.py   the root-owned helper and its sudoers rule
tibbers/modes.py        which mode this is, and which tabs it earns
tibbers/ugg.py          ARAM Mayhem augment rankings from u.gg
tibbers/opgg.py         builds, counters and Arena statistics from op.gg
tibbers/guide.py        those numbers, wearing the client's names and icons
tibbers/importer.py     writes the build back: rune page, spells, item set
tibbers/server.py       local HTTP API, art proxy, reload channel
tibbers/shell.py        menu bar item, settings window, picker window
tibbers/prefs.py        settings, remembered picks, window geometry
tibbers/lobby.py        party skin sharing: the room, the relay client, when to send
tibbers/mock.py         a scriptable stand-in for the League client
tibbers/static/         the picker UI, the settings page, the mock client
worker/                 the party relay: a Cloudflare Worker, one room per party
scripts/                dev, phase, deploy, build, fetch_modtools, fetch_python,
                        lobby_smoke
tests/                  payload builders  (python -m unittest discover tests)
```

Skins live in `~/Library/Application Support/tibbers/skins`.
