# The u.gg mirror

`data.tibbers.lol` serves u.gg's build files to the app from a host we control.

## Why it exists

u.gg's stats are static JSON in an S3 bucket behind Cloudflare bot
protection. That protection scores the TLS handshake **and the client's
address**: it refuses Python's urllib every time, Windows' curl most of the
time, and anything at all from a datacenter range. Users on the wrong side of
it saw "403 Forbidden" in the build and counters pages, and nothing in the
app's log said why. Fetching once, from one connection u.gg accepts, and
serving the result as plain static files removes both checks from every
user's path.

## Shape

Two roles, usually on two machines:

- **The fetcher** (`ugg_mirror.py sync`, wrapped by `push.sh`) runs where
  u.gg accepts the address. Measured on 2026-09-07: a home connection passes,
  the Hetzner VM is refused with the very same curl through a SOCKS tunnel,
  so the VM cannot fetch for itself. `push.sh` syncs into a local tree and
  rsyncs it to the host, data first and the two root documents last.
- **The host** (Caddy on the VM) only serves. `install.sh --push` lays the
  directory down and appends the site block to `/etc/caddy/Caddyfile`.

If a host ever turns up whose address u.gg accepts, `install.sh --sync` puts
the fetcher there under a systemd timer instead.

## What the sync does

- reads u.gg's version manifest and takes the newest two patches;
- for each patch, each queue tibbers reads and each champion u.gg lists,
  fetches `overview` (the build) and `matchups` (the counters) through curl,
  with a conditional request, so an unchanged file costs a 304 and no bytes;
- stores each file exactly as received, gzip-compressed, under the same path
  u.gg uses (`/lol/1.5/<endpoint>/<patch>/<queue>/<champion>/<version>.json`);
- publishes only files that gunzip to valid JSON, deletes files u.gg has
  removed, prunes patches that left the window;
- writes `manifest.json` last, so a client never learns of a patch whose
  files are not there yet, and `status.json`, whose `nextRunAt` (one interval
  after the run started) tells the app when to expire its cache.

Matchup *pair* builds (`overview/.../matchups/A_B/`) are not mirrored: that is
170×170 files per queue. The app reads those from u.gg directly and falls
back to the general build when refused.

Caddy rewrites a request for `x.json` to the `x.json.gz` on disk and sends it
with `Content-Encoding: gzip` regardless of `Accept-Encoding` (tibbers and
every browser accept gzip; Caddy's own `precompressed` would need a plain
copy beside every file). ETags come for free, so the app's revalidation is a
304.

A full first sync is about 2.5 GB across six queues and two patches; after
that a run mostly revalidates and downloads what u.gg regenerated, roughly
once a day per file. One ranked queue for one patch measured 164 MB in 150 s.

## Install

On the host (once, and again after changing `Caddyfile` or `install.sh`):

```
scp -r mirror hetzner:~/tibbers-mirror
ssh hetzner sudo ~/tibbers-mirror/install.sh --push
```

DNS: an `A` record `data.tibbers.lol` to the host. Caddy issues the
certificate on the first request after that.

On the fetching Mac:

```
mirror/push.sh                                        # once by hand, to watch it
sed "s|@REPO@|$PWD|" mirror/lol.tibbers.mirror.plist > ~/Library/LaunchAgents/lol.tibbers.mirror.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/lol.tibbers.mirror.plist
```

`push.sh` reads `MIRROR_HOST` (default `hetzner`), `MIRROR_DIR`,
`MIRROR_LOCAL` and `MIRROR_INTERVAL_HOURS`; anything else on its command
line goes to `ugg_mirror.py sync` (`--queues`, `--patches`, `--champions`).

## Operate

```
curl -s https://data.tibbers.lol/status.json | python3 -m json.tool
tail -f /tmp/lol.tibbers.mirror.log                   # on the Mac
launchctl kickstart -k gui/$(id -u)/lol.tibbers.mirror  # push now
ssh hetzner tail -f /var/log/caddy/tibbers-mirror.log
```

`status.json` carries counts for the last run (`fetched`, `unchanged`,
`missing`, `failed`); a non-zero `failed` means u.gg challenged the fetcher
and the affected files were left as they were.

## Try it locally

```
python3 mirror/ugg_mirror.py sync --out /tmp/mirror/public --champions 5 --patches 1
python3 mirror/ugg_mirror.py serve /tmp/mirror/public --port 7790
TIBBERS_UGG_MIRROR=http://127.0.0.1:7790 scripts/dev.sh --no-window --mock
```

`serve` mimics the Caddy site. `TIBBERS_UGG_MIRROR=` (empty) makes the app
read u.gg directly, as it did before the mirror.
