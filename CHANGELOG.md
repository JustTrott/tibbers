# Changelog

One section per version, newest first. A version in progress lists what has
landed on its branch and, under *Planned*, what it will still carry; the
section becomes the release notes when it ships. See "Versions and branches"
in `CLAUDE.md`.

## 1.1.1 — in progress

A new picker, English and Russian, the build pages working on Windows, and
skins working again after League's 16.18 patch. Everything here is new since
1.0.2; 1.1.0 (below) never reached anyone.

**The picker**
- The skin rail is a ring, like the client's own carousel: the base skin sits in the middle and is the default, the newest skins one step to its left, the oldest one step to its right, and stepping off either end comes round the other side. The whole strip slides as one piece; tiles keep their order while mods build.
- A rail that fits the window stands still; only the highlight moves.
- The last pick wins. A pick made while a skin was still building used to be dropped, and arrowing through the rail could arm the first skin passed over. The picker shows what is actually armed, under the skin name.
- A skin remembers its chroma and brings it back when picked again.
- The picker is an ordinary window with a minimize button. Always-on-top and auto-hide are gone; it opens at lock-in and otherwise behaves like any other window.
- Rune import no longer asks which page to replace when all slots are full: it takes over the page in use under the Tibbers name.

**English and Russian**
- The app follows the system language, and Settings has a Language row at the top. Champion, skin, item, rune and augment names come from the client and u.gg already localized. On Windows, Setup opens in the display language too.

**Game modes**
- ARAM Mayhem gets its own augment rankings (u.gg's tiers, per champion and for the whole mode), keeps ARAM's build for items, and drops the rune block the mode does not use.

**Skins**
- Skins with switchable stages (Exalted and their kin) were missing the panel that switches them. Their mods are rebuilt with it.

**Start at sign-in**
- A switch in Settings, on both platforms, read from the machine rather than from a preference. Not offered from a portable copy.

**Windows: builds and counters, at last**
- The build and counters pages were empty on most Windows machines. u.gg's CDN scores the TLS handshake and refuses the one Windows' own curl presents, and most machines do not have a curl at all. tibbers now fetches a small curl that presents a browser's handshake (curl-impersonate, about 4 MB) alongside the injection tools on first launch, and reads u.gg through it.
- Setup downloads the injection tools and that curl itself, on its own progress page, and offers Retry when tibbers is still running instead of telling you to run Setup again.

**macOS: a build that runs on other Macs**
- Earlier builds linked the Python on the machine that built them, and named the build folder in their launcher, so a downloaded copy could not start anywhere else. The app now carries its own Python inside the bundle (20 MB download, was 9).

**macOS: the game opens again after League 16.18**
- League's 16.18 patch, on 10 September, made every game started with a skin armed close the instant it opened -- no window, no error, just the client offering Reconnect. The game was reacting to how it had been hooked: the patcher allocated a page inside it, and the game's anti-cheat kills it for that. tibbers now carries its own build of the patcher, which puts what it needs inside a function it already replaces and allocates nothing at all. Nothing else about the hook changed.

**Both platforms**
- Build data is trusted for eight hours before it is checked again (u.gg regenerates it roughly daily), so the pages meet the CDN's bot check far less often. A refused request is retried a few times, and when every attempt is refused the copy you already have is shown rather than nothing.

## 1.1.0 — pulled

Published 2026-09-09 for a few minutes with a macOS build that could not start on any Mac but the one that built it, then taken down. Its tag stays so the number is never reused; its content shipped as 1.1.1.

## 1.0.2 — 2026-09-08

A solid Windows build, and updates that update themselves properly. macOS is unchanged bar the version and a download checksum check.

**Windows: no more freezes**
- The picker froze the whole app on some lock-ins and at game start ("Tibbers.exe is not responding"). The window was being driven from the League-watcher thread, which could deadlock against the UI thread. Every window action now runs on the UI thread and never blocks.
- Minimising a window no longer parks it off-screen.

**Windows: a clean update**
- Updating is the installer itself, run silently. It waits for tibbers to close, replaces the app through Windows' own Restart Manager, logs to `work\update.log`, and reopens tibbers in the tray.
- The download is checked against the release's SHA-256 before anything runs (macOS too).
- One copy runs at a time: launching tibbers while it is already in the tray opens the running copy's Settings.
- No injection tool ever opens a console window during champ select.

**Updates**
- New releases are checked every six hours and when Settings opens, and installed once League is idle — never during champ select, a queue, or a game. 1.0.0 and 1.0.1 have the old updater and had to install this one by hand.

## 1.0.1 — 2026-09-03

Updates install themselves.

- New releases are checked every six hours and whenever Settings opens, not just once at launch. With **Install updates by itself** on (the default), a new build is downloaded and swapped in while League is idle. **Update now** installs at once.
- Windows: no more empty terminal windows during champ select; the patcher was started with a console of its own.
- Windows: a minimised window is no longer remembered as its position.

## 1.0.0 — 2026-09-02

Now on Windows as well as macOS.

- Windows: native desktop app (a WebView2 window and a system-tray icon), skin injection with no elevation and no password via LTK's patcher, a per-user installer, in-app updates.
- Build guide opens on the general build (the larger sample), matchup one click away.
- Counters focus your lane opponent and follow it as a better one is surfaced.

## 0.1.1 — 2026-09-01

- The passwordless prompt at launch, and one-click self-update from Settings.

## 0.1.0 — 2026-08-31

First public release: macOS on Apple Silicon, a skin picker that hooks the running game and never touches the League install.
