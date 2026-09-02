#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local web UI.

Serves a picker on 127.0.0.1 that shows the champion you locked and the skins
available for it, and proxies splash art from the LCU so the browser can
display images it has no credentials for.

Bound to loopback only. There is no authentication because there is no remote
surface: anything that could reach this port can already read the lockfile.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("tibbers.server")

STATIC = Path(__file__).parent / "static"

#: The pages, under every path each answers on.
PAGES = {"/": "index.html", "/index.html": "index.html",
         "/picker": "index.html", "/settings": "settings.html"}

#: The mock client is a page in its own window rather than an overlay
#: injected into the picker: a developer tool drawn on top of the interface
#: being judged covers the thing it is there to help you look at. Kept apart
#: from PAGES so only an instance running --mock can reach it.
MOCK_PAGES = {"/mock": "mock.html"}

#: Hosts /api/browse will hand to the system browser. The picker window is
#: not a browser, so "open this page where the numbers came from" has to go
#: through here -- and an endpoint that opened arbitrary URLs would let any
#: local process put any page on the user's screen with this app's name on
#: the request.
BROWSE_HOSTS = {"u.gg", "op.gg", "www.op.gg"}

# Art is immutable for a given path and every tile is fetched on each champion
# change, so proxying it uncached means re-pulling megabytes from the client
# for images the browser already showed once. Bounded so a long session cannot
# grow without limit.
_ART_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_ART_CACHE_MAX = 400
_ART_LOCK = threading.Lock()


def _art_cached(key: str):
    with _ART_LOCK:
        hit = _ART_CACHE.get(key)
        if hit is not None:
            _ART_CACHE.move_to_end(key)
        return hit


def _art_remember(key: str, blob: bytes, ctype: str) -> None:
    """Hold art in memory, newest last, within the bound."""
    with _ART_LOCK:
        _ART_CACHE[key] = (blob, ctype)
        _ART_CACHE.move_to_end(key)
        while len(_ART_CACHE) > _ART_CACHE_MAX:
            _ART_CACHE.popitem(last=False)


def _art_path_ok(asset: str) -> bool:
    """Whether an asset path may be relayed to the client as it stands.

    It is pasted onto `/lol-game-data/assets/` and sent to the LCU, so
    anything that could climb out of that prefix is refused rather than
    forwarded. The percent-decoded form is checked too: `urlparse` does not
    decode, so `%2e%2e` would otherwise pass here and be decoded at the far
    end. Loopback and read-only either way -- this is about not relaying a
    request the picker would never make.
    """
    if not asset:
        return False
    for form in (asset, unquote(asset)):
        form = form.replace("\\", "/")
        if form.startswith("/"):
            return False
        if ".." in form.split("/"):
            return False
    return True


def _art_ctype(name: str) -> str:
    return "image/jpeg" if name.lower().endswith((".jpg", ".jpeg")) \
        else "image/png"


def _art_disk_path(key: str) -> Path:
    """Stable on-disk name for a cached asset."""
    from . import system
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    ext = ".jpg" if _art_ctype(key) == "image/jpeg" else ".png"
    return system.data_dir() / "artcache" / (digest + ext)


def _art_from_disk(key: str):
    """Second-level cache, so art survives a restart and a closed client.

    The League client serves this art, so without it nothing renders. Keeping
    a copy means the mock overlay still works with League shut down.

    A hit is promoted into memory. Without that, every splash that had fallen
    out of the memory cache -- or every one at all, after a restart -- was read
    off the disk again on each redraw, which is megabytes per champion change
    for bytes already in hand.
    """
    path = _art_disk_path(key)
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    ctype = "image/jpeg" if path.suffix == ".jpg" else "image/png"
    _art_remember(key, blob, ctype)
    return blob, ctype


def _art_store(key: str, blob: bytes, ctype: str) -> None:
    _art_remember(key, blob, ctype)
    try:
        path = _art_disk_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(blob)
        tmp.replace(path)
    except OSError as exc:
        log.debug("could not cache art %s: %s", key, exc)


