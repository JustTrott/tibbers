#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mirror u.gg's build files, so tibbers reads them from a host it controls.

u.gg's stats live in an S3 bucket behind Cloudflare bot protection, and that
protection refuses most non-browser clients outright -- Python's urllib is
refused every time, and whether a machine's curl passes depends on the TLS
handshake it happens to produce (Apple's curl passes; Windows' usually does
not). Users on the wrong side of that see "403 Forbidden" in the build and
counters pages. This script does the fetching once, from one machine whose
handshake is known to pass, and publishes the files as static content under
the *same paths* u.gg uses, so the app only has to swap its base URL.

Sync (`sync`):

* Reads u.gg's version manifest, mirrors the newest patches (two by default)
  for every queue tibbers reads, for every champion u.gg lists that patch.
* Fetches with curl and a conditional request; an unchanged file costs a
  304 and no bytes. u.gg sends gzip and its files are stored as received,
  never decompressed, so disk and bandwidth are what the wire carried.
* Tells a missing file (S3's XML AccessDenied) from a bot challenge (an HTML
  page): the first removes any stale copy, the second is retried and then
  left as it was. A file is only published once it gunzips to valid JSON.
* Writes `manifest.json` last, so a client never learns of a patch whose
  files are not on disk yet, and `status.json` with the next run time, so
  clients can expire their caches just after it rather than on a guess.

Serve (`serve`): a development stand-in for the Caddy site the real host
runs (see Caddyfile beside this file), which rewrites a request for
`x.json` to the `x.json.gz` on disk and sends it as gzip.

Where it runs matters: u.gg's bot protection also scores the client's
address, and a datacenter range is refused whatever the client (measured on
the Hetzner VM: the very same curl that passes from a home connection is
refused from there). So the sync runs on a machine u.gg accepts and pushes
the tree to the serving host (push.sh), unless the host itself is accepted
(the systemd units).

Standard library only, plus curl.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("ugg-mirror")

MANIFEST_URL = ("https://static.bigbrain.gg/assets/lol/riot_patch_update/"
                "prod/ugg/ugg-api-versions.json")
BASE = "https://stats2.u.gg"
PREFIX = "/lol/1.5"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: The queues tibbers reads (see tibbers/modes.py). A queue u.gg does not
#: publish for a patch simply yields no files.
QUEUES = ("ranked_solo_5x5", "ranked_flex_sr", "normal_draft_5x5",
          "normal_blind_5x5", "swiftplay", "normal_aram")

#: The per-champion files tibbers reads. Matchup *pair* builds
#: (overview/.../matchups/A_B/) are 170x170 per queue and are not mirrored;
#: the app falls back to u.gg for those and to the general build after that.
ENDPOINTS = ("overview", "matchups")

RETRY_DELAYS = (3, 8, 20)


# -- transport -------------------------------------------------------------

class Fetch:
    """One curl response: status, a few headers, and the body on disk."""

    def __init__(self, status: int, headers: Dict[str, str], body: Path):
        self.status, self.headers, self.body = status, headers, body

    def text(self, limit: int = 600) -> bytes:
        try:
            return self.body.read_bytes()[:limit]
        except OSError:
            return b""


def curl(url: str, into: Path, etag: Optional[str] = None,
         timeout: int = 90) -> Optional[Fetch]:
    """GET through curl, asking for gzip and keeping the body as sent."""
    headers_path = into.with_suffix(into.suffix + ".h")
    cmd = ["curl", "-sS", "--http1.1", "-A", UA,
           "-H", "Accept-Encoding: gzip", "--max-time", str(timeout),
           "-D", str(headers_path), "-o", str(into), "-w", "%{http_code}", url]
    if etag:
        cmd[1:1] = ["-H", f"If-None-Match: {etag}"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 15)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("curl failed for %s: %s", url, exc)
        return None
    if done.returncode != 0:
        log.warning("curl exit %s for %s: %s", done.returncode, url,
                    done.stderr.strip()[:200])
        return None
    headers: Dict[str, str] = {}
    try:
        for line in headers_path.read_text(errors="replace").splitlines():
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
    except OSError:
        pass
    finally:
        headers_path.unlink(missing_ok=True)
    try:
        status = int(done.stdout.strip() or 0)
    except ValueError:
        status = 0
    return Fetch(status, headers, into)


def classify_403(fetch: Fetch) -> str:
    """'missing' for S3's AccessDenied, 'challenge' for Cloudflare's page."""
    head = fetch.text()
    if head[:2] == b"\x1f\x8b":
        try:
            head = gzip.decompress(fetch.body.read_bytes())[:600]
        except (OSError, EOFError, ValueError):
            head = b""
    return "missing" if b"AccessDenied" in head else "challenge"


def valid_gzip_json(path: Path) -> bool:
    try:
        json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        return True
    except (OSError, EOFError, ValueError, UnicodeDecodeError):
        return False


# -- the sync ----------------------------------------------------------------

