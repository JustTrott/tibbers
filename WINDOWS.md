# tibbers on Windows

**Status: experimental, in progress on the `windows-support` branch.** The
cross-platform core is done and the macOS build is unaffected; the Windows-only
paths (injection, process discovery, install detection) were written on macOS
and need validating on a Windows machine.

## What's the same, and what's different

The whole picker, the u.gg / op.gg guide, the on-hover skin building, and the
WAD reading/writing are pure Python and already cross-platform. Windows differs
in only a few places, all behind `tibbers/system.py`:

| | macOS | Windows |
|---|---|---|
| Injection | cslol `mod-tools runoverlay`, needs **root** (`task_for_pid`) | cslol `mod-tools.exe runoverlay`, **no elevation** — the DLL is injected into the same-user game process |
| Passwordless setup | a sudoers helper | not needed; there's nothing to elevate |
| App shell | native menu-bar app | runs in a **browser tab** for now (no tray yet) |
| Updates | in-app OTA | re-download for now (OTA is macOS-only so far) |

The injection is the **same** cslol `mod-tools.exe` that cslol-manager and Rose
use — same binary, same `runoverlay <overlay> <config> --game:<...>
--opts:configless`, same hook. tibbers pre-arms `runoverlay` during champ select
so cslol's own poll-and-hook catches the game at launch, which is how it works
on macOS and in cslol-manager (Rose instead freezes the game at launch — a
timing choice on top of the identical injection, not a different technique).

## Running it (from source)

```powershell
git clone https://github.com/JustTrott/tibbers.git
cd tibbers
py -3 -m venv .venv
.\.venv\Scripts\pip install psutil xxhash zstandard pywebview

# mod-tools.exe (from cslol-manager). If this fails, run
# cslol-manager-windows.exe yourself and copy its mod-tools.exe into tools\
powershell -ExecutionPolicy Bypass -File scripts\fetch_modtools.ps1

.\.venv\Scripts\python main.py
```

`main.py` opens the picker in your browser (there's no native window on Windows
yet). Start League as usual, hover a champion, pick a skin, launch the game —
no password prompt, unlike macOS.

## What to check when testing

1. **Install discovery** — does it find League at `C:\Riot Games\League of
   Legends` (or wherever the running client points)? `python main.py` logs the
   game/client dirs it found.
2. **LCU connection** — does the picker fill with your hovered champion? That
   proves the lockfile path and client API work.
3. **Skin building** — do skins light up (mods built out of your install)?
4. **Injection** — pick a skin, launch the game, watch
   `%LOCALAPPDATA%\tibbers\work\runoverlay.log` for `Found League → Scanning →
   Patching`, and check the skin is in game.
5. **Patcher survives a restart** — the patcher is started detached with a
   Python holder; closing and reopening tibbers should keep the skin on a
   running game.

## Not done yet (follow-ups)

- A native Windows shell / system-tray item (currently browser-only).
- Packaging to a `.exe` (PyInstaller) and a Windows release + OTA.
- If the pre-armed hook ever misses (game reads its WADs before the hook lands),
  add Rose's freeze-at-launch as a fallback.
