#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The macOS app shell: a menu bar item, a settings window, and a picker window
that shows itself when it is needed.

Shape of the app:

* **Settings window** opens at launch. Closing it does not quit.
* **Menu bar item** is what the app lives in once that window is closed, so it
  keeps watching champ select without occupying the Dock or a window.
* **Picker window** is small and appears by itself when a champion is locked,
  then goes away when champ select ends. It is deliberately not always-on-top:
  it raises itself at the one moment it is wanted rather than sitting over
  everything permanently.

Two macOS details decide the whole design here.

*Nothing is ever really closed.* pywebview stops the run loop as soon as its
last window closes, so a window that honestly closes takes the process with it,
menu bar item included -- and the app would quit the moment you dismissed the
settings window. Both windows therefore refuse the native close and hide
instead. Quit from the menu is the only real exit.

*Every AppKit call is marshalled to the main queue.* pywebview fires its window
events on worker threads, and touching an NSWindow off the main thread aborts
the process outright, with SIGTRAP and no Python traceback.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("tibbers.shell")

PICKER_SIZE = (620, 470)
SETTINGS_SIZE = (700, 560)
BACKDROP = "#0a0e13"

APP_NAME = "Tibbers"
#: Riot ships ability icons at 64x64 and no larger, which is ample for a menu
#: bar item and the reason the Dock icon is upscaled at build time.
ICON = Path(__file__).resolve().parent.parent / "assets" / "tibbers.png"
#: Menu bar items are measured in points; 18 leaves the standard breathing room.
ICON_POINTS = 18


def on_main(fn: Callable[[], object]) -> None:
    """Run *fn* on the AppKit main queue.

    The return value is dropped on purpose. A block handed to AppKit is typed
    void, and returning anything from it raises an Objective-C exception that
    kills the process rather than surfacing as a Python error -- so a callback
    ending in `native.orderOut_(None), app.activate()` would take the app down.
    Swallowing the result here means callers cannot make that mistake.
    """
    def block():
        fn()

    try:
        import AppKit
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(block)
    except Exception as exc:  # noqa: BLE001
        log.debug("main-queue dispatch failed: %s", exc)


def off_main(fn: Callable[[], None]) -> None:
    """Run *fn* on a worker thread.

    pywebview creates a window only when `create_window` is called from a
    thread other than the main one -- from the main thread it appends to its
    list and returns, and no window ever appears. An NSMenuItem action runs on
    the main thread, so every menu command has to hop off it first.
    """
    threading.Thread(target=fn, daemon=True).start()