class Mirror:

    def __init__(self, out: Path, state_path: Path, interval_hours: float,
                 queues: Iterable[str], patches: int, concurrency: int,
                 champion_limit: Optional[int] = None):
        self.out = out
        self.state_path = state_path
        self.interval = timedelta(hours=interval_hours)
        self.queues = tuple(queues)
        self.patch_count = patches
        self.concurrency = concurrency
        self.champion_limit = champion_limit
        self.state: Dict[str, dict] = {}
        self.lock = threading.Lock()
        self.counts = {"fetched": 0, "unchanged": 0, "missing": 0,
                       "failed": 0, "bytes": 0}
        self.tmp = out / ".tmp"

    # -- state ---------------------------------------------------------

    def load_state(self) -> None:
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            self.state = {}

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, sort_keys=True))
        tmp.replace(self.state_path)

    # -- helpers -------------------------------------------------------

    def _scratch(self, tag: str) -> Path:
        self.tmp.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=tag + "-", dir=self.tmp)
        os.close(fd)
        return Path(name)

    def fetch_json(self, url: str) -> Optional[dict]:
        """A small JSON document, decoded, or None."""
        scratch = self._scratch("json")
        try:
            for attempt, delay in enumerate((0,) + RETRY_DELAYS):
                if delay:
                    time.sleep(delay)
                fetch = curl(url, scratch)
                if fetch is None:
                    continue
                if fetch.status == 200:
                    raw = scratch.read_bytes()
                    if raw[:2] == b"\x1f\x8b":
                        raw = gzip.decompress(raw)
                    return json.loads(raw.decode("utf-8"))
                if fetch.status == 403 and classify_403(fetch) == "missing":
                    return None
                log.warning("%s -> %s (attempt %d)", url, fetch.status, attempt)
        except (OSError, EOFError, ValueError) as exc:
            log.warning("%s unreadable: %s", url, exc)
        finally:
            scratch.unlink(missing_ok=True)
        return None

    @staticmethod
    def order(patch: str) -> List[int]:
        try:
            return [int(part) for part in patch.split("_")]
        except ValueError:
            return [0]

    # -- one file ------------------------------------------------------

    def sync_file(self, rel: str) -> None:
        """Bring one file under `rel` (e.g. lol/1.5/overview/16_17/...) up to date."""
        url = f"{BASE}/{rel}"
        target = self.out / (rel + ".gz")
        known = self.state.get(rel) or {}
        etag = known.get("etag") if target.exists() else None
        scratch = self._scratch("file")
        try:
            for attempt, delay in enumerate((0,) + RETRY_DELAYS):
                if delay:
                    time.sleep(delay)
                fetch = curl(url, scratch, etag)
                if fetch is None:
                    continue
                if fetch.status == 304:
                    with self.lock:
                        self.counts["unchanged"] += 1
                        known["seen"] = time.time()
                        self.state[rel] = known
                    return
                if fetch.status == 200:
                    if fetch.headers.get("content-encoding", "").lower() != "gzip":
                        # Never seen, but the disk format is gzip, so make it so.
                        scratch.write_bytes(gzip.compress(scratch.read_bytes()))
                    if not valid_gzip_json(scratch):
                        log.warning("%s: 200 but not JSON, keeping the old copy", rel)
                        break
                    target.parent.mkdir(parents=True, exist_ok=True)
                    size = scratch.stat().st_size
                    scratch.replace(target)
                    with self.lock:
                        self.counts["fetched"] += 1
                        self.counts["bytes"] += size
                        self.state[rel] = {"etag": fetch.headers.get("etag"),
                                           "modified": fetch.headers.get("last-modified"),
                                           "seen": time.time(), "size": size}
                    return
                if fetch.status == 403 and classify_403(fetch) == "missing":
                    target.unlink(missing_ok=True)
                    with self.lock:
                        self.counts["missing"] += 1
                        self.state.pop(rel, None)
                    return
                log.info("%s -> %s, attempt %d", rel, fetch.status, attempt + 1)
            with self.lock:
                self.counts["failed"] += 1
        finally:
            scratch.unlink(missing_ok=True)

    # -- the run -------------------------------------------------------

    def run(self) -> int:
        started = datetime.now(timezone.utc)
        self.out.mkdir(parents=True, exist_ok=True)
        self.load_state()

        manifest = self.fetch_json(MANIFEST_URL)
        if not isinstance(manifest, dict) or not manifest:
            log.error("could not read u.gg's manifest; nothing changed")
            return 1
        patches = sorted(manifest, key=self.order, reverse=True)[:self.patch_count]
        log.info("patches %s, queues %s", patches, ", ".join(self.queues))

        wanted: List[str] = []
        champions_by_patch: Dict[str, List[str]] = {}
        for patch in patches:
            version = (manifest.get(patch) or {}).get("primary_roles") or "1.5.0"
            roles = self.fetch_json(f"{BASE}{PREFIX}/primary_roles/{patch}/{version}.json")
            champions = sorted(roles, key=int) if isinstance(roles, dict) else []
            if not champions:
                # A brand-new patch may not list champions yet; borrow the last
                # patch's list so the files appear as soon as u.gg has them.
                champions = sorted({rel.split("/")[5] for rel in self.state
                                    if rel.count("/") >= 6}, key=int)
                log.warning("no champion list for %s; using %d known ids",
                            patch, len(champions))
            if self.champion_limit:
                champions = champions[:self.champion_limit]
            champions_by_patch[patch] = champions
            for queue in self.queues:
                for endpoint in ENDPOINTS:
                    version = (manifest.get(patch) or {}).get(endpoint) or "1.5.0"
                    for champion in champions:
                        wanted.append(f"{PREFIX.lstrip('/')}/{endpoint}/{patch}/"
                                      f"{queue}/{champion}/{version}.json")
        log.info("%d files to check", len(wanted))

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            list(pool.map(self.sync_file, wanted))

        self.prune(set(wanted))
        self.save_state()

        finished = datetime.now(timezone.utc)
        # Manifest last: a client that reads it now finds every file it names.
        self.publish("manifest.json", manifest)
        self.publish("status.json", {
            "generatedAt": finished.isoformat(timespec="seconds"),
            "startedAt": started.isoformat(timespec="seconds"),
            "durationSeconds": round((finished - started).total_seconds(), 1),
            "intervalSeconds": int(self.interval.total_seconds()),
            "nextRunAt": self.next_run(started).isoformat(timespec="seconds"),
            "patches": patches,
            "queues": list(self.queues),
            "endpoints": list(ENDPOINTS),
            "champions": {p: len(c) for p, c in champions_by_patch.items()},
            "files": sum(1 for rel in self.state),
            "counts": self.counts,
        })
        log.info("done in %.0fs: %d fetched (%d bytes), %d unchanged, "
                 "%d missing, %d failed", (finished - started).total_seconds(),
                 self.counts["fetched"], self.counts["bytes"],
                 self.counts["unchanged"], self.counts["missing"],
                 self.counts["failed"])
        return 0 if self.counts["failed"] == 0 else 2

    def next_run(self, started: datetime) -> datetime:
        """When the scheduler is expected to run this again.

        One interval after this run started. Clients expire just after it;
        if the scheduler is late (a laptop asleep, a timer drifting), they
        revalidate a few times cheaply until the run arrives.
        """
        return started + self.interval

    def publish(self, name: str, document: dict) -> None:
        tmp = self.out / (name + ".tmp")
        tmp.write_text(json.dumps(document, separators=(",", ":")))
        tmp.replace(self.out / name)

    def prune(self, wanted: set) -> None:
        """Drop files for patches that fell out of the window."""
        root = self.out / PREFIX.lstrip("/")
        if not root.exists():
            return
        removed = 0
        for path in root.rglob("*.json.gz"):
            rel = str(path.relative_to(self.out))[:-3]
            if rel not in wanted:
                path.unlink(missing_ok=True)
                self.state.pop(rel, None)
                removed += 1
        for directory in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        if removed:
            log.info("pruned %d files outside the patch window", removed)


