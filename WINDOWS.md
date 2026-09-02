# tibbers on Windows

The whole picker, the u.gg / op.gg guide, on-hover skin building and the WAD
reading/writing are pure Python and already cross-platform. Windows differs in
a few places, all behind `tibbers/system.py` and `tibbers/shell.py`.

| | macOS | Windows |
|---|---|---|
| Overlay build | cslol `mod-tools mkoverlay` | same — cslol `mod-tools.exe mkoverlay` (never injects) |
| Injection | cslol `mod-tools runoverlay`, needs **root** | **LTK's patcher** (`ltk_patcher_host.exe`), **no elevation** |
| Passwordless setup | a sudoers helper | not needed; nothing elevates |
| App shell | native window + menu-bar item | native window (WebView2) + **system-tray icon** |
| Updates | in-app OTA | in-app OTA (this doc) |
| Packaging | `.app` bundle (`build_app.sh`) | PyInstaller + Inno Setup (`build_windows.ps1`) |

## Why two patchers on Windows

cslol's Windows *injection* DLL is a dead end: recent builds carry an expiry
kill-switch, and Riot's Vanguard names `cslol-dll.dll` incompatible and blocks
the game from starting with it. cslol's *overlay builder* (`mkoverlay`) is
unaffected — it is pure local file work that never touches the game — and is
still used.

The injection is handed to **LTK Manager's patcher**, the maintained cslol
successor, whose hook Vanguard accepts. LTK drives over a stdin/stdout line
protocol; tibbers spawns `ltk_patcher_host.exe`, points it at the overlay, and
holds its stdin open so it outlives the app (a detached holder, exactly as on
macOS). One LTK detail matters: LTK verifies its overlay and, for a base-skin
swap — skin0 pointing at another skin's mesh and audio, which is precisely what
tibbers does on purpose — it would refuse to serve. tibbers sets LTK's own
`OPT_OUT_AH_V1` hook flag, which downgrades that check to a warning. That is
LTK's internal quality gate, not the game's anti-cheat: the DLL clears Vanguard
separately, before this runs. tibbers does not touch, evade, or work around the
anti-cheat, and neither of these binaries is modified.

## Running from source

```powershell
git clone https://github.com/JustTrott/tibbers.git
cd tibbers
py -3 -m venv .venv
.\.venv\Scripts\pip install psutil xxhash zstandard pywebview pystray Pillow

# Fetch BOTH tool pairs -- cslol mod-tools (mkoverlay) and the LTK patcher
# (injection). This installs the four files into tools\.
powershell -ExecutionPolicy Bypass -File scripts\fetch_modtools.ps1

.\.venv\Scripts\python main.py
```

`main.py` opens the picker as a native WebView2 window and puts a tibbers icon
in the system tray; closing the window leaves it running in the tray, watching
champ select. `pywebview` needs the WebView2 runtime, which ships with Windows
11 and installs on Windows 10 from Microsoft's Evergreen installer. If
`pywebview` is missing, `main.py` falls back to a browser tab. Start League as
usual, hover a champion, pick a skin, launch the game — no password prompt.

## Packaging

```powershell
# Freeze to dist\Tibbers\  (a PyInstaller onedir)
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

# ...and build the installer (needs Inno Setup; winget install JRSoftware.InnoSetup)
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Installer
```

This produces three things in `dist\`:

| artifact | what it is |
|---|---|
| `Tibbers\` | the frozen app (run `Tibbers\Tibbers.exe` directly) |
| `Tibbers-<ver>-setup.exe` | the per-user installer (no admin) |
| `Tibbers-windows.zip` | the OTA / release asset |

The patcher binaries are **not** bundled — cslol's DLL is unlicensed and LTK's
is signed by its own publisher, so neither is ours to redistribute. Instead:

- the installer runs `Tibbers.exe --fetch-tools` right after install, so the
  first real launch is ready;
- failing that, the app fetches them itself on first run (`tibbers/wintools.py`),
  into `%LOCALAPPDATA%\tibbers\tools` (writable, unlike Program Files).

The installer installs per-user into `%LOCALAPPDATA%\Programs\Tibbers`, adds a
Start Menu entry, and offers a run-at-login tray launch. The skin library and
preferences live in the data dir and survive an uninstall.

## Releasing (and OTA)

OTA mirrors macOS: `tibbers/update.py` checks the repo's latest GitHub Release
at launch, every six hours after, and when Settings is opened. With the
`auto_update` preference on (the default) it downloads the platform asset and,
once League is idle, swaps it in via a detached script that waits for the app
to exit, replaces the install, and relaunches `--quiet`; the Settings button
does the same on request. Windows looks for **`Tibbers-windows.zip`**; macOS for `Tibbers.zip`.

To cut a release: bump `tibbers/__version__`, build with `-Installer`, and
attach both `Tibbers-windows.zip` (OTA) and `Tibbers-<ver>-setup.exe` (first
install) to a GitHub Release tagged `v<ver>`. The zip's name must stay exactly
`Tibbers-windows.zip` or OTA will not find it.

Note: the frozen exe and installer are **unsigned** for now, so Windows
SmartScreen shows an "unknown publisher" prompt (More info → Run anyway) and
some antivirus may be noisy about a PyInstaller injector. Code signing is a
later step (an OV/EV certificate).

## What to check when testing injection

1. **Install discovery** — finds League at `C:\Riot Games\League of Legends`
   (or wherever the running client points). `python main.py` logs it.
2. **LCU connection** — the picker fills with your hovered champion.
3. **Skin building** — skins light up (mods built out of your install).
4. **Injection** — pick a skin, launch the game, watch
   `%LOCALAPPDATA%\tibbers\work\runoverlay.log` for `scanning for game →
   game found → overlay verified → redirected wad`, and the skin in game.
5. **Patcher survives a restart** — closing and reopening tibbers keeps the
   skin on a running game (the LTK host is adopted, not restarted).