class MenuBar:
    """A status item, so the app can run with no window open."""

    def __init__(self, on_settings: Callable[[], None],
                 on_picker: Callable[[], None],
                 on_quit: Callable[[], None]):
        self.on_settings = on_settings
        self.on_picker = on_picker
        self.on_quit = on_quit
        self._item = None
        self._delegate = None
        self._observer = None

    def install(self) -> None:
        """Install the status item, once the app has actually finished launching.

        pywebview runs the start hook on a thread *before* it calls NSApp.run,
        so this is reached while the app is still coming up. A status item
        created that early is accepted -- isVisible even reports true -- and
        then never drawn. It survived a launch from a terminal, where the
        startup sequence is looser, and vanished when launched from the Dock,
        which is the launch that matters.
        """
        def arm():
            import AppKit
            app = AppKit.NSApplication.sharedApplication()
            if app.isRunning():
                self._install()
                return

            centre = AppKit.NSNotificationCenter.defaultCenter()

            def launched(_note):
                self._install()

            # Held on self: the observer is deregistered when it is released,
            # and a token dropped here would take the status item with it.
            self._observer = centre.addObserverForName_object_queue_usingBlock_(
                AppKit.NSApplicationDidFinishLaunchingNotification, None,
                AppKit.NSOperationQueue.mainQueue(), launched)

        on_main(arm)

    def _install(self) -> None:
        try:
            import AppKit
            import objc
        except ImportError:
            log.error("PyObjC is missing; there will be no menu bar item")
            return

        app = AppKit.NSApplication.sharedApplication()
        log.info("installing menu bar item: policy=%s bundle=%s thread=%s",
                 app.activationPolicy(),
                 AppKit.NSBundle.mainBundle().bundleIdentifier(),
                 threading.current_thread().name)

        class Handler(AppKit.NSObject):
            def openSettings_(self, _sender): self.cb_settings()      # noqa: N802
            def openPicker_(self, _sender):   self.cb_picker()        # noqa: N802
            def quitApp_(self, _sender):      self.cb_quit()          # noqa: N802

        handler = Handler.alloc().init()
        handler.cb_settings = lambda: off_main(self.on_settings)
        handler.cb_picker = lambda: off_main(self.on_picker)
        handler.cb_quit = lambda: off_main(self.on_quit)
        self._delegate = handler                # keep it alive

        bar = AppKit.NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)

        button = item.button()
        if button is not None:
            image = self._icon(AppKit)
            if image is not None:
                button.setImage_(image)
            else:
                button.setTitle_("◈")
            button.setToolTip_(APP_NAME)

        menu = AppKit.NSMenu.alloc().init()
        for label, sel, key in (
            ("Open picker", "openPicker:", "p"),
            ("Settings…", "openSettings:", ","),
            (None, None, None),
            (f"Quit {APP_NAME}", "quitApp:", "q"),
        ):
            if label is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, objc.selector(getattr(handler, sel.replace(":", "_")),
                                     signature=b"v@:@"), key)
            mi.setTarget_(handler)
            menu.addItem_(mi)

        item.setMenu_(menu)
        self._item = item
        log.info("menu bar item installed: visible=%s length=%s button=%s",
                 item.isVisible(), item.length(), item.button() is not None)

        # isVisible lies. The window server assigns the item its place in the
        # menu bar asynchronously, and when it refuses -- which it does for a
        # process whose image no longer matches what LaunchServices launched --
        # the item still reports visible while its window stays zero-height and
        # unplaced. The height is the only honest signal, so check it once the
        # placement has had time to happen.
        def placed(_timer):
            button = item.button()
            window = button.window() if button is not None else None
            if window is None:
                log.warning("menu bar item has no window; it will not be shown")
                return
            frame = window.frame()
            if frame.size.height <= 0:
                log.warning("menu bar item was refused a place in the menu bar "
                            "(window %gx%g at %g,%g)", frame.size.width,
                            frame.size.height, frame.origin.x, frame.origin.y)
            else:
                log.info("menu bar item placed at %g,%g (%gx%g)",
                         frame.origin.x, frame.origin.y,
                         frame.size.width, frame.size.height)

        import Foundation
        Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            3.0, False, placed)

    @staticmethod
    def _icon(AppKit):
        """The Tibbers icon, sized for the menu bar.

        Not a template image: it is drawn in its own colours, and flattening it
        to a stencil would lose everything that identifies it.
        """
        if not ICON.is_file():
            return None
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(ICON))
        if image is None:
            return None
        image.setSize_(AppKit.NSMakeSize(ICON_POINTS, ICON_POINTS))
        image.setTemplate_(False)
        return image

    def set_status(self, text: str) -> None:
        """Reflect state in the tooltip, not the title: the bar is not a log."""
        def apply():
            if self._item is not None and self._item.button() is not None:
                self._item.button().setToolTip_(f"{APP_NAME} — {text}")
        on_main(apply)


