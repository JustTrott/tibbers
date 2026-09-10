#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build statistics from u.gg.

u.gg's build pages are rendered from static JSON on a CDN, so this reads the
same files the site does rather than scraping a page. Numbers here reproduce
what u.gg shows, because they are the same numbers.

Only statistics come from here. Every name and icon comes from the local
client (see `gamedata`), so nothing about the guide depends on this being
reachable beyond the figures themselves.

Three things about the transport are load-bearing:

* The CDN sits behind bot protection that scores the TLS handshake, not just
  the headers. Measured in September 2026, `urllib` is refused every time; a
  curl on a home connection passes on the handshake alone, and where the
  system curl's handshake scores badly, or there is no system curl at all
  (Windows, on both counts), the app supplies one that presents a browser's,
  via `system.browser_curl`, run with the flags it came with -- a browser
  speaks HTTP/2, and pinning HTTP/1.1 on it undoes the disguise. curl goes
  first, the header sets are a last resort, and because the challenge is
  not deterministic a challenged curl is retried a few times before giving
  up.
* A 403 from u.gg is ambiguous. A missing file and a bot block both return
  it, and they are told apart by the body: XML `AccessDenied` means the
  patch, champion or version is wrong; an HTML challenge means refused.
* A fetched file is trusted for `CACHE_SECONDS` (u.gg advertises four hours
  and regenerates roughly daily, so the window is longer to meet the CDN less
  often), then revalidated with its stored ETag -- a conditional request that
  comes back 304 with no body when nothing changed.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import system

log = logging.getLogger("tibbers.ugg")

VERSIONS_URL = ("https://static.bigbrain.gg/assets/lol/riot_patch_update/"
                "prod/ugg/ugg-api-versions.json")
BASE = "https://stats2.u.gg/lol/1.5"

