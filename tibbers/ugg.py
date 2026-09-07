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
  the headers. Measured in September 2026, `urllib` is refused every time
  and curl passes from macOS but not reliably from Windows. So the files
  are read first from a mirror (`MIRROR`, see mirror/ in the repository), a
  plain static host serving the same paths, and from u.gg itself only for
  what the mirror does not carry -- matchup pairs, old patches -- or when
  it is down. Against u.gg, curl goes first and the header sets after.
* A 403 from u.gg is ambiguous. A missing file and a bot block both return
  it, and they are told apart by the body: XML `AccessDenied` means the
  patch, champion or version is wrong; an HTML challenge means refused.
* Cache lifetimes follow the source. A file from the mirror expires just
  after the mirror's next scheduled refresh (its `status.json` says when),
  one from u.gg after the four hours the CDN advertises; either is then
  revalidated with its ETag, which costs nothing when nothing changed.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import OrderedDict
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, system

log = logging.getLogger("tibbers.ugg")

VERSIONS_URL = ("https://static.bigbrain.gg/assets/lol/riot_patch_update/"
                "prod/ugg/ugg-api-versions.json")
BASE = "https://stats2.u.gg/lol/1.5"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: What the mirror sees. It is ours, so the app says who it is.
CLIENT_UA = f"tibbers/{__version__}"

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

#: u.gg's own page defaults: every region pooled, Emerald and above.
WORLD, EMERALD_PLUS, OVERALL = 12, 17, 8

#: The client's lane names, in u.gg's numbering.
ROLE_IDS = {"jungle": 1, "utility": 2, "bottom": 3, "top": 4, "middle": 5}

#: Below this many games a cell is noise -- every region/tier/role key exists
#: even when it holds a single match at 100% win rate.
MIN_MATCHES = 100

CACHE_SECONDS = 4 * 60 * 60

#: Where the files are read from first: a mirror that fetched them once, from
#: a host whose handshake the CDN accepts, and serves them under the same
#: paths (see mirror/ in the repository). The CDN refuses urllib outright and
#: a machine's curl only when its TLS fingerprint happens to score badly --
#: Windows' usually does -- and those users saw a bare 403 in the guide. The
#: mirror is a plain static host, so no fingerprinting stands in the way.
#: Set the variable empty to read u.gg directly, as before.
MIRROR = os.environ.get("TIBBERS_UGG_MIRROR", "https://data.tibbers.lol").rstrip("/")

#: The mirror's status document says when it next refreshes. Cached files
#: expire that long after it, plus up to as much again of jitter, so every
#: client does not revalidate in the same minute -- and the status itself is
#: re-read no more often than this.
STATUS_SECONDS = 10 * 60
STATUS_SLACK = 10 * 60

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