def quiet_launch() -> None:
    """Stop pywebview activating the app as the run loop starts.

    `BrowserView.first_show` calls activateIgnoringOtherApps_ unconditionally,
    before NSApp.run and regardless of whether the window is hidden -- so
    every launch pulled focus off whatever was in front, which mid-game is a
    borderless client losing the foreground.

    Rather than patching pywebview's internals, the app is put into the
    Prohibited activation policy first: activation requests against a
    Prohibited app do nothing. `Windows.go_background()` lifts it to Accessory
    from the main queue, which cannot run until the run loop is up -- i.e.
    after the activation attempt has already been ignored.
    """
    try:
        # Importing the platform module runs its class body, which sets the
        # policy to Regular; the override has to come after that, and
        # pywebview itself imports it by this same name.
        import importlib

        importlib.import_module("webview.platforms.cocoa")
        import AppKit

        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyProhibited)
        log.info("quiet launch: activation suppressed until the run loop is up")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not suppress activation on launch: %s", exc)


class Windows:
    """Owns the two webview windows, their visibility, and the Dock icon."""

    def __init__(self, base_url: str, prefs=None):
        self.base_url = base_url.rstrip("/")
        self.prefs = prefs
        self.settings = None
        self.picker = None
        self._visible: set = set()
        self._quitting = False
        self._on_top = False
        self._lock = threading.Lock()

    # -- construction ------------------------------------------------------

    def _remember(self, name: str, **fields) -> None:
        if self.prefs is not None:
            try:
                self.prefs.remember_geometry(name, **fields)
            except Exception as exc:  # noqa: BLE001
                log.debug("could not record %s geometry: %s", name, exc)

    def _stored(self, name: str, default_size) -> dict:
        box = self.prefs.geometry(name) if self.prefs is not None else {}
        return {
            "width": int(box.get("width") or default_size[0]),
            "height": int(box.get("height") or default_size[1]),
            "x": box.get("x"),
            "y": box.get("y"),
        }

    def was_visible(self, name: str) -> bool:
        """Whether *name* was on screen when the app last stopped."""
        return bool((self.prefs.geometry(name) if self.prefs else {}).get("visible"))

    def _spawn(self, name: str, hidden: bool):
        """Create one window. Both are made once and then shown and hidden."""
        import webview

        if name == "settings":
            box = self._stored("settings", SETTINGS_SIZE)
            w = webview.create_window(
                APP_NAME, f"{self.base_url}/settings",
                width=box["width"], height=box["height"],
                x=box["x"], y=box["y"],
                min_size=(560, 460), background_color=BACKDROP,
                hidden=hidden, focus=True,
            )
        else:
            box = self._stored("picker", PICKER_SIZE)
            w = webview.create_window(
                "Pick a skin", f"{self.base_url}/picker",
                width=box["width"], height=box["height"],
                x=box["x"], y=box["y"],
                min_size=(460, 380), background_color=BACKDROP,
                frameless=True, easy_drag=True,
                # `focus` is what pywebview's window answers canBecomeKeyWindow
                # with, and WebKit only tracks the mouse in a key window: a
                # window created with focus=False never gets :hover, only
                # clicks. Creating it hidden does not need focus=False to stay
                # out of the way -- showing is our own code, and it decides.
                hidden=hidden, focus=True,
            )

        # Refuse the native close. Without this, dismissing a window ends the
        # run loop and the whole app goes with it.
        w.events.closing += lambda: self._on_closing(name)
        # Where it ended up, so a restart puts it back. The picker is
        # frameless and dragged by its body, so a moved event is the only
        # record of where the user actually wants it.
        w.events.moved += lambda x, y: self._remember(name, x=int(x), y=int(y))
        w.events.resized += lambda width, height: self._remember(
            name, width=int(width), height=int(height))
        return w

    def prepare(self) -> None:
        """Build the picker up front, hidden.

        Called before the run loop starts, which is the one moment the main
        thread is the right place to create a window from.

        It appears at the one moment the user is under a timer, so paying for
        the WKWebView and the first page load now is worth it; by the time a
        champion locks, the window only has to be ordered forward.
        """
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

    # -- closing -----------------------------------------------------------

    def _on_closing(self, name: str):
        """Hide rather than close. Returning False cancels the native close."""
        if self._quitting:
            return None
        self._hide(name)
        return False

    def begin_quit(self) -> None:
        """Stop vetoing closes, so the app can actually exit."""
        self._quitting = True

    # -- visibility --------------------------------------------------------

    def _show(self, name: str, raise_it: bool) -> None:
        # pywebview realises a window only when create_window is called from a
        # thread other than the main one; from the main thread it registers the
        # window, drops it, and returns the dead object with no error. Menu
        # commands arrive on the main thread, so the hop belongs here rather
        # than at each call site -- and a dropped window would otherwise be
        # cached forever, so every later attempt would short out on it too.
        if threading.current_thread() is threading.main_thread():
            off_main(lambda: self._show(name, raise_it))
            return

        # A window being created in order to be shown is created visible; only
        # `prepare` builds one up front to keep out of sight.
        window = self._ensure(name, hidden=False)
        with self._lock:
            self._visible.add(name)
        self._remember(name, visible=True)

        def apply():
            native = getattr(window, "native", None)
            if native is None:
                log.warning("%s window was never realised by the toolkit", name)
                return
            import AppKit
            app = AppKit.NSApplication.sharedApplication()
            # Back into the Dock and Cmd-Tab while a window is up; the policy
            # has to change before activating or the app cannot come forward.
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
            if name == "picker" and self._on_top:
                native.setLevel_(AppKit.NSFloatingWindowLevel)
            if raise_it:
                app.activateIgnoringOtherApps_(True)
                native.makeKeyAndOrderFront_(None)
                native.orderFrontRegardless()
            else:
                native.orderFront_(None)

        on_main(apply)

    def _hide(self, name: str) -> None:
        with self._lock:
            window = self.settings if name == "settings" else self.picker
            self._visible.discard(name)
            alone = not self._visible
        self._remember(name, visible=False)

        if window is None:
            return

        def apply():
            native = getattr(window, "native", None)
            if native is None:
                return
            native.orderOut_(None)
            if alone:
                # Nothing on screen: leave the Dock and live in the menu bar.
                import AppKit
                AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                    AppKit.NSApplicationActivationPolicyAccessory)

        on_main(apply)

    # -- the two windows ---------------------------------------------------

    def open_settings(self) -> None:
        self._show("settings", raise_it=True)

    def open_picker(self, raise_it: bool = True) -> None:
        self._show("picker", raise_it=raise_it)

    def close_picker(self) -> None:
        self._hide("picker")

    def set_on_top(self, on_top: bool) -> None:
        """Float the picker above other windows, or stop."""
        with self._lock:
            window = self.picker
        self._on_top = bool(on_top)
        if window is None:
            return

        def apply():
            native = getattr(window, "native", None)
            if native is None:
                return
            import AppKit
            native.setLevel_(AppKit.NSFloatingWindowLevel if on_top
                             else AppKit.NSNormalWindowLevel)

        on_main(apply)

    def stand_down(self) -> None:
        """Stop floating, stay open.

        Once the game starts the picker is still worth reading -- the build
        and the counters are as useful in the loading screen as in champ
        select -- but it has no business sitting over the game. Dropping the
        window level leaves it a normal window: behind the game, still there
        when you tab to it.
        """
        with self._lock:
            window = self.picker
        if window is None:
            return

        def apply():
            native = getattr(window, "native", None)
            if native is None:
                return
            import AppKit
            native.setLevel_(AppKit.NSNormalWindowLevel)

        on_main(apply)

    def picker_open(self) -> bool:
        with self._lock:
            return "picker" in self._visible

    def restore(self) -> None:
        """Put back whatever was on screen when the app last stopped.

        Without raising anything: a restart during a game must not pull focus
        off it, and a window that comes back in front is as disruptive as one
        that never comes back at all. Positions are restored at creation, so
        this only decides what is shown.
        """
        for name in ("settings", "picker"):
            if self.was_visible(name):
                log.info("restoring the %s window where it was", name)
                self._show(name, raise_it=False)

    def go_background(self) -> None:
        """Drop out of the Dock when launching straight into the menu bar."""
        def apply():
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)
        on_main(apply)