#: ARAM Mayhem's augment rankings, which are not on the build CDN at all.
#: The mode has no build file -- ``overview/<patch>/aram_mayhem/<champion>``
#: is AccessDenied on every patch tried, which is why the build is borrowed
#: from ARAM -- but its augment tier lists are plain static JSON here, keyed
#: by the same patch string, and served without the challenge `BASE` puts up.
MAYHEM_BASE = "https://static.bigbrain.gg/custom-aram-mayhem"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: The CDN scores clients on their headers as well as their TLS handshake, and
#: the scoring is not stable: measured over repeated requests, a bare
#: user-agent and a browser-like set both passed every time, while adding
#: "Accept-Encoding: gzip, deflate" was refused one time in three. Sending no
#: user-agent at all was refused every time. So the sets below are tried in
#: turn, and a refusal moves to the next rather than failing the fetch.
HEADER_SETS = (
    {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"},
    {"User-Agent": UA},
    {"User-Agent": UA, "Accept-Encoding": "gzip"},
)

#: The CDN's challenge is not deterministic: a client it accepts is still
#: refused a fraction of the time (measured at roughly one request in three
#: with some header sets, and seen even mid-sync from an accepted machine).
#: So a curl that comes back 403-with-a-challenge is retried a few times with
#: a short backoff before the fetch gives up on it and tries the header sets.
CURL_ATTEMPTS = 3
CURL_BACKOFF = (0.0, 0.7, 1.8)

#: u.gg's own page defaults: every region pooled, Emerald and above.
WORLD, EMERALD_PLUS, OVERALL = 12, 17, 8

#: The client's lane names, in u.gg's numbering.
ROLE_IDS = {"jungle": 1, "utility": 2, "bottom": 3, "top": 4, "middle": 5}

#: Below this many games a cell is noise -- every region/tier/role key exists
#: even when it holds a single match at 100% win rate.
MIN_MATCHES = 100

#: How long a fetched file is trusted before it is revalidated. u.gg advertises
#: four hours and regenerates a file roughly daily, so a longer window costs a
#: few hours of freshness on build statistics and buys far fewer requests --
#: which is fewer chances to meet the CDN's intermittent challenge. Revalidation
#: is conditional (the stored ETag), so an unchanged file costs a 304 and no
#: body. Override with TIBBERS_UGG_CACHE_HOURS.
def _cache_hours() -> float:
    try:
        return max(0.5, float(os.environ.get("TIBBERS_UGG_CACHE_HOURS", "8")))
    except ValueError:
        return 8.0


CACHE_SECONDS = int(_cache_hours() * 60 * 60)

#: How many fetched payloads to hold in memory. Every one of them is also on
#: disk, so an eviction costs a file read rather than a request -- and an
#: overview file is hundreds of kilobytes, which an unbounded cache kept for
#: every champion looked at for as long as the app was open.
MEMORY_MAX = 32

#: How many decoded matchup tables to hold. One champ select asks for the same
#: table three times over -- the counters page, the how-am-I-doing row and the
#: lane-opponent nomination -- and decoding it walks every opponent in the
#: file. Enough for both directions of a couple of champ selects.
TABLE_MEMO_MAX = 8


class Unavailable(Exception):
    """u.gg could not be reached, or has nothing for this champion and role."""


def _curl_argv() -> List[str]:
    """The curl to run, as an argv prefix, transport flags included.

    A machine on a home connection clears the CDN on the TLS handshake alone,
    and the system curl does on macOS -- over HTTP/1.1, pinned because that
    is what was measured to pass. Where the system handshake is refused
    (Windows, whose handshake the CDN scores worst, and where most machines
    have no system curl at all) the app supplies a curl that presents a
    browser's handshake (see `system.browser_curl`), preferred when present
    and run exactly as the platform hands it over: a browser speaks HTTP/2,
    and forcing HTTP/1.1 onto an impersonated handshake makes the fingerprint
    inconsistent, which the CDN refuses every time (Windows, September 2026:
    0 of 5 with ``--http1.1``, 10 of 10 without). `TIBBERS_CURL` overrides
    both, as given, for measuring one client against another.
    """
    override = os.environ.get("TIBBERS_CURL")
    if override:
        return shlex.split(override)
    supplied = system.browser_curl()
    if supplied is not None:
        return list(supplied)
    return ["curl", "--http1.1"]


def _curl(url: str,
          etag: Optional[str] = None) -> Optional[Tuple[int, bytes, Optional[str]]]:
    """Fetch through curl: (status, body, response ETag), or None when curl
    itself could not run or connect.

    curl is the client most often accepted by the CDN's fingerprinting. The
    status and the ETag are written after the body by ``-w`` on their own
    lines, so a 403 body can still be read (a missing file and a bot challenge
    share that status) and a 200's ETag is captured for the next conditional
    request. With *etag*, the request is conditional and a 304 comes back with
    an empty body.
    """
    cmd = _curl_argv() + ["-sS", "--compressed", "-A", UA, "--max-time", "20",
                          "-w", "\n%header{etag}\n%{http_code}"]
    if etag:
        cmd += ["-H", f"If-None-Match: {etag}"]
    cmd.append(url)
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=25,
                              creationflags=system.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("curl failed to run: %s", exc)
        return None
    if done.returncode != 0:
        log.debug("curl exit %s: %s", done.returncode,
                  done.stderr.decode("utf-8", "replace").strip()[:200])
        return None
    # The last two lines are the ETag and the status; everything before is the
    # body, which may itself contain newlines.
    rest, _, status = done.stdout.rpartition(b"\n")
    body, _, tag = rest.rpartition(b"\n")
    try:
        code = int(status)
    except ValueError:
        return None
    return code, body, tag.decode("ascii", "replace").strip() or None


def _decode(response) -> bytes:
    raw = response.read()
    encoding = (response.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


class UGG:
    """Fetches and decodes u.gg's build data."""

    def __init__(self):
        self._lock = threading.Lock()
        self._versions: Optional[dict] = None
        self._versions_at = 0.0
        self._memory: "OrderedDict[str, Any]" = OrderedDict()
        self._tables: "OrderedDict[tuple, List[dict]]" = OrderedDict()

    # -- transport ---------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        safe = key.replace("/", "_")
        return system.data_dir() / "ugg" / f"{safe}.json"

    def _remember(self, key: str, entry: dict) -> None:
        with self._lock:
            self._memory[key] = entry
            self._memory.move_to_end(key)
            while len(self._memory) > MEMORY_MAX:
                self._memory.popitem(last=False)

    @staticmethod
    def _fresh(entry: Optional[dict], ttl: int) -> bool:
        """Whether a cached entry can be served without asking anyone."""
        if not entry:
            return False
        return time.time() - entry.get("at", 0) < ttl

    def _get(self, url: str, key: str, ttl: int = CACHE_SECONDS) -> Any:
        with self._lock:
            hit = self._memory.get(key)
            if hit is not None:
                self._memory.move_to_end(key)
        if self._fresh(hit, ttl):
            return hit["data"]

        path = self._cache_path(key)
        stored = None
        try:
            stored = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass

        if self._fresh(stored, ttl):
            self._remember(key, stored)
            return stored["data"]

        try:
            data, etag, unchanged = self._from_ugg(url, key, stored)
        except Unavailable:
            raise
        except Exception as exc:  # noqa: BLE001 -- every transport failed
            if stored:
                log.debug("serving stale u.gg data for %s: %s", key, exc)
                return stored["data"]
            refused = isinstance(exc, urllib.error.HTTPError) and exc.code == 403
            raise Unavailable("u.gg refused the request (403)" if refused
                              else str(exc)) from exc
        if unchanged and stored:
            stored["at"] = time.time()
            self._store(path, key, stored)
            return stored["data"]
        entry = {"at": time.time(), "etag": etag, "data": data}
        self._store(path, key, entry)
        return data

    # -- u.gg itself -------------------------------------------------------

    def _from_ugg(self, url: str, key: str,
                  stored: Optional[dict]) -> Tuple[Any, Optional[str], bool]:
        """(data, etag, unchanged) straight from u.gg's CDN.

        curl goes first: measured against the CDN, urllib is refused every
        time and the right curl passes. Its challenge is not deterministic,
        so a challenged curl is retried a few times before the header sets
        are tried as a last resort. A held copy makes the request conditional,
        so an unchanged file comes back 304 with no body. Raises Unavailable
        for a file that does not exist, and the last transport error when
        nothing got through.
        """
        etag = stored.get("etag") if stored else None
        for attempt in range(CURL_ATTEMPTS):
            if attempt:
                time.sleep(CURL_BACKOFF[min(attempt, len(CURL_BACKOFF) - 1)])
            fetched = _curl(url, etag)
            if fetched is None:
                break                       # curl absent or could not connect
            status, body, fresh_etag = fetched
            if status == 304 and stored:
                return None, etag, True
            if status == 200:
                try:
                    return json.loads(body.decode("utf-8")), fresh_etag, False
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.debug("u.gg answered %s with something not JSON", key)
                    break
            if status == 403 and b"AccessDenied" in body[:400]:
                raise Unavailable(f"u.gg has no data at {url}")
            log.debug("curl got %s from u.gg for %s (attempt %d)",
                      status, key, attempt + 1)

        etag_header = {"If-None-Match": etag} if etag else {}
        last: Exception = RuntimeError("u.gg refused the request")
        for attempt, extra in enumerate(HEADER_SETS):
            request = urllib.request.Request(url, headers={**extra, **etag_header})
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    body = _decode(response)
                    return (json.loads(body.decode("utf-8")),
                            response.headers.get("ETag"), False)
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and stored:
                    return None, stored.get("etag"), True
                if exc.code == 403:
                    # A missing file and a bot block share this status; only
                    # the body separates them, and only one is worth retrying.
                    detail = b""
                    try:
                        detail = exc.read()[:400]
                    except Exception:  # noqa: BLE001
                        pass
                    if b"AccessDenied" in detail:
                        raise Unavailable(f"u.gg has no data at {url}") from exc
                    last = exc
                    log.debug("u.gg challenged header set %d for %s", attempt, key)
                    continue
                last = exc
                break
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                last = exc
                break
        log.info("u.gg unreachable for %s: %s", key, last)
        raise last

    def _store(self, path: Path, key: str, entry: dict) -> None:
        self._remember(key, entry)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.part")
            tmp.write_text(json.dumps(entry))
            tmp.replace(path)
        except OSError as exc:
            log.debug("could not cache %s: %s", key, exc)

    # -- versions ----------------------------------------------------------

    def manifest(self) -> dict:
        """Every patch u.gg publishes, with each endpoint's file version.

        Nothing here can be hardcoded: the patch moves fortnightly and each
        endpoint's version moves independently of it.
        """
        if self._versions and time.time() - self._versions_at < 3600:
            return self._versions
        data = self._get(VERSIONS_URL, "versions", ttl=3600)
        if not isinstance(data, dict) or not data:
            raise Unavailable("could not read u.gg's version manifest")
        self._versions = data
        self._versions_at = time.time()
        return data

    @staticmethod
    def _order(patch: str):
        try:
            return [int(part) for part in patch.split("_")]
        except (AttributeError, ValueError):
            return [0]

    def patches(self, limit: int = 8) -> List[str]:
        """The most recent patches, newest first."""
        return sorted(self.manifest(), key=self._order, reverse=True)[:limit]

    def resolve_patch(self, patch: Optional[str] = None) -> str:
        """The patch to read, defaulting to the newest published."""
        manifest = self.manifest()
        if patch and patch in manifest:
            return patch
        return sorted(manifest, key=self._order)[-1]

    def versions(self) -> dict:
        patch = self.resolve_patch()
        return {"patch": patch, "endpoints": self.manifest()[patch]}

    def _url(self, endpoint: str, tail: str, queue: str,
             patch: Optional[str] = None) -> str:
        chosen = self.resolve_patch(patch)
        version = (self.manifest().get(chosen) or {}).get(endpoint) or "1.5.0"
        return f"{BASE}/{endpoint}/{chosen}/{queue}/{tail}/{version}.json"

    # -- selection ---------------------------------------------------------

    @staticmethod
    def _overview_matches(data: list) -> int:
        """Games behind an overview cell: its overall block is [wins, matches]."""
        try:
            return int(data[6][1])
        except (IndexError, TypeError, ValueError):
            return 0

    @staticmethod
    def _rankings_matches(data: list) -> int:
        """Games behind a rankings cell, which starts [wins, matches, ...]."""
        try:
            return int(data[1])
        except (IndexError, TypeError, ValueError):
            return 0

    @staticmethod
    def _cell(payload: dict, role_id: Optional[int],
              matches_of=None, tiers=(EMERALD_PLUS, OVERALL)) -> Optional[list]:
        """Pick the best-populated cell for a role, widening rather than lying.

        Every region/tier/role combination exists in the file even when it
        holds a handful of games, so a narrow selection can be a 100% win rate
        over one match. Emerald+ worldwide is tried first, then the whole
        ranked population, and a role is only accepted if it has enough games
        behind it.
        """
        candidates = []
        for tier in tiers:
            region = (payload.get(str(WORLD)) or payload.get(WORLD) or {})
            block = region.get(str(tier)) or region.get(tier) or {}
            if role_id is not None:
                roles = [role_id]
            else:
                roles = sorted(int(r) for r in block)
            for role in roles:
                entry = block.get(str(role)) or block.get(role)
                if not entry:
                    continue
                # Overview cells are wrapped as [data, lastUpdated]; rankings
                # cells are the data array itself. The wrapper is recognised
                # by its first element being a list, because unwrapping a
                # rankings cell yields its win count and reads as valid.
                data = (entry[0] if isinstance(entry, list) and entry
                        and isinstance(entry[0], list) else entry)
                probe = matches_of or UGG._overview_matches
                candidates.append((probe(data), data))

        if not candidates:
            return None
        best = max(candidates, key=lambda c: c[0])
        strong = [c for c in candidates if c[0] >= MIN_MATCHES]
        return (strong[0][1] if strong else best[1])

    # -- decoding ----------------------------------------------------------
    #
    # u.gg's payloads are positional arrays, and the position of the match
    # count is not consistent between them. Four orderings appear:
    #
    #     most blocks       [matches, wins, ...]
    #     a general overall [wins, matches]            <- reversed
    #     a matchup overall [losses, matches]          <- reversed AND negated
    #     item options      [itemId, wins, matches]    <- and again
    #
    # Guessing wrong does not fail, it reports a wrong win rate, so each is
    # read through a named helper rather than indexed inline.
    #
    # The matchup case was measured, not guessed. In a matchup overview
    # `data[6]` is byte-for-byte the same pair as this pairing's row in the
    # `matchups` file, which is `[championId, losses, matches]` -- checked
    # across every cached pairing, all of them exact. So the first element
    # counts the games this champion LOST, and reading it as wins reported
    # Jinx into Caitlyn at 46.75% when the true figure is 53.25%.

    @staticmethod
    def _rate(matches: int, wins: int) -> float:
        return round(wins / matches * 100, 2) if matches else 0.0

    @classmethod
    def _lead(cls, block: Optional[list]) -> dict:
        """A block that begins [matches, wins, ...]."""
        if not block:
            return {"matches": 0, "winRate": 0.0}
        matches, wins = int(block[0]), int(block[1])
        return {"matches": matches, "winRate": cls._rate(matches, wins)}

    @classmethod
    def _option(cls, entry: Optional[list]) -> Optional[dict]:
        """An item option, which begins [itemId, wins, matches]."""
        if not entry or len(entry) < 3:
            return None
        item_id, wins, matches = int(entry[0]), int(entry[1]), int(entry[2])
        return {"itemId": item_id, "matches": matches,
                "winRate": cls._rate(matches, wins)}

    @classmethod
    def _overall(cls, block: Optional[list], matchup: bool) -> Optional[dict]:
        """The overall cell, whose first element flips meaning by file.

        A general overview counts wins there; a matchup overview counts
        losses. Both are `[n, matches]`, which is why reading one as the
        other is silent rather than an error.
        """
        if not block or len(block) < 2:
            return None
        first, matches = int(block[0]), int(block[1])
        wins = matches - first if matchup else first
        return {"matches": matches, "winRate": cls._rate(matches, wins)}

    def decode(self, data: list, trees: Dict[int, int],
               matchup: bool = False) -> dict:
        """Turn one positional cell into something with names on it.

        `trees` maps a perk id to the tree it belongs to, which is the only
        way to split the six runes: they arrive in one flat list, not grouped.

        `matchup` says which kind of overview this cell came from, because
        `data[6]` counts wins in one and losses in the other.
        """
        runes_block = data[0] if len(data) > 0 else None
        out: dict = {}

        overall = self._overall(data[6] if len(data) > 6 else None, matchup)
        if overall:
            out["overall"] = overall

        if runes_block:
            primary_tree, secondary_tree = int(runes_block[2]), int(runes_block[3])
            picked = [int(p) for p in (runes_block[4] or [])]
            primary = [p for p in picked if trees.get(p) == primary_tree]
            secondary = [p for p in picked if trees.get(p) == secondary_tree]
            out["runes"] = {
                **self._lead(runes_block),
                "primaryTree": primary_tree,
                "secondaryTree": secondary_tree,
                "keystone": primary[0] if primary else None,
                "primary": primary[1:],
                "secondary": secondary,
            }

        spells = data[1] if len(data) > 1 else None
        if spells:
            out["spells"] = {**self._lead(spells),
                             "ids": [int(s) for s in (spells[2] or [])]}

        start = data[2] if len(data) > 2 else None
        if start:
            out["start"] = {**self._lead(start),
                            "items": [int(i) for i in (start[2] or [])]}

        core = data[3] if len(data) > 3 else None
        if core:
            out["core"] = {**self._lead(core),
                           "items": [int(i) for i in (core[2] or [])]}

        skills = data[4] if len(data) > 4 else None
        if skills:
            out["skills"] = {**self._lead(skills),
                             "order": [str(s).upper() for s in (skills[2] or [])],
                             "priority": str(skills[3] or "")}

        options = data[5] if len(data) > 5 else None
        if options:
            # options[3] is consumables, not a slot: wards and potions would
            # otherwise appear as a fourth item recommendation.
            for name, index in (("fourth", 0), ("fifth", 1), ("sixth", 2)):
                picks = [self._option(e) for e in (options[index] or [])]
                out[name] = [p for p in picks if p]

        shards = data[8] if len(data) > 8 else None
        if shards:
            out["shards"] = {**self._lead(shards),
                             "ids": [int(s) for s in (shards[2] or [])]}

        return out

    # -- public ------------------------------------------------------------

    def build(self, champion_id: int, role: Optional[str],
              opponent_id: Optional[int] = None,
              queue: str = "ranked_solo_5x5",
              trees: Optional[Dict[int, int]] = None,
              patch: Optional[str] = None) -> dict:
        """The recommended build, optionally for a specific matchup."""
        role_id = ROLE_IDS.get((role or "").lower())
        chosen = self.resolve_patch(patch)
        if opponent_id:
            url = self._url("overview", f"matchups/{int(champion_id)}_{int(opponent_id)}",
                            queue, chosen)
            key = f"matchup-{chosen}-{champion_id}-{opponent_id}-{queue}"
        else:
            url = self._url("overview", str(int(champion_id)), queue, chosen)
            key = f"overview-{chosen}-{champion_id}-{queue}"

        payload = self._get(url, key)
        cell = self._cell(payload, role_id)
        if cell is None:
            raise Unavailable("no build for this champion and role")
        out = self.decode(cell, trees or {}, matchup=bool(opponent_id))
        out["role"] = role
        out["matchup"] = opponent_id
        out["patch"] = chosen
        # A specific matchup can rest on a few dozen games -- especially when
        # the nominated opponent does not really play this lane -- and a win
        # rate over 34 games says nothing. Flagged rather than hidden, so the
        # picker can show the number and let it be judged.
        out["matches"] = (out.get("overall") or {}).get("matches", 0)
        out["thin"] = out["matches"] < MIN_MATCHES
        return out

    def build_with_fallback(self, champion_id: int, role: Optional[str],
                            opponent_id: Optional[int] = None,
                            queue: str = "ranked_solo_5x5",
                            trees: Optional[Dict[int, int]] = None,
                            patch: Optional[str] = None) -> dict:
        """The matchup build where it is worth having, the general one otherwise.

        Returns the general build alongside a thin matchup rather than in
        place of it: which to trust is a judgement, and the numbers for both
        are what make it.
        """
        general = self.build(champion_id, role, None, queue, trees, patch)
        if not opponent_id:
            return general
        try:
            matchup = self.build(champion_id, role, opponent_id, queue, trees, patch)
        except Unavailable as exc:
            general["matchupError"] = str(exc)
            return general
        matchup["general"] = {"matches": general["matches"],
                              "winRate": (general.get("overall") or {}).get("winRate")}
        return matchup

    def matchup_table(self, champion_id: int, role: Optional[str],
                      queue: str = "ranked_solo_5x5",
                      tiers=(EMERALD_PLUS, OVERALL),
                      patch: Optional[str] = None) -> List[dict]:
        """Every opponent this champion meets in this role, with lane diffs.

        One fetch answers two questions: which opponents are hardest, and how
        many games back each -- the second is what makes it possible to guess
        which of the enemy team is actually in your lane.

        The decoded rows are memoised. One refresh of a champ select asks for
        the same table three times -- the counters page, the row against the
        team actually in the game, and the lane-opponent nomination -- and
        each one re-walked every opponent in the file to rebuild an identical
        answer. The rows are read, never modified, by all three.
        """
        role_id = ROLE_IDS.get((role or "").lower())
        chosen = self.resolve_patch(patch)
        memo_key = (int(champion_id), role_id, queue, chosen, tuple(tiers))
        with self._lock:
            memoised = self._tables.get(memo_key)
            if memoised is not None:
                self._tables.move_to_end(memo_key)
        if memoised is not None:
            return memoised

        payload = self._get(self._url("matchups", str(int(champion_id)), queue, chosen),
                            f"matchups-{chosen}-{champion_id}-{queue}")
        cell = self._cell(payload, role_id, lambda d: sum(
            int(r[2]) for r in d if isinstance(r, list) and len(r) > 2), tiers)
        if not cell:
            self._memoise_table(memo_key, [])
            return []

        total = sum(int(r[2]) for r in cell if isinstance(r, list) and len(r) > 2)
        rows = []
        for row in cell:
            if not isinstance(row, list) or len(row) < 3:
                continue
            enemy, losses, matches = int(row[0]), int(row[1]), int(row[2])
            if not matches:
                continue
            entry = {"championId": enemy, "matches": matches,
                     "winRate": round((1 - losses / matches) * 100, 2),
                     "share": matches / total if total else 0.0}
            # The lane diffs are stored negated, per opponent per game.
            for name, index in (("goldAt15", 4), ("csAt15", 6), ("xpAt15", 3)):
                if len(row) > index:
                    try:
                        entry[name] = round(-(float(row[index]) / matches), 1)
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
            rows.append(entry)
        self._memoise_table(memo_key, rows)
        return rows

    def _memoise_table(self, key: tuple, rows: List[dict]) -> None:
        with self._lock:
            self._tables[key] = rows
            self._tables.move_to_end(key)
            while len(self._tables) > TABLE_MEMO_MAX:
                self._tables.popitem(last=False)

    def opponent_samples(self, champion_id: int, role: Optional[str],
                         queue: str = "ranked_solo_5x5",
                         patch: Optional[str] = None) -> Dict[int, int]:
        """How many games back each possible opponent.

        Read from the same cell every other figure on the page is: Emerald+
        worldwide, widening to the whole ranked population only when `_cell`
        finds that cell too thin to mean anything. Deliberately the same
        population, not a broader one -- this decides which enemy the entire
        guide is then written about, and ranking opponents on one population
        while reporting on another would make the page disagree with itself.

        When a patch is new enough that these counts cannot separate five
        enemies, the answer is the patch setting: pinning the previous patch
        buys the evidence back openly, where quietly reaching for a different
        rank would not.
        """
        return {r["championId"]: r["matches"]
                for r in self.matchup_table(champion_id, role, queue, patch=patch)}

    # -- ARAM Mayhem -------------------------------------------------------

    def _mayhem(self, tail: str, key: str, holds: str,
                patch: Optional[str] = None) -> dict:
        """One Mayhem file, from the newest patch that actually has it.

        The fallback is one patch deep and openly reported: the patch that
        answered comes back in the payload and is printed under the table, so
        a ranking read from last patch says so rather than passing as this
        one's. Beyond one step the data is old enough that no data is the
        more honest answer.
        """
        wanted = self.resolve_patch(patch)
        tried = [wanted]
        if not patch:
            recent = self.patches(2)
            if len(recent) > 1 and recent[0] == wanted:
                tried.append(recent[1])

        why = "not published"
        for chosen in tried:
            url = f"{MAYHEM_BASE}/{chosen}/{tail.format(patch=chosen)}"
            try:
                data = self._get(url, f"{key}-{chosen}")
            except Unavailable as exc:
                why = str(exc)
                continue
            if isinstance(data, dict) and data:
                return {"patch": chosen, **data}
            why = "the file was empty"
        raise Unavailable(f"u.gg has no Mayhem {holds} for "
                          f"{' or '.join(tried)}: {why}")

    def mayhem_augments(self, champion_id: int,
                        patch: Optional[str] = None) -> dict:
        """How u.gg ranks every augment for one champion in ARAM Mayhem.

        Letters, not numbers: u.gg publishes the bands and keeps the figures
        behind them to itself, so this is passed on as the ranking it is
        rather than dressed up with rates it does not carry.
        """
        out = self._mayhem(
            f"tierlist-per-champion-augments-{{patch}}/"
            f"tierlist-augments-{int(champion_id)}-{{patch}}.json",
            f"mayhem-augments-{int(champion_id)}", "augment ranking", patch)
        tiers = out.get("tiers")
        if not isinstance(tiers, dict) or not tiers:
            raise Unavailable(
                f"u.gg ranks no Mayhem augments for champion {champion_id}")
        return {"patch": out["patch"], "tiers": tiers,
                "lastUpdated": out.get("lastUpdated")}

    def mayhem_augment_pool(self, patch: Optional[str] = None) -> dict:
        """Every augment the mode offers, ranked across all champions.

        The champion file ranks only what it has enough games for, which is
        roughly two thirds of the pool. This is what fills the rest in: an
        augment you were just offered and cannot find is the one failure the
        page exists to prevent.
        """
        out = self._mayhem(f"tierlist-augments-by-rarity-{{patch}}.json",
                           "mayhem-augment-pool", "augment pool", patch)
        rarities = out.get("rarities")
        if not isinstance(rarities, dict) or not rarities:
            raise Unavailable("u.gg published no Mayhem augment pool")
        return {"patch": out["patch"], "rarities": rarities}
