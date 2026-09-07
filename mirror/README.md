# The u.gg mirror

`data.tibbers.lol` serves u.gg's build files to the app from a host we control.

## Why it exists

u.gg's stats are static JSON in an S3 bucket behind Cloudflare bot
protection. That protection scores the TLS handshake, and it refuses Python's
urllib every time and Windows' curl most of the time. Users on the wrong side
of it saw "403 Forbidden" in the build and counters pages, and nothing in the
app's log said why. Fetching once, from one machine whose handshake passes,
and serving the result as plain static files removes the fingerprint from
every user's path.

## What it does

`ugg_mirror.py sync` runs every four hours from a systemd timer:

- reads u.gg's version manifest and takes the newest two patches;
- for each patch, each queue tibbers reads and each champion u.gg lists,
  fetches `overview` (the build) and `matchups` (the counters) through curl,
  with a conditional request, so an unchanged file costs a 304 and no bytes;
- stores each file exactly as received, gzip-compressed, under the same path
  u.gg uses (`/lol/1.5/<endpoint>/<patch>/<queue>/<champion>/<version>.json`);
- publishes only files that gunzip to valid JSON, deletes files u.gg has
  removed, prunes patches that left the window;
- writes `manifest.json` last, so a client never learns of a patch whose
  files are not there yet, and `status.json`, whose `nextRunAt` tells the app
  when to expire its cache.

Matchup *pair* builds (`overview/.../matchups/A_B/`) are not mirrored: that is
170×170 files per queue. The app reads those from u.gg directly and falls
back to the general build when refused.

nginx serves the tree with `gzip_static always` and `gunzip on`: the .gz on
disk is the only copy, sent as-is to clients that accept gzip and inflated for
the rest. ETags come for free, so the app's revalidation is a 304.

A full first sync is about 2.5 GB across six queues and two patches; after
that a run mostly revalidates, and downloads only what u.gg regenerated
(roughly once a day per file).

## Install

On the host, from a checkout:

```
sudo mirror/install.sh
sudo certbot --nginx -d data.tibbers.lol     # once, after DNS points here
journalctl -fu tibbers-mirror                 # watch the first sync
```

Rerun `install.sh` after changing anything here; it does not touch the TLS
blocks certbot adds.

## Operate

```
systemctl list-timers tibbers-mirror.timer
systemctl start tibbers-mirror.service        # sync now
curl -s https://data.tibbers.lol/status.json | python3 -m json.tool
```

`status.json` carries counts for the last run (`fetched`, `unchanged`,
`missing`, `failed`); a non-zero `failed` means u.gg challenged the host and
the affected files were left as they were.

## Try it locally

```
python3 mirror/ugg_mirror.py sync --out /tmp/mirror/public --champions 5 --patches 1
python3 mirror/ugg_mirror.py serve /tmp/mirror/public --port 7790
TIBBERS_UGG_MIRROR=http://127.0.0.1:7790 scripts/dev.sh --no-window --mock
```

`serve` mimics nginx's gzip handling. `TIBBERS_UGG_MIRROR=` (empty) makes the
app read u.gg directly, as it did before the mirror.