class Reloader:
    """Tells open pages to reload themselves.

    Two callers: `POST /api/reload`, which deploy.sh --static uses to refresh
    the windows of a running instance without restarting the process, and --
    in a dev instance only -- a watch on tibbers/static that fires when a file
    there changes.

    Pages find out by holding one request open (`GET /api/reload?since=N`)
    that answers when the token moves or after a timeout, so an idle page
    makes one request every half minute rather than polling. Nothing is sent
    on a connection that is never opened, which is what keeps this free in
    production: an instance nobody has a window on does no work at all.
    """

    #: How long a listener is parked before it is answered anyway. Short
    #: enough that WKWebView's own request timeout is never in play.
    WAIT = 25.0

    def __init__(self, watch: Optional[Path] = None):
        self.token = 0
        self.reason = "started"
        self._cond = threading.Condition()
        self._watch = Path(watch) if watch else None
        self._stop = threading.Event()

    def bump(self, reason: str = "reload") -> int:
        with self._cond:
            self.token += 1
            self.reason = reason
            self._cond.notify_all()
            log.info("reload requested (%s) -> token %d", reason, self.token)
            return self.token

    def wait(self, since: Optional[int]) -> dict:
        with self._cond:
            if since is not None and since == self.token:
                self._cond.wait(self.WAIT)
            return {"token": self.token, "reason": self.reason,
                    "watching": self._watch is not None}

    def stop(self) -> None:
        self._stop.set()
        # Wake the parked listeners too. Without this, shutdown left every
        # open page's request sitting on the condition for up to WAIT seconds.
        with self._cond:
            self._cond.notify_all()

    # -- the dev-only file watch -------------------------------------------

    def _fingerprint(self) -> tuple:
        out = []
        for path in sorted(self._watch.rglob("*")):
            try:
                if path.is_file():
                    stat = path.stat()
                    out.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return tuple(out)

    def watch_static(self, interval: float = 0.5) -> None:
        """Fire whenever a file under the watched directory changes.

        mtime polling rather than FSEvents: it is four files, the interval is
        half a second, and a dependency-free watch that works the same when
        the app is run from the bundle is worth more than the elegance.
        """
        if self._watch is None:
            return

        def run() -> None:
            last = self._fingerprint()
            while not self._stop.wait(interval):
                try:
                    now = self._fingerprint()
                except Exception:  # noqa: BLE001
                    continue
                if now == last:
                    continue
                changed = [Path(p).name for p, *_ in set(now) - set(last)]
                last = now
                self.bump("changed: " + (", ".join(sorted(changed)) or "static"))

        threading.Thread(target=run, daemon=True, name="static-watch").start()
        log.info("watching %s for changes", self._watch)


