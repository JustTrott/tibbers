#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows desktop shell: the picker and settings windows (pywebview / WebView2)
and a system-tray icon (pystray) that keeps tibbers watching champ select when
its windows are closed -- the Windows counterpart of the macOS menu-bar item.

Mirrors the public interface of ``_shell_macos`` so main.py drives either the
same way. What is missing here relative to macOS is only the activation-policy
dance (Windows has no accessory-app concept) and the main-queue marshalling
(pywebview's WebView2 backend is safe to poke from the tray thread).

NOTE: written on macOS and not yet run on Windows. The window lifecycle and the
tray-thread / GUI-loop interplay are the parts to validate there.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("tibbers.shell")

PICKER_SIZE = (620, 470)
SETTINGS_SIZE = (700, 560)
BACKDROP = "#0a0e13"
APP_NAME = "Tibbers"
ICON = Path(__file__).resolve().parent.parent / "assets" / "tibbers.png"

# Windows parks a minimised window at (-32000, -32000). pywebview divides that
# by the display scale and reports it as an ordinary move (-25600 at 125%), and
# a window created at that position on the next launch is simply never seen.
# Anything this far out is that sentinel, never a place the user put a window.
_PARKED = -10000


def on_screen(x, y, screens=None) -> bool:
    """Is (x, y) somewhere a window can be found -- inside a monitor, or at
    least not parked at the minimised sentinel?

    *screens* is a list of objects with x/y/width/height (pywebview's
    ``webview.screens``); when it is None the live list is consulted, and when
    that is unavailable only the sentinel check applies.
    """
    if x is None or y is None:
        return False
    if x <= _PARKED or y <= _PARKED:
        return False
    if screens is None:
        try:
            import webview
            screens = list(webview.screens)
        except Exception:  # noqa: BLE001
            screens = []
    if not screens:
        return True
    # The title bar must land on some monitor; a window whose top-left is a
    # little past an edge is still draggable, so allow a margin.
    margin = 48
    for scr in screens:
        if (scr.x - margin <= x < scr.x + scr.width - margin
                and scr.y - margin <= y < scr.y + scr.height - margin):
            return True
    return False


# --- main-queue marshalling ------------------------------------------------
# On macOS every AppKit call has to hop to the main queue; on Windows the
# pywebview window methods are safe to call from another thread, so these just
# run the callable where they stand.

def on_main(fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        log.debug("on_main callable failed: %s", exc)


def off_main(fn: Callable[[], None]) -> None:
    fn()


def quiet_launch() -> None:
    """macOS suppresses activation on a silent relaunch; nothing to do here."""
    return None


def gui_backend() -> Optional[str]:
    """Let pywebview pick its Windows backend (WebView2 / edgechromium)."""
    return None


def terminate() -> None:
    """End the run loop: drop the tray and destroy the windows, which returns
    control from webview.start()."""
    global _TRAY
    tray = _TRAY
    if tray is not None:
        try:
            tray.stop()
        except Exception:  # noqa: BLE001
            pass
        _TRAY = None
    try:
        import webview
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.debug("terminate: %s", exc)


def set_topmost(window, on_top: bool) -> None:
    """Set a window's TopMost on the UI thread, without waiting for it.

    pywebview's own `on_top` setter assigns the WinForms property straight
    from the calling thread. Cross-thread that is a synchronous SetWindowPos
    the UI thread has to service -- and when the UI thread is itself waiting
    for the GIL to run a Python handler (its 500 ms timer tick, a Move event)
    the two wait on each other for good: picker never shown, tray menu dead,
    API dead, nothing logged. It struck the first time League patched and the
    library was being rebuilt on eight threads while a champion was locked.
    `show()` and `hide()` are marshalled by pywebview with Invoke; this does
    the same for TopMost with BeginInvoke, which posts and returns.
    """
    try:
        from webview.platforms.winforms import BrowserView
        from System import Func, Type

        form = BrowserView.instances.get(window.uid)
        if form is None:
            return

        def _apply():
            form.TopMost = bool(on_top)

        form.BeginInvoke(Func[Type](_apply))
    except Exception as exc:  # noqa: BLE001 -- a window not yet created, or gone
        log.debug("could not set on_top: %s", exc)


class Windows:
    """The picker and settings windows, their visibility, and geometry."""

    def __init__(self, base_url: str, prefs=None):
        self.base_url = base_url.rstrip("/")
        self.prefs = prefs
        self.picker = None
        self.settings = None
        self._lock = threading.Lock()
        self._quitting = False
        self._on_top = False
        self._visible = {"picker": False, "settings": False}

    # -- geometry ----------------------------------------------------------

    def _remember(self, name: str, **fields) -> None:
        if self.prefs is None:
            return
        try:
            self.prefs.remember_geometry(name, **fields)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not record %s geometry: %s", name, exc)

    def _moved(self, name: str, x: int, y: int) -> None:
        """A window moved. Minimising reports the parked position as a move;
        remembering that would recreate the window off-screen next launch."""
        if not on_screen(x, y):
            log.debug("ignoring off-screen %s position (%s, %s)", name, x, y)
            return
        self._remember(name, x=x, y=y)

    def _stored(self, name: str, default_size) -> dict:
        box = self.prefs.geometry(name) if self.prefs is not None else {}
        box = dict(box or {})
        box.setdefault("width", default_size[0])
        box.setdefault("height", default_size[1])
        box.setdefault("x", None)
        box.setdefault("y", None)
        if not on_screen(box["x"], box["y"]):
            # Off every monitor (a parked position that slipped through an
            # older build, or a monitor that is gone): let it centre instead.
            box["x"] = box["y"] = None
        return box

    def was_visible(self, name: str) -> bool:
        if self.prefs is None:
            return False
        return bool((self.prefs.geometry(name) or {}).get("visible"))

    # -- window creation ---------------------------------------------------

    def _spawn(self, name: str, hidden: bool):
        import webview

        if name == "settings":
            box = self._stored("settings", SETTINGS_SIZE)
            w = webview.create_window(
                APP_NAME, f"{self.base_url}/settings",
                width=box["width"], height=box["height"],
                x=box["x"], y=box["y"],
                min_size=(560, 460), background_color=BACKDROP,
                hidden=hidden,
            )
        else:
            box = self._stored("picker", PICKER_SIZE)
            w = webview.create_window(
                "Pick a skin", f"{self.base_url}/picker",
                width=box["width"], height=box["height"],
                x=box["x"], y=box["y"],
                min_size=(460, 380), background_color=BACKDROP,
                frameless=True, easy_drag=True,
                on_top=(name == "picker" and self._on_top),
                hidden=hidden,
            )

        w.events.closing += lambda: self._on_closing(name)
        w.events.moved += lambda x, y: self._moved(name, int(x), int(y))
        w.events.resized += lambda width, height: self._remember(
            name, width=int(width), height=int(height))
        return w

    def prepare(self) -> None:
        """Build the picker up front, hidden, so a lock-in only has to show it."""
        with self._lock:
            if self.picker is not None:
                return
        w = self._spawn("picker", hidden=True)
        with self._lock:
            self.picker = w

    def _ensure(self, name: str, hidden: bool = True):
        with self._lock:
            existing = self.settings if name == "settings" else self.picker
        if existing is not None:
            return existing
        w = self._spawn(name, hidden=hidden)
        with self._lock:
            if name == "settings":
                self.settings = self.settings or w
                return self.settings
            self.picker = self.picker or w
            return self.picker

    # -- closing: retreat to the tray rather than quit ---------------------

    def _on_closing(self, name: str):
        if self._quitting:
            return None
        self._hide(name)
        return False

    def begin_quit(self) -> None:
        self._quitting = True

    # -- visibility --------------------------------------------------------

    def _window(self, name: str):
        with self._lock:
            return self.settings if name == "settings" else self.picker

    def _show(self, name: str, raise_it: bool) -> None:
        w = self._ensure(name, hidden=True)
        try:
            if name == "picker":
                set_topmost(w, self._on_top)
            w.show()
            self._visible[name] = True
            self._remember(name, visible=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not show %s: %s", name, exc)

    def _hide(self, name: str) -> None:
        w = self._window(name)
        if w is None:
            return
        try:
            w.hide()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not hide %s: %s", name, exc)
        self._visible[name] = False
        self._remember(name, visible=False)

    def open_settings(self) -> None:
        self._show("settings", raise_it=True)

    def open_picker(self, raise_it: bool = True) -> None:
        self._show("picker", raise_it=raise_it)

    def close_picker(self) -> None:
        self._hide("picker")

    def picker_open(self) -> bool:
        return bool(self._visible.get("picker"))

    def set_on_top(self, on_top: bool) -> None:
        self._on_top = bool(on_top)
        w = self._window("picker")
        if w is not None:
            set_topmost(w, self._on_top)

    def stand_down(self) -> None:
        """Drop always-on-top without hiding -- the game is being played."""
        w = self._window("picker")
        if w is not None:
            set_topmost(w, False)

    def go_background(self) -> None:
        """No visible window: live in the tray."""
        self._hide("picker")
        self._hide("settings")

    def restore(self) -> None:
        """After a silent relaunch, put back whatever was visible before."""
        for name in ("settings", "picker"):
            if self.was_visible(name):
                self._show(name, raise_it=False)


# The running tray icon, so terminate() can stop it.
_TRAY = None


class MenuBar:
    """A system-tray icon with the same three actions as the macOS menu."""

    def __init__(self, on_settings: Callable[[], None],
                 on_picker: Callable[[], None],
                 on_quit: Callable[[], None]):
        self.on_settings = on_settings
        self.on_picker = on_picker
        self.on_quit = on_quit
        self._icon = None

    def install(self) -> None:
        """Build the tray icon and run it on its own thread.

        pystray's own message loop and pywebview's GUI loop each want a thread;
        the GUI loop owns the main thread (webview.start), so the tray runs in
        a daemon thread here.
        """
        global _TRAY
        try:
            import pystray
            from PIL import Image
        except Exception as exc:  # noqa: BLE001
            log.warning("no system tray (install pystray + Pillow): %s", exc)
            return

        try:
            image = Image.open(ICON)
        except Exception:  # noqa: BLE001
            image = Image.new("RGBA", (64, 64), (212, 176, 88, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Open picker", lambda: self.on_picker()),
            pystray.MenuItem("Settings", lambda: self.on_settings()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Tibbers", lambda: self.on_quit()),
        )
        self._icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
        _TRAY = self._icon
        threading.Thread(target=self._icon.run, daemon=True).start()

    def set_status(self, text: str) -> None:
        if self._icon is None:
            return
        try:
            self._icon.title = f"{APP_NAME} — {text}"
        except Exception:  # noqa: BLE001
            pass