# -- a stand-in for nginx ------------------------------------------------------

class GzipStaticHandler(SimpleHTTPRequestHandler):
    """Serve `x.json` from `x.json.gz` with Content-Encoding: gzip, as the
    Caddy site does, plus an ETag so conditional requests work."""

    def log_message(self, fmt, *args):  # quieter than the default
        log.debug("%s " + fmt, self.address_string(), *args)

    def send_head(self):
        path = Path(self.translate_path(self.path))
        gz = path.with_suffix(path.suffix + ".gz")
        if path.suffix == ".json" and gz.is_file():
            stat = gz.stat()
            etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return None
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            return open(gz, "rb")
        return super().send_head()


def serve(directory: Path, port: int) -> None:
    handler = lambda *a, **k: GzipStaticHandler(*a, directory=str(directory), **k)  # noqa: E731
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    log.info("serving %s on http://127.0.0.1:%d/", directory, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# -- cli -----------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="bring the mirror up to date")
    s.add_argument("--out", type=Path, required=True, help="web root to publish into")
    s.add_argument("--state", type=Path, help="ETag bookkeeping (default: <out>/../state.json)")
    s.add_argument("--interval-hours", type=float, default=4,
                   help="how often the timer runs; advertised to clients")
    s.add_argument("--queues", nargs="+", default=list(QUEUES))
    s.add_argument("--patches", type=int, default=2, help="newest patches to keep")
    s.add_argument("--concurrency", type=int, default=4)
    s.add_argument("--champions", type=int, help="only the first N champions (testing)")
    s.add_argument("-v", "--verbose", action="store_true")

    v = sub.add_parser("serve", help="serve a mirror directory like nginx would")
    v.add_argument("directory", type=Path)
    v.add_argument("--port", type=int, default=7790)
    v.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)

    if args.command == "serve":
        serve(args.directory, args.port)
        return 0

    state = args.state or (args.out.parent / "state.json")
    args.out.mkdir(parents=True, exist_ok=True)
    lock_path = args.out.parent / ".sync.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error("another sync is running")
            return 3
        mirror = Mirror(args.out, state, args.interval_hours, args.queues,
                        args.patches, args.concurrency, args.champions)
        return mirror.run()


if __name__ == "__main__":
    sys.exit(main())