class State:
    """Shared state between the poller, the injector and the browser."""

    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.phase: Optional[str] = None
        self.champion_id: Optional[int] = None
        self.champion_name: Optional[str] = None
        self.champion_title: str = ""
        self.champion_icon: str = ""
        self.skins: list = []
        self.selected_skin_id: Optional[int] = None
        self.selected_chroma_id: Optional[int] = None
        self.status = "starting"
        self.locked = False
        self.download: dict = {}
        # A library-wide rebuild, while one runs. Separate from `download`,
        # which is about the champion in front of you: a rebuild is started
        # from settings and outlives whatever champ select is doing.
        self.rebuild: dict = {}
        self.patcher: dict = {}
        # True when the app is waiting for the user's one-time answer to how
        # injection should get permission (set the helper up, or prompt each
        # time). The picker shows a card while this holds, and applying the
        # skin resumes once they choose.
        self.ask_elevation = False
        # First-run provisioning of the Windows injection tools, while it runs:
        # {"active": bool, "message": str, "percent": int|None, "error": str}.
        # The picker shows a progress bar so the download does not look hung.
        self.setup: dict = {}
        # The one-time "installed, and I live in the tray" welcome, shown once
        # on the first launch: {"show": bool, "home": "system tray"|"menu bar"}.
        self.welcome: dict = {}
        self.log_lines: list = []
        # Champ select, for the build guide. `role` is what the client
        # assigned; `opponent` is the enemy the user nominated as their lane,
        # which cannot be worked out automatically -- enemy roles are hidden.
        self.role: Optional[str] = None
        self.enemies: list = []
        self.opponent_id: Optional[int] = None
        self.bans: dict = {}
        self.queue: dict = {}
        self.guide: dict = {}
        # What the last import wrote, or why it did not. Kept in state rather
        # than only returned to the caller because auto-import has no caller:
        # it fires on lock-in and the picker has to be able to show what
        # happened, including a needs-a-slot question nobody clicked for.
        self.last_import: dict = {}

    def snapshot(self) -> dict:
        """What `/api/state` answers with.

        `skins` and `enemies` are copied rather than handed over. The caller
        serialises this *after* the lock is released, and the grid is re-sorted
        in place by `refresh_availability` on every mod that lands -- and
        CPython empties a list for the duration of its own sort, so a poll that
        landed in that window serialised a half-empty champion.
        """
        with self.lock:
            return {
                "connected": self.connected,
                "phase": self.phase,
                "championId": self.champion_id,
                "championName": self.champion_name,
                "championTitle": self.champion_title,
                "championIcon": self.champion_icon,
                "skins": list(self.skins),
                "selectedSkinId": self.selected_skin_id,
                "selectedChromaId": self.selected_chroma_id,
                "locked": self.locked,
                "download": self.download,
                "rebuild": self.rebuild,
                "patcher": self.patcher,
                "askElevation": self.ask_elevation,
                "setup": self.setup,
                "welcome": self.welcome,
                "status": self.status,
                "log": self.log_lines[-40:],
                "role": self.role,
                "enemies": list(self.enemies),
                "opponentId": self.opponent_id,
                "bans": self.bans,
                "queue": self.queue,
                "guide": self.guide,
                "import": self.last_import,
            }

    def say(self, message: str) -> None:
        with self.lock:
            self.status = message
            self.log_lines.append(message)
            del self.log_lines[:-200]
        log.info(message)


