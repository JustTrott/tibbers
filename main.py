#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tibbers -- pick a League skin from a local web UI; it is applied when the game
starts.

Deliberately does not modify anything inside the League installation. Riot's
client verifies its own files and repairs whatever changed, so the approach
Rose uses on Windows (loading plugins into the client UI) cannot work on macOS.
Instead the champion is read from the client's own API, the skin is chosen
here, and only the running game process is hooked.

    python main.py                 # start, opens the picker
    python main.py --no-browser    # start without opening a browser
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from tibbers import (downloader, importer, injector, lcu, library, modes,
                       prefs as prefs_mod, server, shell, skinsmith,
                       system)  # noqa: E402

log = logging.getLogger("tibbers")

#: macOS has the native menu-bar shell and the only elevated injection path;
#: Windows runs the same pages in a browser tab and needs no elevation. The
#: platform-specific bits key off this rather than sprinkling `sys.platform`.
IS_MACOS = sys.platform == "darwin"

#: How many guides to remember. Switching between the enemies in one champ
#: select should never refetch, and nothing older than this game is worth
#: holding.
GUIDE_MEMO_MAX = 8

#: The counters and against-this-team half, which is keyed by the whole enemy
#: team as well and so turns over faster.
SHARED_MEMO_MAX = 4

#: How long auto-import waits for the build it is meant to send, and how often
#: it looks. A lock-in beats the fetch more often than not.
AUTO_IMPORT_WAIT = 15.0
AUTO_IMPORT_POLL = 0.4


@dataclass(slots=True)
class Session:
    """What the champ-select handlers carry between polls.

    Held here rather than in `state`, which is what the picker reads: none of
    this is on screen. It was a dict addressed by fifteen string literals
    across five closures, where a typo makes a new key rather than an error --
    and where two fields had quietly gone dead.

    `slots` is the point of the exercise: without it a dataclass takes a
    misspelled attribute as happily as the dict did.
    """

    #: The queued skin and its chroma. Kept here rather than read from champ
    #: select at injection time: champ select has already ended when the game
    #: starts, so the live value is None exactly when it is needed.
    selected: Optional[int] = None
    chroma: Optional[int] = None
    champion_id: Optional[int] = None
    #: The overlay build and patcher start, while it runs.
    thread: Optional[threading.Thread] = None
    #: What the last poll reported, so a change can be told from a repeat.
    #: Tracked here rather than inferred from `state`: the mock writes state
    #: before notifying, so comparing against it always reports "unchanged".
    last_champion: Optional[int] = None
    was_locked: bool = False
    was_in_select: bool = False
    #: Bumped per fetch; a guide from an older generation is dropped rather
    #: than written over a situation that has moved on.
    guide_generation: int = 0
    #: The champ-select situation the guide on screen was fetched for.
    guide_key: Optional[tuple] = None
    opponent_by_hand: bool = False
    guide_memo: "OrderedDict[tuple, dict]" = field(default_factory=OrderedDict)
    shared_memo: "OrderedDict[tuple, dict]" = field(default_factory=OrderedDict)
    #: The champion auto-import has already run for. Keyed by champion rather
    #: than a flag, so a lock reported on every poll imports once, a patch
    #: change does not re-import, and re-picking does.
    auto_imported: Optional[int] = None
    #: The library rebuild, while one runs. One at a time: it walks every
    #: champion the client knows, and two of them racing would write the same
    #: files from two threads.
    rebuild: Optional[threading.Thread] = None


def memoise(memo: OrderedDict, key, value, keep: int):
    """Remember *value* under *key*, keeping only the newest *keep* entries."""
    memo[key] = value
    while len(memo) > keep:
        memo.popitem(last=False)
    return value


def build_skin_list(client: lcu.LCU, champion_id: int) -> list:
    """Every skin for the champion, marked with whether we have a mod for it."""
    available = library.available_for_champion(champion_id)
    skins = client.champion_skins(champion_id)
    for skin in skins:
        skin["available"] = skin["id"] in available
        have = library.available_chromas(champion_id, skin["id"])
        for chroma in skin["chromas"]:
            chroma["available"] = chroma["id"] in have
    # Skins we can actually apply come first; the rest stay visible so it is
    # obvious what is missing rather than silently absent.
    skins.sort(key=lambda s: (not s["available"], s["id"]))
    return skins


