# Working on tibbers

A macOS skin picker that hooks the **running League game process** and never
touches the League installation. Someone is usually playing on this machine
while you work. Everything below exists because of that.

## Before anything else

```bash
scripts/phase.sh          # exit 0 idle · 10 champ select · 20 in game · 30 no client
```

Run it before any restart, build, or install, and branch on the exit code. It
needs no venv and no running app.

| exit | meaning | what you may do |
|------|---------|-----------------|
| `0` / `30` | idle, or League not running | anything |
| `10` | **champ select** | UI edits and `deploy.sh --static` only. Never restart. |
| `20` | in game | restart is fine *if* `patcherDetached` is true in `--json`; `deploy.sh` checks for you |

## The four rules

1. **Never restart, kill, signal, `open`, or activate the live app.** It runs
   from `/Applications/Tibbers.app` on `:7777` with its data in
   `~/Library/Application Support/tibbers`. `scripts/deploy.sh` is the only
   thing that restarts it, and only the user runs `deploy.sh` (it installs
   into `/Applications`).
2. **Set `TIBBERS_HOME` on every instance you start.** Instances share one
   data directory otherwise, and a second `mkoverlay` rebuilds the directory
   the live patcher is serving a live game from. `scripts/dev.sh` does this
   for you; the app also refuses to inject whenever `TIBBERS_HOME` is set.
3. **Verify in a browser tab.** `http://127.0.0.1:<dev port>/` renders the
   same pages the app window does. Activating the app window takes the
   foreground off a game.
4. **Leave the injector alone.** `injector.py`, `privileged.py` and
   `system.py`'s elevated paths decide what is done to the running game, and
   Riot bans for it. Process lifetime (spawn, detach, adopt, pidfile) is
   changeable; the command, its arguments and the hook are not.

## Scripts

| script | use it for |
|--------|-----------|
| `scripts/dev.sh --no-window --mock` | **the default way to work.** Source, own port, own `TIBBERS_HOME`, injection off, champ select replayable with no client |
| `scripts/phase.sh [--json\|--quiet]` | what League is doing; the exit code is the answer |
| `scripts/deploy.sh` | build + install + quiet restart. **The user runs this**, or an agent only when asked |
| `scripts/deploy.sh --static` | UI-only change: syncs `tibbers/static` into the bundle and reloads open windows. No restart, so it is safe in champ select |
| `scripts/build_app.sh` | build to `dist/` to check the bundle still builds. `--install` writes to `/Applications` — the user's call |
| `scripts/fetch_python.sh` | the CPython that ships inside the bundle (python-build-standalone, pinned), into `runtime/python`. Run once; `build_app.sh` refuses without it |
| `scripts/lobby_smoke.sh [--url https://lobby.tibbers.lol]` | party sharing end to end: the relay in `worker/` and `tibbers/lobby.py` against it, on `wrangler dev` unless given a URL. Against the live relay it opens more connections than the zone's rate limit allows (20 in 10 s per IP), so its later checks get refused there. Deploying `worker/` is the user's call: `wrangler deploy` in it, with the token the `cloudflare` skill describes |

`dev.sh --mock` prints its own controls; drive them with
`curl -s 127.0.0.1:7778/api/mock -d '{"action":"hover","value":202}'`.

## How the pieces actually sit

- **Checkout = dev instance. Bundle = frozen snapshot.**
  `/Applications/Tibbers.app` carries its own copy of `main.py`, `tibbers/`,
  `tools/`, `assets/` and the third-party packages under
  `Contents/Resources/`, and its launcher resolves every path relative to
  itself. Editing this checkout does **not** reach the running app. Only
  `deploy.sh` changes the bundle — a full deploy for Python, `--static` for
  UI files.
- **The patcher outlives the app.** It is started detached, recorded in
  `<data dir>/work/patcher.json`, and adopted on the next start. `build_overlay`
  refuses while a patcher is serving a running game.
- **Preferences live in memory.** `Prefs` loads `preferences.json` once at
  start and writes on change, so editing that file under a running app is
  overwritten. Read and change settings through `GET`/`POST /api/settings`.
- **Logs:** `<data dir>/tibbers.log` — `~/Library/Application Support/tibbers/`
  for the live app, `.dev/home-<port>/` for a dev instance. The patcher's own
  output is `work/runoverlay.log` beside it.

## Build data

| source | how |
|--------|-----|
| op.gg (`opgg.py`) | **everything: builds, counters, every mode.** Plain `urllib`, honest user-agent, no challenge. `robots.txt` on the API host is `Disallow:` — all permitted — so keep the agent honest. One request per champion and lane carries the build and the counters together, and names its own patch, so there is no manifest to resolve. No per-matchup build exists: counters carry each matchup's win rate but no build for it |
| u.gg (`ugg.py`) | **ARAM Mayhem's augment rankings only**, on `static.bigbrain.gg`, which puts up no challenge. Mayhem has no build file, so its items stay ARAM's. The build CDN (`stats2.u.gg`) is no longer used at all: it scores the TLS handshake and refuses roughly one request in five, which is what the spoofed agent, the header sets and the downloaded curl-impersonate all existed to get past. All of that is gone — do not bring it back |
| metasrc | **off limits.** `robots.txt` names ClaudeBot `Disallow: /`, its terms forbid extraction, and reaching it needs a spoofed browser fingerprint |
| skin mods | **no network at all.** Built from the install by `skinsmith.py`. There is no download path and no skin repository is contacted; a skin that cannot be built simply has no mod |

Names and icons always come from the League client, never from these.

## Checks before you commit

```bash
.venv/bin/python -m py_compile main.py tibbers/*.py
scripts/build_app.sh                 # to dist/ only
```

One commit per finished piece. See `README.md` for the architecture and
`DESIGN.md` for why it is shaped this way.

## Versions and branches

Every version is a branch named for it -- `1.1.1`, `1.2.0` -- on GitHub. The
branches on GitHub are the record of which versions have work in progress. A
version without a branch has nothing in it, and "we'll add this in 1.2.0" is
only decided once it is written under that version in `CHANGELOG.md` on the
`1.2.0` branch and pushed.

- A version branch starts from the tip of the version before it. Work on a
  version happens on its branch; `main` is what has shipped.
- `CHANGELOG.md` has one section per version, newest first. A version in
  progress lists what has landed and, under *Planned*, what it still will;
  a change that lands on the branch adds its line in the same commit. The
  section is the release notes when the version ships.
- Betas: `scripts/release_beta.ps1` (dry run) then `-Publish` cuts
  `vX.Y.Z-beta.N` from the version branch as a GitHub pre-release. Stable
  installs never see pre-releases; a beta install follows every release and
  updates to the next beta, then to `X.Y.Z` (`update.py`). The branch keeps
  `__version__ = "X.Y.Z-beta.N"` until shipping sets `X.Y.Z`. Beta numbers
  are never reused either. Publishing one is the user's call.
- Shipping: fast-forward `main` to the branch, build, publish the GitHub
  release `vX.Y.Z` from that commit with the section as its notes. A number
  that has been published is never reused, even if the release was pulled:
  a fix after publishing is the next patch version, on its own branch.

## The website

`tibbers.lol` lives on the `site` branch (one `index.html`), not on `main`, and
is hosted on Vercel in the user's personal scope. Edit it in a worktree of that
branch; pushing `site` auto-deploys through Vercel's Git integration (its
production branch is set to `site`). `vercel deploy --prod --yes` is the manual
fallback. DNS is a Cloudflare zone.