def make_handler(state: State, get_lcu, on_select, mock=None,
                 list_champions=None, hooks=None, reloader=None):
    hooks = hooks or {}
    reloader = reloader or Reloader()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter than the default
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_cacheable(self, code: int, body: bytes, ctype: str) -> None:
            """Art only. Lets the browser skip the request entirely on redraw."""
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400, immutable")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self):
            path = urlparse(self.path).path

            page = PAGES.get(path)
            if page is None and mock is not None:
                page = MOCK_PAGES.get(path)
            if page is not None:
                try:
                    body = (STATIC / page).read_bytes()
                except OSError:
                    self._send(500, b"UI missing", "text/plain")
                    return
                # Injected rather than baked into each page, and on every
                # instance: the live app needs it too, because that is how
                # `deploy.sh --static` refreshes windows already open.
                body = body.replace(
                    b"</body>", b'<script src="/reload.js"></script></body>')
                self._send(200, body, "text/html; charset=utf-8")
                return

            if path == "/reload.js":
                try:
                    self._send(200, (STATIC / "reload.js").read_bytes(),
                               "application/javascript; charset=utf-8")
                except OSError:
                    self._send(404, b"", "application/javascript")
                return

            if path == "/api/reload":
                # Parked until the token moves, so an open page costs one
                # request every WAIT seconds rather than a poll loop.
                raw = parse_qs(urlparse(self.path).query).get("since", [None])[0]
                since = int(raw) if raw and raw.isdigit() else None
                self._json(reloader.wait(since))
                return

            if (path == "/api/mock/champions" and mock is not None
                    and list_champions is not None):
                self._json(list_champions())
                return

            if path.startswith("/fonts/"):
                name = Path(path).name
                # Only serve files that are actually in the fonts directory;
                # never let a request assemble its own path.
                font = STATIC / "fonts" / name
                if font.parent != (STATIC / "fonts") or not font.is_file():
                    self._send(404, b"", "font/ttf")
                    return
                self._send(200, font.read_bytes(), "font/ttf")
                return

            if path == "/api/state":
                self._json(state.snapshot())
                return

            if path == "/api/settings":
                describe = hooks.get("describe_settings")
                self._json(describe() if describe else {})
                return

            # Proxy skin art. The LCU needs an auth header the browser has no
            # way to supply, so requests are relayed here.
            if path.startswith("/api/art/"):
                asset = path[len("/api/art/"):]
                if not _art_path_ok(asset):
                    self._send(400, b"bad asset path", "text/plain")
                    return

                hit = _art_cached(asset) or _art_from_disk(asset)
                if hit is not None:
                    self._send_cacheable(200, hit[0], hit[1])
                    return

                lcu = get_lcu()
                if lcu is None:
                    self._send(503, b"", "image/png")
                    return
                blob = lcu.get_bytes("/lol-game-data/assets/" + asset)
                if blob is None:
                    self._send(404, b"", "image/png")
                    return
                ctype = _art_ctype(asset)
                _art_store(asset, blob, ctype)
                self._send_cacheable(200, blob, ctype)
                return

            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "bad json"}, 400)
                return

            if path == "/api/reload":
                token = reloader.bump(str(payload.get("reason") or "requested"))
                self._json({"ok": True, "token": token})
                return

            if path == "/api/settings":
                handler = hooks.get("change_setting")
                if handler is None:
                    self._json({"ok": False, "error": "unavailable"}, 404)
                    return
                self._json(handler(payload))
                return

            if path == "/api/window":
                handler = hooks.get("window_action")
                if handler is None:
                    self._json({"ok": False, "error": "no window shell"}, 404)
                    return
                self._json(handler(payload))
                return

            if path == "/api/mock":
                if mock is None:
                    self._json({"ok": False, "error": "not in mock mode"}, 404)
                    return
                self._json(mock.apply(str(payload.get("action") or ""),
                                      payload.get("value")))
                return

            if path == "/api/import":
                hook = hooks.get("import_build")
                if hook is None:
                    self._json({"ok": False, "error": "unavailable"}, 404)
                    return
                self._json(hook(payload))
                return

            if path == "/api/elevation":
                hook = hooks.get("choose_elevation")
                if hook is None:
                    self._json({"ok": False, "error": "unavailable"}, 404)
                    return
                self._json(hook(payload))
                return

            if path == "/api/welcome":
                hook = hooks.get("dismiss_welcome")
                if hook is None:
                    self._json({"ok": False, "error": "unavailable"}, 404)
                    return
                self._json(hook(payload))
                return

            if path == "/api/opponent":
                hook = hooks.get("choose_opponent")
                if hook is None:
                    self._json({"ok": False, "error": "unavailable"}, 404)
                    return
                self._json(hook(payload))
                return

            if path == "/api/browse":
                url = str(payload.get("url") or "")
                parts = urlparse(url)
                if parts.scheme != "https" or \
                        parts.netloc.lower() not in BROWSE_HOSTS:
                    self._json({"ok": False, "error": "not a known source"}, 400)
                    return
                import webbrowser
                webbrowser.open(url)
                self._json({"ok": True})
                return

            if path == "/api/select":
                skin_id = payload.get("skinId")
                chroma_id = payload.get("chromaId")
                for name, value in (("skinId", skin_id), ("chromaId", chroma_id)):
                    if value is not None and not isinstance(value, int):
                        self._json({"error": f"{name} must be an integer"}, 400)
                        return
                on_select(skin_id, chroma_id)
                self._json({"ok": True, "selectedSkinId": skin_id,
                            "selectedChromaId": chroma_id})
                return

            self._json({"error": "not found"}, 404)

    return Handler


class PortInUse(RuntimeError):
    """Raised instead of a bare OSError, so main can explain the fix."""

    def __init__(self, port: int):
        self.port = port
        super().__init__(f"port {port} is already in use")


def serve(state: State, get_lcu, on_select, port: int = 7777,
          mock=None, list_champions=None,
          allow_fallback: bool = True, hooks=None,
          reloader=None) -> ThreadingHTTPServer:
    """Bind the picker's local server.

    When the caller did not insist on a specific port, walk forward to the
    next free one instead of failing. Launched from the Dock there is nowhere
    to print an explanation, so a port clash would otherwise look like the app
    simply refusing to open.
    """
    handler = make_handler(state, get_lcu, on_select, mock, list_champions,
                           hooks, reloader)
    attempts = range(port, port + 12) if allow_fallback else [port]

    for candidate in attempts:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), handler)
            httpd.chosen_port = candidate
            break
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                continue
            raise
    else:
        raise PortInUse(port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