def _curl(url: str) -> Optional[Tuple[int, bytes]]:
    """Fetch through curl: the status and the body, or None when curl itself
    could not run or connect.

    curl is the client most often accepted by the CDN's fingerprinting; the
    status comes back separately so a 403 body can still be read, because
    a missing file and a bot challenge share that status.
    """
    try:
        from . import system
        done = subprocess.run(
            ["curl", "-sS", "--http1.1", "--compressed", "-A", UA,
             "--max-time", "20", "-w", "\n%{http_code}", url],
            capture_output=True, timeout=25,
            creationflags=system.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("curl failed to run: %s", exc)
        return None
    if done.returncode != 0:
        log.debug("curl exit %s: %s", done.returncode,
                  done.stderr.decode("utf-8", "replace").strip()[:200])
        return None
    body, _, status = done.stdout.rpartition(b"\n")
    try:
        return int(status), body
    except ValueError:
        return None


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
        self._mirror_failing = False
        self._status_next: Optional[float] = None
        self._status_at = 0.0

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
        """Whether a cached entry can be served without asking anyone.

        An entry from the mirror carries the expiry the mirror's schedule
        gave it; one from u.gg lives for the CDN's own cache lifetime.
        """
        if not entry:
            return False
        expires = entry.get("expires")
        if expires is not None:
            return time.time() < expires
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

        mirror_url = self._mirror_url(url)
        if mirror_url:
            outcome = self._from_mirror(mirror_url, stored)
            if outcome is not None:
                data, etag, unchanged = outcome
                if unchanged and stored:
                    stored["at"] = time.time()
                    stored["expires"] = self._mirror_expiry()
                    self._store(path, key, stored)
                    return stored["data"]
                entry = {"at": time.time(), "etag": etag, "data": data,
                         "source": "mirror", "expires": self._mirror_expiry()}
                self._store(path, key, entry)
                return data

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
            stored.pop("expires", None)
            self._store(path, key, stored)
            return stored["data"]
        entry = {"at": time.time(), "etag": etag, "data": data, "source": "ugg"}
        self._store(path, key, entry)
        return data

    # -- the mirror --------------------------------------------------------

    @staticmethod
    def _mirror_url(url: str) -> Optional[str]:
        """The mirror's copy of a u.gg URL, or None when it keeps none.

        The mirror serves u.gg's paths verbatim under its own host, and the
        manifest as /manifest.json.
        """
        if not MIRROR:
            return None
        if url == VERSIONS_URL:
            return f"{MIRROR}/manifest.json"
        if url.startswith(BASE + "/"):
            return f"{MIRROR}/lol/1.5/{url[len(BASE) + 1:]}"
        return None

    def _from_mirror(self, url: str,
                     stored: Optional[dict]) -> Optional[Tuple[Any, Optional[str], bool]]:
        """(data, etag, unchanged) from the mirror, or None to try u.gg.

        A 404 means the mirror does not carry this file -- matchup pairs and
        old patches -- and anything else that goes wrong means the mirror is
        down; both fall through to u.gg. Only an ETag the mirror itself gave
        out is sent back to it.
        """
        headers = {"Accept-Encoding": "gzip", "User-Agent": CLIENT_UA}
        if stored and stored.get("source") == "mirror" and stored.get("etag"):
            headers["If-None-Match"] = stored["etag"]
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(_decode(response).decode("utf-8"))
                self._mirror_ok()
                return data, response.headers.get("ETag"), False
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and stored:
                self._mirror_ok()
                return None, stored.get("etag"), True
            if exc.code == 404:
                log.debug("mirror has no %s", url)
                return None
            self._mirror_down(f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._mirror_down(str(exc))
        return None

    def _mirror_ok(self) -> None:
        if self._mirror_failing:
            log.info("u.gg mirror is reachable again")
            self._mirror_failing = False

    def _mirror_down(self, why: str) -> None:
        if not self._mirror_failing:
            log.info("u.gg mirror unreachable (%s); reading u.gg directly", why)
            self._mirror_failing = True
        else:
            log.debug("u.gg mirror still unreachable: %s", why)

    def _mirror_expiry(self) -> float:
        """When a file fetched from the mirror now should be checked again.

        Just after the mirror's next refresh, with jitter so clients spread
        out; the CDN's own lifetime when the schedule cannot be read.
        """
        now = time.time()
        with self._lock:
            next_run = self._status_next
            stale = now - self._status_at > STATUS_SECONDS
        if stale:
            next_run = self._read_status()
            with self._lock:
                self._status_next, self._status_at = next_run, now
        if next_run is None:
            return now + CACHE_SECONDS
        return max(next_run, now) + STATUS_SLACK + random.uniform(0, STATUS_SLACK)

    @staticmethod
    def _read_status() -> Optional[float]:
        """The mirror's next refresh as a timestamp, or None."""
        request = urllib.request.Request(
            f"{MIRROR}/status.json", headers={"User-Agent": CLIENT_UA})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = json.loads(_decode(response).decode("utf-8"))
            stamp = status["nextRunAt"].replace("Z", "+00:00")
            return datetime.fromisoformat(stamp).timestamp()
        except (urllib.error.URLError, OSError, ValueError, KeyError,
                TypeError, AttributeError) as exc:
            log.debug("mirror status unreadable: %s", exc)
            return None

    # -- u.gg itself -------------------------------------------------------

    def _from_ugg(self, url: str, key: str,
                  stored: Optional[dict]) -> Tuple[Any, Optional[str], bool]:
        """(data, etag, unchanged) straight from u.gg's CDN.

        curl goes first: measured against the CDN, urllib is refused every
        time and curl passes from most machines. The header sets follow for
        the machines where it does not, with a conditional request when a
        copy is held. Raises Unavailable for a file that does not exist,
        and the last transport error when nothing got through.
        """
        fetched = _curl(url)
        if fetched is not None:
            status, body = fetched
            if status == 200:
                try:
                    return json.loads(body.decode("utf-8")), None, False
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.debug("u.gg answered %s with something not JSON", key)
            elif status == 403 and b"AccessDenied" in body[:400]:
                raise Unavailable(f"u.gg has no data at {url}")
            else:
                log.debug("curl got %s from u.gg for %s", status, key)

        etag_header = {"If-None-Match": stored["etag"]} if (
            stored and stored.get("source", "ugg") == "ugg"
            and stored.get("etag")) else {}
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
