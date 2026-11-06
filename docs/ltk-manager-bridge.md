# Windows LTK Manager backend (opt-in)

This backend replaces Tibbers' direct patcher-host launch with the LTF bridge
used by Rose. It requires an installed LTK Manager. macOS is unaffected.
It has automated session tests; native compilation and an actual League match
must still be verified on a Windows development machine with MSVC and LTK.
It is deliberately opt-in until that verification is complete.

## Build and run

Install Visual Studio C++ Build Tools (x64, Windows SDK) and Tibbers' Python
dependencies as described in WINDOWS.md, then run from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/build_ltf.py
$env:TIBBERS_LTK_BACKEND = 'manager'
# Optional when LTK is outside its usual installation directories:
$env:TIBBERS_LTK_MANAGER = 'C:\path\to\ltk-manager.exe'
.\.venv\Scripts\python.exe main.py
```

Close any existing LTK Manager instance first. Tibbers refuses to take over
an instance it did not launch. The new backend stages the original .fantome
archives and lets LTK build the overlay: it does not run cslol mkoverlay.
Each staged archive gets a unique META/info.json Name, to avoid replacing a
mod already in the manager's library. Mod WAD contents are preserved.

For packaging use `scripts/build_windows.ps1 -LtkBridge` (optionally with
`-Installer`). The spec includes the locally compiled launcher, bridge and
MIT notice when available. Only these own-built components are bundled;
users must install LTK Manager separately. In manager mode automatic standalone
LTK patcher downloads/refreshes are skipped. To switch back, unset
TIBBERS_LTK_BACKEND after stopping the session with Tibbers while idle.

## Where the mods come from

Manager mode does not build skin0 overlays from the installed game. The
patcher's anti-skinhack pass rejects those: it reads the overlay's skin0
skeleton and refuses it when the skeleton belongs to one of the champion's
other skins (`base-skin check: skin0 skeleton is one of ... other skins'
meshes`). Hook flag 4 (`OPT_OUT_AH_V1`) is still sent, but patcher-30 no
longer downgrades this particular check, which is why Rose fails here too
(Alban1911/Rose#432, LeagueToolkit/ltk-manager#924).

Instead, manager mode downloads the ready-made archives Rose itself uses,
from the LeagueSkins catalogue (`Alban1911/LeagueSkins`): the manifest's
commit pins every URL and each file is checked against its git blob SHA-1
and size before it replaces anything. Those archives carry the skin's own
skin0 plus the model entries it names, so the check passes. They are stored
under `skins-leagueskins` to keep them apart from older locally generated
mods, and are fetched per skin on hover - Rose mirrors the whole library,
Tibbers only the skins it is about to show.

Network or catalogue failures are not fatal: the skin is offered without a
mod, the log names the reason, and a champion with missing skins is retried
after a minute. `TIBBERS_LTK_BACKEND` also clears the build-mod action's
skinsmith requirement, since nothing is generated any more.


## Session behavior

Tibbers launches the real manager in the background. The vendored launcher
sets its recorded parent to the desktop shell for LTK's host verification,
and loads the LTF DLL before the manager's WebView2 starts. Python sends
token-authenticated loopback JSON commands; LTF calls the manager's Tauri
backend over CDP. It does not patch LTK's DLL on disk.

Before scanning, Tibbers temporarily disables the user's enabled mods, imports
the selected mods and enables them. Installed IDs and disabled IDs are saved
to work/ltk-session.json so shutdown or a subsequent run can remove Tibbers'
mods and restore the user's selection. Cleanup failures retain this journal
and leave the manager running; do not delete the journal to hide an error.
An interrupted import is reconciled against its saved library snapshot.
Do not edit the hidden manager's library from another tool during a session.
If the process dies, recovery uses a new owned manager; it does not terminate
unrelated processes. Ownership checks include PID, creation time and exe path.

The manager must report scanning before the game is created. Skin changes
after game launch are rejected; the game is never suspended. During a match
an app shutdown leaves the manager alive, and a restart adopts that session.
The existing app lifecycle stops and cleans up the session when appropriate.

Readiness is not successful application: `patched` requires a current-session
`overlay verified` or `redirected wad` log record. Host verification failures
remain errors even if the bridge itself answered successfully. Manager logs:
`%APPDATA%\dev.leaguetoolkit.manager\logs\ltk-manager.*.log`.

## Manual acceptance check (still required)

1. Start with an enabled personal mod in LTK; close LTK and launch Tibbers
   with manager mode enabled. Select a skin before the game starts.
2. Confirm manager scanning is reported, launch a practice match and check
   both its visible skin and `overlay verified` / `redirected wad` logs.
3. Restart Tibbers mid-match: the manager must remain alive and be adopted.
4. End the match/cancel selection and stop the patcher: imported Tibbers mods
   must disappear and the original enabled mods must be restored.
5. Try with LTK already open, a missing bridge, and a refused overlay: each
   must report an error instead of claiming the skin was applied.

This integration depends on internal LTK commands and checks and may require
changes after a manager update. Provenance: native/ltf/TIBBERS.md.