def _make_overlay(window) -> None:
    """Raise the window above other apps and let it follow every Space.

    on_top alone puts it at NSStatusWindowLevel, which is enough over ordinary
    windows. The collection behaviour is what keeps it visible when the game
    is on another Space or in macOS's own fullscreen.

    Nothing can float over an *exclusive fullscreen* game: that mode captures
    the display outright. League has to be in borderless for this to work.
    """
    try:
        import AppKit
    except ImportError:
        return

    native = getattr(window, "native", None)
    if native is None:
        log.debug("no native window handle; overlay flags not applied")
        return

    def apply() -> None:
        try:
            native.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorStationary
            )
            native.setLevel_(AppKit.NSStatusWindowLevel + 1)
            native.setHidesOnDeactivate_(False)
            log.info("overlay window configured (level %d)", native.level())
        except Exception as exc:  # noqa: BLE001
            log.debug("could not configure overlay window: %s", exc)

    # pywebview fires `shown` on a worker thread, and AppKit aborts the whole
    # process (SIGTRAP, no Python traceback) if a window is touched from off
    # the main thread. Hand the work to the main queue.
    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(apply)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pick a League skin from a web UI.")
    ap.add_argument("--port", type=int, default=None,
                    help="fixed port; without it the first free port from "
                         "7777 upward is used")
    ap.add_argument("--browser", action="store_true",
                    help="open in your web browser instead of an app window")
    ap.add_argument("--no-ui", action="store_true",
                    help="run headless; the UI is still served on --port")
    ap.add_argument("--home", metavar="DIR",
                    help="use DIR for the library, overlay and preferences "
                         "instead of the usual location, so a second instance "
                         "cannot disturb the running one")
    ap.add_argument("--settings", action="store_true",
                    help="open the settings window at launch, which otherwise "
                         "only happens the first time")
    ap.add_argument("--overlay", action="store_true",
                    help="float a compact window above the game instead of a "
                         "normal app window (needs League in borderless)")
    ap.add_argument("--no-browser", action="store_true",
                    help=argparse.SUPPRESS)  # back-compat alias for --no-ui
    ap.add_argument("--no-window", action="store_true",
                    help=argparse.SUPPRESS)  # what scripts/dev.sh calls it
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--install-helper", action="store_true",
                    help="one admin prompt, then injection never prompts again")
    ap.add_argument("--uninstall-helper", action="store_true")
    ap.add_argument("--helper-status", action="store_true")
    ap.add_argument("--dev", action="store_true",
                    help="development instance: injection is off and the "
                         "picker reloads itself when tibbers/static changes")
    ap.add_argument("--no-inject", action="store_true",
                    help="never build an overlay and never start the patcher; "
                         "the UI still works end to end")
    ap.add_argument("--allow-inject", action="store_true",
                    help="permit injection even with TIBBERS_HOME set, which "
                         "otherwise disables it")
    ap.add_argument("--keep-patcher", action="store_true",
                    help="always leave the patcher running on exit, not only "
                         "when a game is in progress")
    ap.add_argument("--stop-patcher", action="store_true",
                    help="stop a patcher left running by an earlier instance, "
                         "then exit")
    ap.add_argument("--check-update", action="store_true",
                    help="check GitHub Releases for a newer build, then exit")
    ap.add_argument("--quiet", action="store_true",
                    help="start without taking focus and without opening a "
                         "window -- for relaunching while a game is running")
    ap.add_argument("--mock", action="store_true",
                    help="drive the UI from a mock client overlay, so every "
                         "state can be opened without queueing a game")
    ap.add_argument("--demo", type=int, metavar="CHAMPION_ID",
                    help="seed the UI with a real champion's skins so the "
                         "interface can be worked on outside champ select")
    args = ap.parse_args()

    if args.home:
        # Before anything reads it: every path in the app derives from here.
        os.environ["TIBBERS_HOME"] = args.home

    handlers = [logging.StreamHandler()]
    # Launched from the Dock there is no console to print to, and a menu bar
    # app with nothing on screen has no other way to say what went wrong.
    try:
        from tibbers import system as _system
        log_path = _system.data_dir() / "tibbers.log"
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            log_path.unlink()
        handlers.append(logging.FileHandler(log_path))
    except OSError:
        pass

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    if sys.platform != "darwin":
        print("tibbers targets macOS.")
        return 2

    if args.check_update:
        from tibbers import update
        result = update.check()
        if result.get("error"):
            print(f"could not check: {result['error']}")
            return 1
        if result.get("available"):
            print(f"update available: {result['current']} -> {result['version']}")
        else:
            print(f"up to date ({result['current']})")
        return 0

    from tibbers import privileged
    if args.helper_status:
        print(privileged.describe())
        return 0
    if args.uninstall_helper:
        ok, msg = privileged.uninstall()
        print(msg)
        return 0 if ok else 1
    if args.install_helper:
        ok, msg = privileged.install(Path(__file__).parent / "tools")
        print(msg)
        return 0 if ok else 1

    game_dir, client_dir = system.find_install()
    if game_dir is None:
        print("League of Legends installation not found.")
        return 1

    tools_dir = Path(__file__).parent / "tools"
    modtools = tools_dir / "mod-tools"
    if not modtools.exists():
        print(f"mod-tools missing at {modtools}")
        print("Run: scripts/fetch_modtools.sh")
        return 1

    # Injection is off by construction in any instance running out of its own
    # TIBBERS_HOME. A dev instance shares the machine, the game and root with
    # the real one; the only thing keeping them apart is that it never builds
    # an overlay and never starts a patcher. Relying on the launcher to pass
    # --no-inject would make that a convention rather than a guarantee.
    scratch_home = bool(os.environ.get("TIBBERS_HOME"))
    can_inject = (not args.no_inject and not args.dev
                  and (args.allow_inject or not scratch_home))

    prefs = prefs_mod.Prefs()
    state = server.State()
    inject = injector.Injector(game_dir, tools_dir, system.data_dir() / "work",
                               enabled=can_inject)

    if args.stop_patcher:
        inject.stop_patcher()
        print("patcher stopped" if not inject.is_running()
              else "patcher is still running")
        return 0

    if not can_inject:
        why = ("--no-inject" if args.no_inject else "--dev" if args.dev
               else f"TIBBERS_HOME={os.environ['TIBBERS_HOME']}")
        state.say(f"injection disabled ({why}) -- "
                  f"no overlay is built and no patcher is started")

    # A patcher started before this app was restarted is still out there
    # waiting for -- or already hooked into -- the game. Take it over rather
    # than starting a second one, and above all leave its overlay alone.
    adopted = inject.adopt()
    if adopted:
        state.say(f"adopted the running patcher (pid {adopted.get('pid')})"
                  + (f" for skin {adopted['skinId']}"
                     if adopted.get("skinId") else ""))

    stats = library.stats()
    state.say(f"game: {game_dir}")
    if privileged.available():
        state.say("elevation: passwordless helper installed")
    elif privileged.stale():
        state.say("elevation: helper is from an older build -- "
                  "re-run with --install-helper (will prompt until then)")
    else:
        state.say("elevation: will prompt once per skin "
                  "(install the helper with --install-helper to stop that)")
    state.say(f"library: {stats['skins']} skins across "
              f"{stats['champions']} champions in {stats['path']}")

    watcher = lcu.PhaseWatcher(on_change=lambda s: None)
    session = Session()
    if adopted:
        # Come back describing what is actually applied, rather than an empty
        # picker over a game that already has a skin on it.
        session.selected = adopted.get("skinId")
        session.chroma = adopted.get("chromaId")
        session.champion_id = adopted.get("championId")
        with state.lock:
            state.selected_skin_id = adopted.get("skinId")
            state.selected_chroma_id = adopted.get("chromaId")
    from tibbers.gamedata import GameData
    from tibbers.guide import Guide
    from tibbers.ugg import Unavailable

    gamedata = GameData(lambda: watcher.lcu)
    guides = Guide(gamedata)

    # The same rule that keeps a dev instance away from the overlay keeps it
    # away from the account. A copy running out of its own TIBBERS_HOME still
    # talks to the one League client, and a rune page written from it lands on
    # the real account -- there is nothing else separating the two, so import
    # follows injection rather than having a second, weaker guard of its own.
    imports = importer.Importer(lambda: watcher.lcu, say=state.say,
                                stat_rows=gamedata.stat_rows,
                                dry_run=not can_inject)
    if not can_inject:
        state.say("import runs dry -- nothing is written to the client")

    def build_on_screen(mode: str) -> dict:
        """Exactly what the picker is showing, resolved the same way it is.

        Read from `state.guide` rather than refetched. The picker has already
        chosen between the general and the matchup build, and re-deriving it
        here from champion and role would let an import send something other
        than the numbers that were on screen when the button was pressed --
        the one failure this feature cannot afford.
        """
        with state.lock:
            guide = dict(state.guide or {})
            champion_id = state.champion_id
            champion_name = state.champion_name
            queue = dict(state.queue or {})

        if not champion_id:
            raise importer.Incomplete("no champion yet")
        if guide.get("state") != "ready":
            raise importer.Incomplete(
                guide.get("error") or "the build is not on screen yet")

        if not champion_name:
            champion_name = (gamedata.champion(champion_id) or {}).get("name") \
                or f"Champion {champion_id}"

        arena = guide.get("mode") == "arena"
        if arena:
            build = guide
        else:
            matchup = guide.get("matchup")
            build = matchup if (mode == "matchup" and matchup) \
                else (guide.get("general") or {})
        return {"build": build, "championId": champion_id,
                "championName": champion_name, "arena": arena,
                "kind": queue.get("kind"), "mapId": queue.get("mapId")}

    def import_build(payload: dict) -> dict:
        """Write the build on screen into the client."""
        mode = str(payload.get("mode") or "matchup").lower()
        what = str(payload.get("what") or "all").lower()
        if what not in ("all", "runes", "spells", "items"):
            return {"ok": False, "error": f"unknown import target: {what}"}
        raw = payload.get("confirmReplacePageId")
        replace = int(raw) if raw not in (None, "", 0) else None

        try:
            context = build_on_screen("general" if mode == "general" else "matchup")
        except importer.Incomplete as exc:
            result = {"ok": False, "error": str(exc), "done": []}
        else:
            result = imports.run(
                context["build"], context["championId"], context["championName"],
                what=what, kind=context["kind"], map_id=context["mapId"],
                arena=context["arena"], spells=prefs.get("import_spells"),
                replace_page_id=replace)
        result["at"] = time.time()
        with state.lock:
            state.last_import = result
        return result

    def auto_import(champion_id: int) -> None:
        """Import once, when you lock in, if that was asked for.

        The guide is fetched on the network and a lock-in beats it more often
        than not, so this waits for the page the user would have been looking
        at rather than importing whatever was on screen at the instant of the
        lock. It gives up rather than importing a stale champion's build.
        """
        deadline = time.time() + AUTO_IMPORT_WAIT
        while time.time() < deadline:
            with state.lock:
                ready = (state.guide or {}).get("state") == "ready"
                still = state.champion_id == champion_id
            if not still:
                return
            if ready:
                break
            time.sleep(AUTO_IMPORT_POLL)
        else:
            state.say("auto import: no build arrived in time")
            return
        result = import_build({"what": "all", "mode": "matchup"})
        if result.get("needsSlot"):
            # Said once and then left alone. Making room means deleting a page
            # the player owns, and that is never done without being asked for
            # by name -- least of all by something that fired on its own.
            state.say("auto import: all three rune pages are in use -- "
                      "press Import and choose one to replace")

    windows = None      # set once the UI shell exists
    menubar = None      # ditto; the tooltip is where state is reported

    def refresh_guide(champion_id: int, role, opponent, generation: int) -> None:
        """Fetch and resolve the guide off the watcher's thread.

        Champ select is on a timer and this reaches the network, so it never
        runs inline. Results are dropped if the situation moved on while they
        were in flight -- an enemy locking mid-fetch is normal, not an error.
        """
        def stale() -> bool:
            return session.guide_generation != generation

        try:
            patch = prefs.get("patch")
            with state.lock:
                queue = dict(state.queue)
            if queue.get("source") == "opgg":
                arena = guides.arena(champion_id)
                if stale():
                    return
                with state.lock:
                    state.guide = arena
                # The roster tier list is the champ-select half of Arena and
                # comes from a different file, so it lands separately rather
                # than holding the augment table behind it.
                try:
                    tiers = guides.arena_tiers(champion_id)
                except Unavailable as exc:
                    log.debug("no Arena tier list: %s", exc)
                    return
                if stale():
                    return
                with state.lock:
                    state.guide = {**state.guide, "tierList": tiers}
                return

            ugg_queue = queue.get("uggQueue")
            if queue.get("source") != "ugg" or not ugg_queue:
                # Better to say nothing than to dress Summoner's Rift numbers
                # up as Arena. Falling back to the ranked queue here would
                # have produced a confident, entirely wrong page.
                with state.lock:
                    state.guide = {"state": "unsupported",
                                   "label": queue.get("label") or "this mode"}
                return
            # ARAM and Arena assign no lane, so a role would select an empty
            # cell rather than the pooled one every game lands in.
            if not queue.get("roles", True):
                role = None
            memo_key = (champion_id, role, opponent, patch, ugg_queue)
            pair = session.guide_memo.get(memo_key)
            if pair is None:
                pair = memoise(session.guide_memo, memo_key,
                               guides.pair(champion_id, role, opponent,
                                           queue=ugg_queue, patch=patch),
                               GUIDE_MEMO_MAX)
            if stale():
                return
            with state.lock:
                state.guide = {**pair, "state": "ready",
                               "against": state.guide.get("against") or [],
                               "counterTables":
                                   state.guide.get("counterTables") or {}}
            # Counters are a lane idea. ARAM and Arena publish no matchup
            # file at all, and asking for one throws away a build that was
            # already fetched and is the more useful half anyway.
            if not queue.get("roles", True):
                return

            with state.lock:
                locked = [e["championId"] for e in state.enemies
                          if e.get("championId")]

            # The enemies belong in this key. Two of these three results are
            # about them, and the first run of a champ select happens on
            # hover, when nobody has locked yet -- keying without them pinned
            # that empty answer for the rest of the game.
            shared_key = (champion_id, role, opponent, patch, ugg_queue,
                          tuple(locked))
            shared = session.shared_memo.get(shared_key)
            if shared is not None:
                if stale():
                    return
                with state.lock:
                    state.guide = {**state.guide, **shared}
                return

            # The two directions the counters page switches between. Both are
            # the same table with the subject swapped, so they are built the
            # same way and the page only picks one.
            tables = {}
            you = guides.counter_table(champion_id, role, queue=ugg_queue,
                                       patch=patch)
            if stale():
                return
            if you:
                tables["you"] = you
            if opponent:
                them = guides.counter_table(opponent, role, mine=champion_id,
                                            queue=ugg_queue, patch=patch)
                if stale():
                    return
                if them:
                    tables["them"] = them
            with state.lock:
                state.guide = {**state.guide, "counterTables": tables}

            against = guides.against(champion_id, role, locked,
                                     queue=ugg_queue, patch=patch)
            if stale():
                return
            with state.lock:
                state.guide = {**state.guide, "against": against}
            memoise(session.shared_memo, shared_key,
                    {"counterTables": tables, "against": against},
                    SHARED_MEMO_MAX)
        except Unavailable as exc:
            if stale():
                return
            with state.lock:
                # Only a missing build empties the page. A missing counter
                # list leaves the build standing, which is most of the value.
                if (state.guide or {}).get("state") == "ready":
                    state.guide = {**state.guide, "countersError": str(exc)}
                else:
                    state.guide = {"state": "unavailable", "error": str(exc)}
            state.say(f"no build data: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("guide failed")
            if not stale():
                with state.lock:
                    state.guide = {"state": "error", "error": str(exc)}

    def start_guide(champion_id, role, opponent) -> None:
        session.guide_generation += 1
        generation = session.guide_generation
        with state.lock:
            state.guide = {"state": "loading", "opponentId": opponent}
        threading.Thread(target=refresh_guide, daemon=True,
                         args=(champion_id, role, opponent, generation)).start()

    def refresh_availability(champion_id: int) -> None:
        """Re-mark which skins and chromas have a local mod, after files land.

        Chromas are re-marked too: they are built alongside their skin, and a
        picker that never notices them arriving is a picker with no chromas.
        """
        available = library.available_for_champion(champion_id)
        with state.lock:
            if state.champion_id != champion_id:
                return
            for skin in state.skins:
                skin["available"] = skin["id"] in available
                if skin.get("chromas"):
                    have = library.available_chromas(champion_id, skin["id"])
                    for chroma in skin["chromas"]:
                        chroma["available"] = chroma["id"] in have
            state.skins.sort(key=lambda k: (not k["available"], k["id"]))

    def on_download_progress(champion_id: int, progress: dict) -> None:
        with state.lock:
            state.download = dict(progress, championId=champion_id)
        if progress["state"] == "downloading":
            # Cheap enough to re-mark on each completion, and it makes skins
            # light up as they arrive rather than all at the end.
            refresh_availability(champion_id)
        elif progress["state"] == "complete":
            refresh_availability(champion_id)
            got, miss = progress["fetched"], progress["missing"]
            if got or miss:
                state.say(f"{got} skins built from the install "
                          f"({miss} have no mod available)")

    downloads = downloader.ChampionDownloader(on_progress=on_download_progress)

    def get_lcu():
        return watcher.lcu

    def disarm(reason: str = "") -> None:
        """Stop the patcher, if one is running."""
        if inject.is_running():
            inject.stop_patcher()
        session.thread = None
        if reason:
            state.say(reason)

    def arm(skip_ask: bool = False) -> None:
        """Build the overlay and start the patcher, ready for the next game.

        Done at selection time, not at game start: runoverlay has its own loop
        that waits for the game and patches it when it is ready, so it needs to
        be running beforehand and then left alone.

        `skip_ask` is set when resuming after the one-time elevation choice, so
        the choice is not asked for again while acting on it.
        """
        skin_id = session.selected
        champ = session.champion_id
        if not skin_id or not champ:
            return
        if session.thread is not None and session.thread.is_alive():
            return

        # A chroma is a separate mod living inside its parent skin's folder;
        # picking one replaces the base skin rather than layering on it.
        chroma_id = session.chroma
        if chroma_id:
            mod = library.find_chroma_mod(champ, skin_id, chroma_id)
            label = f"chroma {chroma_id}"
        else:
            mod = library.find_mod(champ, skin_id)
            label = f"skin {skin_id}"
        if mod is None:
            state.say(f"no mod file for {label}")
            return

        # A patcher that has already hooked a running game keeps what it is
        # serving. Rebuilding the overlay under it is what corrupts a live
        # game, and re-arming is meaningless anyway: the game read its WADs
        # minutes ago.
        if inject.overlay_in_use():
            state.say("a patcher is already serving the running game -- "
                      "leaving it and its overlay alone")
            return

        # The first real injection with no passwordless helper: offer to set it
        # up once, instead of prompting for a password every single game. The
        # picker shows the choice; applying resumes through choose_elevation.
        # Asked only when injection is actually live (never in a dev instance)
        # and only until the user has answered once.
        from tibbers import privileged as priv
        if (inject.enabled and not skip_ask
                and prefs.get("elevation_choice") is None
                and not priv.available()):
            with state.lock:
                state.ask_elevation = True
            state.say("first skin -- set tibbers up so it won't ask for your "
                      "password every game?")
            return

        meta = {"championId": champ, "skinId": skin_id, "chromaId": chroma_id,
                "label": label}

        def work():
            result = inject.prepare(mod, progress=state.say, meta=meta)
            if not result.ok:
                state.say("failed: " + result.message)

        thread = threading.Thread(target=work, daemon=True)
        session.thread = thread
        thread.start()

    def on_select(skin_id, chroma_id=None):
        session.selected = skin_id
        session.chroma = chroma_id
        with state.lock:
            state.selected_skin_id = skin_id
            state.selected_chroma_id = chroma_id
        champ = session.champion_id
        if champ and prefs.get("remember_selections"):
            prefs.remember(champ, skin_id, chroma_id)
        if skin_id is None:
            disarm("selection cleared")
            return
        state.say(f"queued skin {skin_id}"
                  + (f" chroma {chroma_id}" if chroma_id else ""))
        # Prepare now rather than at game start: the patcher must already be
        # watching before the game launches.
        arm()

    def on_change(snapshot: dict) -> None:
        with state.lock:
            state.connected = snapshot["connected"]
            state.phase = snapshot["phase"]
            state.patcher = (inject.patcher_status()
                             if inject.is_running() else {})

        if not snapshot["connected"]:
            state.say("waiting for the League client...")
            with state.lock:
                state.champion_id = None
                state.champion_name = None
                state.skins = []
            return

        champ = snapshot["championId"]
        locked = snapshot.get("locked", False)

        # Track the previous champion here rather than inferring it from
        # `state`. The mock writes state before notifying, so comparing
        # against it always reports "unchanged" and nothing downstream fires.
        changed = champ != session.last_champion
        session.last_champion = champ
        # Read once and used throughout, including by the guide trigger below,
        # which had its own one-line helper for the same comparison.
        in_select = snapshot["phase"] == "ChampSelect"

        with state.lock:
            # Keep the last champion on screen once champ select ends, so the
            # grid does not vanish while the game is loading.
            if champ or snapshot["phase"] in (None, "None", "Lobby"):
                state.champion_id = champ
            state.locked = locked

        # Champ select context for the build guide. Enemies keep arriving
        # after you lock, so this is refreshed on every change rather than
        # captured once.
        # Resolved here rather than in the picker, which has no champion
        # dictionary and would otherwise show five blank squares.
        enemies = []
        for slot in snapshot.get("enemies") or []:
            champion_id = slot.get("championId")
            info = gamedata.champion(champion_id) if champion_id else None
            enemies.append({**slot,
                            "name": (info or {}).get("name", ""),
                            "icon": (info or {}).get("icon", "")})
        with state.lock:
            state.role = snapshot.get("role")
            state.enemies = enemies
            state.bans = snapshot.get("bans") or {}
            # An opponent chosen for a previous game means nothing now.
            if state.opponent_id is not None and not any(
                    e.get("championId") == state.opponent_id for e in enemies):
                state.opponent_id = None
            chosen = state.opponent_id
            picked_by_hand = session.opponent_by_hand
            champion = state.champion_id
            role = state.role

        # Nominate the enemy who most often meets this champion in this role,
        # unless one was chosen by hand. Enemies keep locking, so this is
        # revisited as they arrive rather than settled at the first.
        # Deliberately not `locked`: that name already holds whether *you*
        # have locked in, which is what decides the picker appearing. Reusing
        # it here meant the picker opened when an ENEMY locked -- close enough
        # in time on the Rift to look right, and never at all in Arena, where
        # the enemy list is always empty.
        locked_enemies = [e["championId"] for e in enemies if e.get("championId")]
        with state.lock:
            queue_block = state.queue or {}
            has_lanes = queue_block.get("roles", True)
            # The same file the rest of the page is drawn from. Asked without
            # it, the nomination came off ranked solo whatever the mode was,
            # so a Swiftplay lobby was handed the enemy who meets this
            # champion most often in a queue nobody in it is playing.
            ugg_queue = queue_block.get("uggQueue") or modes.UGG_RANKED
        if locked_enemies and champion and has_lanes and not picked_by_hand:
            suggestion = guides.suggest_opponent(champion, role, locked_enemies,
                                                 queue=ugg_queue,
                                                 patch=prefs.get("patch"))
            if suggestion and suggestion != chosen:
                with state.lock:
                    state.opponent_id = suggestion
                chosen = suggestion

        # The enemies are part of this too: two thirds of the guide is about
        # them, and an enemy locking without changing the nominated opponent
        # would otherwise leave the page describing a smaller team than the
        # one on screen.
        key = (champion, role, chosen, tuple(locked_enemies))
        with state.lock:
            has_source = bool((state.queue or {}).get("source"))
        if (champion and has_source and in_select
                and key != session.guide_key):
            session.guide_key = key
            start_guide(champion, role, chosen)

        # Tracked outside the guard below, so it is still right if the shell
        # was not built yet when champ select began.
        leaving_select = session.was_in_select and not in_select
        entering_select = in_select and not session.was_in_select
        session.was_in_select = in_select

        if entering_select:
            # In mock mode the client is only connected for art, so asking it
            # about the queue would answer for whatever real game is running.
            client = watcher.lcu if mock_client is None else None
            queue = client.queue() if client is not None else {}
            with state.lock:
                if queue:
                    state.queue = queue
                state.opponent_id = None
                state.guide = {}
                state.last_import = {}
                # Whatever is known now, however it got there: under the mock
                # the queue is set by the mock rather than fetched here, and
                # reading only the fetch left those modes with no verdict at
                # all -- which the page renders as still loading.
                queue = dict(state.queue or {})
            session.opponent_by_hand = False
            session.guide_key = None
            if queue and not queue.get("source"):
                # Said once, and shown on the page rather than left to spin:
                # a guide that never starts otherwise reads as one still
                # loading.
                with state.lock:
                    state.guide = {"state": "unsupported",
                                   "label": queue.get("label") or "This mode"}
                state.say(f"{queue.get('label') or 'this queue'} "
                          f"-- skins work, build data does not exist for it")

        # Import the build the moment you lock in, if that was asked for.
        # Outside the window guard below: this is worth doing headless, and
        # the picker being closed is no reason for the runes not to be there.
        if not in_select:
            session.auto_imported = None
        elif (locked and champ and prefs.get("auto_import")
                and session.auto_imported != champ):
            session.auto_imported = champ
            threading.Thread(target=auto_import, args=(champ,),
                             daemon=True).start()

        # Show the picker the moment a champion is locked, and take it away
        # when champ select ends. `was_locked` only guards the opening, so
        # that a lock reported on every poll opens the window once.
        # windows is None in headless and browser modes.
        if windows is not None:
            if in_select:
                if locked and not session.was_locked and prefs.get("auto_show"):
                    windows.open_picker(raise_it=True)
                session.was_locked = locked
            else:
                # Close whatever is open, not only what was opened here: the
                # app can start mid-champ-select and never see the lock, and
                # the picker can be opened from the menu bar. Keying the close
                # off the opening left it sitting over the whole match.
                if leaving_select and windows.picker_open():
                    if prefs.get("auto_hide"):
                        windows.close_picker()
                    else:
                        # The game has started: stop covering it, but leave
                        # the build on screen to tab back to.
                        windows.stand_down()
                session.was_locked = False

        if changed:
            # An import result belongs to the champion it was for. Left
            # standing, a needs-a-slot question about Shyvana would still be
            # on screen over Malphite's build, offering to replace a page for
            # a build nobody is looking at any more.
            with state.lock:
                state.last_import = {}

        if champ and changed:
            session.champion_id = champ
            client = watcher.lcu
            if client is not None:
                info = client.champion_info(champ)
                name = info["name"]
                skins = build_skin_list(client, champ)
                with state.lock:
                    state.champion_name = name
                    state.champion_title = info["title"]
                    state.champion_icon = info["icon"]
                    state.skins = skins
                    state.selected_skin_id = None
                session.selected = None
                have = sum(1 for k in skins if k["available"])
                state.say(f"{'locked' if locked else 'hovering'} {name} "
                          f"-- {have} skins ready")

                # Re-apply what was picked for this champion last time. The
                # chroma is keyed by skin, not champion, so switching skins
                # never carries the wrong one over.
                if prefs.get("remember_selections"):
                    remembered = prefs.skin_for(champ)
                    if remembered and any(k["id"] == remembered and k["available"]
                                          for k in skins):
                        chroma = prefs.chroma_for(remembered)
                        on_select(remembered, chroma)
                        state.say(f"restored {remembered}"
                                  + (f" chroma {chroma}" if chroma else ""))

                # Build this champion's mods now, while champ select is still
                # running. Hovering is the earliest signal available, and a
                # champion takes about a second, so they are ready well before
                # the game starts -- which is what removes the need to build
                # every champion up front.
                if prefs.get("download_on_hover") or locked:
                    downloads.ensure(champ, skins, info.get("alias"))

        # Report through the tooltip, at the end, once the champion's name has
        # actually been filled in above.
        if menubar is not None:
            with state.lock:
                name = state.champion_name
            if champ and name:
                menubar.set_status(f"{'locked' if locked else 'hovering'} {name}")
            else:
                menubar.set_status(str(snapshot["phase"] or "idle").lower()
                                   if snapshot["phase"] not in (None, "None")
                                   else "watching for champ select")

        # Back to the lobby: the game is over, so clear and release.
        if snapshot["phase"] in (None, "None", "Lobby"):
            disarm()
            downloads.cancel()
            session.selected = None
            session.champion_id = None
            with state.lock:
                state.skins = []
                state.champion_name = None
                state.selected_skin_id = None

    def load_champion(champion_id: int):
        """Champion data for the mock overlay, from the live client."""
        client = watcher.lcu or lcu.LCU.connect()
        if client is None:
            return None
        watcher.lcu = client
        info = client.champion_info(champion_id)
        skins = build_skin_list(client, champion_id)
        downloads.ensure(champion_id, skins, info.get("alias"))
        return {**info, "skins": skins}

    mock_client = None
    def list_champions():
        client = watcher.lcu or lcu.LCU.connect()
        if client is None:
            return [{"id": 202, "name": "Jhin"}]
        watcher.lcu = client
        data = client.get("/lol-game-data/assets/v1/champion-summary.json") or []
        # The summary also lists mode variants under their own ids (Jade_Ahri
        # is 60103), which are not pickable champions. Real champions occupy
        # the low id range; keep the lowest id per name.
        by_name: dict = {}
        for c in data:
            cid, name = c.get("id", 0), c.get("name")
            if not name or not (0 < cid < 10000):
                continue
            if name not in by_name or cid < by_name[name][0]:
                # The alias travels with the id because it is what names the
                # champion's archive on disk, and a rebuild has nowhere else
                # to get it.
                by_name[name] = (cid, c.get("alias") or "")
        return sorted(({"id": i, "name": n, "alias": a}
                       for n, (i, a) in by_name.items()),
                      key=lambda c: c["name"])

    if args.mock:
        from tibbers import mock as mock_mod

        def mock_applied() -> None:
            """Feed the mock's state back through the real change handler.

            Every field the handler reads has to be here: it writes the whole
            champ select context back from the snapshot, so anything missing
            is not merely ignored, it is cleared.
            """
            with state.lock:
                snap = {"connected": state.connected, "phase": state.phase,
                        "championId": state.champion_id, "locked": state.locked,
                        "role": state.role, "enemies": list(state.enemies),
                        "bans": dict(state.bans)}
            on_change(snap)

        mock_client = mock_mod.MockClient(state, load_champion, mock_applied,
                                          describe_champion=gamedata.champion)
        watcher.lcu = lcu.LCU.connect()   # for art only
        state.say(f"mock client at http://127.0.0.1:{args.port}/mock")
    elif args.demo:
        # Real skins, real art, real chroma data -- just not gated behind
        # being in champ select. Selecting still queues for a real game.
        client = lcu.LCU.connect()
        if client is None:
            print("League client must be running for --demo (it serves the art).")
            return 1
        # The art proxy reads watcher.lcu; demo mode never starts the watcher,
        # so hand it the client or every image 503s.
        watcher.lcu = client
        info = client.champion_info(args.demo)
        skins = build_skin_list(client, args.demo)
        session.champion_id = args.demo
        with state.lock:
            state.connected = True
            state.phase = "ChampSelect"
            state.locked = True
            state.champion_id = args.demo
            state.champion_name = info["name"]
            state.champion_title = info["title"]
            state.champion_icon = info["icon"]
            state.skins = skins
        have = sum(1 for k in skins if k["available"])
        state.say(f"demo: {info['name']} -- {have}/{len(skins)} skins ready")
        downloads.ensure(args.demo, skins, info.get("alias"))
    else:
        watcher.on_change = on_change
        watcher.start()

    #: Set to stop a rebuild early -- the app quitting, mostly. Held out
    #: here so the shutdown path can reach the thread `start_rebuild` made.
    rebuild_cancel = threading.Event()

    def on_rebuild_progress(progress: dict) -> None:
        with state.lock:
            state.rebuild = dict(progress)
        if progress["state"] == "rebuilding" and progress["champion"]:
            state.say(f"rebuilding {progress['champion']} -- "
                      f"{progress['champions']}/{progress['total_champions']} "
                      f"champions, {progress['made']} mods built")

    def start_rebuild() -> dict:
        """Rebuild every mod out of the install, for every champion.

        The champion and skin lists come from the client, so it has to be
        running -- the same source the picker uses, rather than a list baked
        in here that a new champion would age out.
        """
        if session.rebuild is not None and session.rebuild.is_alive():
            return {"ok": False, "message": "a rebuild is already running"}
        if not skinsmith.available():
            return {"ok": False,
                    "message": "no League install to build mods from"}
        client = watcher.lcu or lcu.LCU.connect()
        if client is None:
            return {"ok": False,
                    "message": "the League client has to be running -- the "
                               "champion and skin lists come from it"}
        watcher.lcu = client
        champions = list_champions()
        rebuild_cancel.clear()

        def work() -> None:
            summary = downloader.rebuild(champions, client.champion_skins,
                                         on_rebuild_progress, rebuild_cancel)
            # "Has this champion been prepared" was answered before the
            # library changed under it, so let every champion be looked at
            # again rather than trusting that.
            downloads.forget()
            state.say(f"rebuilt {summary['made']} mods across "
                      f"{summary['champions']} champions -- "
                      f"{summary['failed']} could not be built")
            with state.lock:
                champion = state.champion_id
            if champion:
                refresh_availability(champion)

        session.rebuild = threading.Thread(target=work, daemon=True)
        session.rebuild.start()
        state.say(f"rebuilding the library from the install "
                  f"-- {len(champions)} champions")
        return {"ok": True, "message": "rebuilding"}

    # The result of the one startup update check, shared with the settings
    # page. Filled by a background thread so a slow or unreachable GitHub never
    # holds up launch, and only when running from an installed bundle -- a
    # checkout updates itself with git.
    update_state: dict = {}

    def check_for_update() -> None:
        from tibbers import update
        if update.installed_app() is None:
            return
        result = update.check()
        update_state.clear()
        update_state.update(result)
        if result.get("available"):
            state.say(f"an update is available ({result['version']}) -- "
                      "see Settings")

    threading.Thread(target=check_for_update, daemon=True).start()

    def describe_settings() -> dict:
        from tibbers import privileged as priv
        try:
            patches = guides.ugg.patches()
        except Exception as exc:  # noqa: BLE001
            # The settings page shows an empty patch list either way, but
            # swallowing this made "u.gg is unreachable" and "there are no
            # patches" the same event, with nothing written down for either.
            log.warning("could not read u.gg's patch list: %s", exc)
            patches = []
        return {
            "patches": patches,
            "settings": prefs.settings(),
            "memory": prefs.stats(),
            "library": library.stats(),
            "helper": priv.available(),
            # Windows injects with no elevation, so the whole passwordless
            # section is macOS-only.
            "elevationSupported": IS_MACOS,
            "update": dict(update_state),
            "version": __import__("tibbers").__version__,
        }

    def change_setting(payload: dict) -> dict:
        from tibbers import privileged as priv
        action = payload.get("action")
        if action == "forget":
            prefs.forget_all()
            return {"ok": True}
        if action == "install":
            ok, message = priv.install(Path(__file__).parent / "tools")
            state.say(message)
            return {"ok": ok, "message": message}
        if action == "uninstall":
            ok, message = priv.uninstall()
            state.say(message)
            return {"ok": ok, "message": message}
        if action == "rebuild":
            return start_rebuild()
        if action == "update":
            from tibbers import update
            url = update_state.get("url")
            if not update_state.get("available") or not url:
                return {"ok": False, "error": "no update available"}

            def work() -> None:
                try:
                    state.say("downloading the update...")
                    update.apply(url)
                except Exception as exc:  # noqa: BLE001
                    state.say(f"update failed: {exc}")
                    return
                # The swap script is now waiting on this process; quitting lets
                # it replace the bundle and reopen the new one.
                state.say("installing -- tibbers will reopen in a moment")
                quit_app()

            threading.Thread(target=work, daemon=True).start()
            return {"ok": True, "updating": True}

        name, value = payload.get("name"), payload.get("value")
        # Not every setting is a switch: the patch is a string, and coercing
        # it to a bool would quietly store True.
        if name == "patch":
            value = str(value) if value else None
        else:
            value = bool(value)
        try:
            prefs.set(name, value)
        except KeyError:
            return {"ok": False, "error": f"unknown setting: {name}"}

        if name == "always_on_top" and windows is not None:
            windows.set_on_top(value)
        if name == "patch":
            # The guide is entirely patch-dependent, so it is refetched rather
            # than left showing figures from another one.
            with state.lock:
                champion, role, opponent = (state.champion_id, state.role,
                                            state.opponent_id)
            session.guide_key = None
            if champion:
                start_guide(champion, role, opponent)
        return {"ok": True, "settings": prefs.settings()}

    def choose_elevation(payload: dict) -> dict:
        """Answer the one-time "how should injection get permission" question.

        `auto` installs the passwordless helper (one prompt, once) and then
        applies the skin through it; `prompt` records that choice and applies
        with the ordinary per-game password prompt. Either way the pending
        selection is resumed with `skip_ask` so it is not asked for again.
        """
        from tibbers import privileged as priv
        choice = payload.get("choice")
        with state.lock:
            state.ask_elevation = False

        if choice == "prompt":
            prefs.set("elevation_choice", "prompt")
            state.say("okay -- tibbers will ask for your password each time")
            arm(skip_ask=True)
            return {"ok": True}

        if choice == "auto":
            def work() -> None:
                state.say("setting up -- approve the one prompt...")
                ok, message = priv.install(Path(__file__).parent / "tools")
                state.say(message)
                # Record the choice only on success: a cancelled or failed
                # install leaves it unanswered, so the offer comes back next
                # time rather than silently prompting forever.
                if ok:
                    prefs.set("elevation_choice", "auto")
                # Apply the skin regardless -- through the helper if it went in,
                # otherwise with the ordinary prompt for this one game.
                arm(skip_ask=True)
            threading.Thread(target=work, daemon=True).start()
            return {"ok": True, "installing": True}

        with state.lock:
            state.ask_elevation = True
        return {"ok": False, "error": "unknown choice"}

    def choose_opponent(payload: dict) -> dict:
        """Nominate an enemy as the lane opponent.

        Enemy roles are hidden during champ select, so this is the only way
        the guide can know which matchup to show.
        """
        raw = payload.get("championId")
        champion = int(raw) if raw not in (None, "", 0) else None
        with state.lock:
            known = {e.get("championId") for e in state.enemies}
            if champion is not None and champion not in known:
                return {"ok": False, "error": "not an enemy in this game"}
            state.opponent_id = champion
            state.guide = {"state": "loading", "opponentId": champion}
            current, role = state.champion_id, state.role
        session.opponent_by_hand = champion is not None
        session.guide_key = (current, role, champion)
        state.say(f"lane opponent: {champion}" if champion
                  else "lane opponent cleared")
        if current:
            start_guide(current, role, champion)
        return {"ok": True, "opponentId": champion}

    def window_action(payload: dict) -> dict:
        if windows is None:
            return {"ok": False, "error": "no window shell"}
        which = payload.get("open")
        if which == "picker":
            windows.open_picker(raise_it=True)
        elif which == "settings":
            windows.open_settings()
        elif payload.get("close") == "picker":
            windows.close_picker()
        return {"ok": True}

    # The file watch is a dev-only cost; the reload endpoint itself is on
    # every instance, because that is how deploy.sh --static refreshes the
    # windows of the running app without restarting it.
    reloader = server.Reloader(
        watch=Path(__file__).parent / "tibbers" / "static" if args.dev else None)
    reloader.watch_static()

    try:
        httpd = server.serve(state, get_lcu, on_select,
                             port=args.port or 7777,
                             mock=mock_client, list_champions=list_champions,
                             allow_fallback=args.port is None,
                             reloader=reloader,
                             hooks={"describe_settings": describe_settings,
                                    "change_setting": change_setting,
                                    "window_action": window_action,
                                    "choose_opponent": choose_opponent,
                                    "choose_elevation": choose_elevation,
                                    "import_build": import_build})
    except server.PortInUse as exc:
        print(f"Port {exc.port} is already in use -- most likely another "
              f"tibbers is still running.")
        print()
        print("  See what is holding it:")
        print(f"    lsof -nP -iTCP:{exc.port} -sTCP:LISTEN")
        print("  Stop it:")
        print("    pkill -f 'main.py'")
        print("  Or use a different port:")
        print(f"    main.py --port {exc.port + 1}")
        watcher.stop()
        return 1
    port = getattr(httpd, "chosen_port", args.port or 7777)
    url = f"http://127.0.0.1:{port}/"
    log.debug("serving on %s", url)

    def shutdown() -> None:
        """Stop everything this process owns -- but not, mid-game, the patcher.

        The patcher is started detached, so it is not taken down by this
        process exiting; it is taken down here, explicitly. Doing that while
        a game is running removes the skin the player is currently wearing,
        which is exactly what made restarting the app mid-game unusable. So
        with a game up it is left running, and the next start adopts it.
        """
        watcher.stop()
        rebuild_cancel.set()
        reloader.stop()
        # Window positions are written on a delay, so that dragging the
        # frameless picker does not rewrite the file sixty times a second.
        # This is where the last one gets out.
        prefs.flush()
        keep = args.keep_patcher or system.game_pid() is not None
        if keep and inject.is_running():
            state.say("leaving the patcher running -- "
                      "a game is in progress and it is holding the skin")
        else:
            inject.stop_patcher()
        httpd.shutdown()

    headless = args.no_ui or args.no_browser or args.no_window
    if headless:
        state.say("running without a window")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
        return 0

    if args.browser:
        webbrowser.open(url + "settings")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
        return 0

    # The native desktop shell -- pywebview windows on both platforms, with a
    # macOS menu-bar item or a Windows system-tray icon. If pywebview is not
    # installed, fall back to a browser tab below.
    try:
        import webview
    except ImportError:
        state.say("pywebview is not installed; opening in the browser")
        webbrowser.open(url + "settings")
        try:
            while True:
                time.sleep(1)
        finally:
            shutdown()
        return 0

    windows = shell.Windows(url, prefs=prefs)
    windows.set_on_top(prefs.get("always_on_top"))
    # The watcher has been running since before this existed, and it only
    # reports changes; without this, a launch during champ select misses the
    # lock-in entirely and the picker never appears.
    watcher.resync()

    def quit_app() -> None:
        # The windows veto their own close to keep the app alive in the menu
        # bar, so quitting has to lift that first or terminate_ never lands.
        windows.begin_quit()
        shutdown()
        # macOS lands this on the main queue; Windows runs it in place. Either
        # way it drops the background presence and destroys the windows, which
        # returns control from webview.start().
        shell.on_main(shell.terminate)

    bar = shell.MenuBar(on_settings=windows.open_settings,
                        on_picker=lambda: windows.open_picker(True),
                        on_quit=quit_app)
    menubar = bar

    # Built before the run loop starts, because pywebview needs a window to
    # start at all -- and hidden, because after the first launch this app is
    # only ever a menu bar item until a champion is locked.
    windows.prepare()

    # The passwordless setup is the one thing a friend has to decide, so it is
    # offered on launch rather than deferred to the first skin apply: whenever
    # injection is live, no helper is installed, and the choice has never been
    # made. The picker carries the card, and it takes the place of the
    # first-run settings window -- the card is the priority; settings stays one
    # click away in the menu bar. --quiet (a silent relaunch) offers nothing.
    from tibbers import privileged as _priv
    # macOS-only: Windows injects with no elevation, so there is nothing to set
    # up and no card to show.
    offer_elevation = (IS_MACOS and inject.enabled and not args.quiet
                       and prefs.get("elevation_choice") is None
                       and not _priv.available())
    if offer_elevation:
        with state.lock:
            state.ask_elevation = True

    # Settings are shown once, on the launch that has nothing configured yet.
    # After that the menu bar item is the way in; popping a window open on
    # every launch is noise for something that runs at login.
    # --quiet is a relaunch of an app that was already running: it opens
    # nothing that was not already open, whatever the first-run rule says.
    show_settings = ((prefs.first_run or args.settings)
                     and not args.quiet and not offer_elevation)
    opened_window = show_settings or offer_elevation
    if show_settings:
        windows.open_settings()
    if offer_elevation:
        windows.open_picker(raise_it=True)

    def after_start() -> None:
        # Settle the activation policy BEFORE creating the status item.
        # pywebview forces the app to Regular as it starts, overriding
        # LSUIElement, and changing the policy afterwards drops any status
        # item already installed -- which is why the menu bar item appeared
        # when launched from a terminal and vanished when launched from the
        # Dock, with the app insisting it had installed one.
        if args.overlay:
            windows.open_picker(raise_it=False)
        elif not opened_window:
            # No window to show: go straight to living in the menu bar.
            windows.go_background()
        if args.quiet:
            # Put back exactly what was on screen before the restart, in the
            # same place, without raising any of it.
            windows.restore()
        bar.install()

    if args.quiet:
        # Before webview.start: pywebview activates the app as its run loop
        # comes up, and a relaunch mid-game must not take the foreground.
        shell.quiet_launch()

    try:
        webview.start(after_start, gui=shell.gui_backend())
    finally:
        state.say("shutting down")
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
