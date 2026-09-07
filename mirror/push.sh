#!/usr/bin/env bash
# Sync u.gg into a local tree, then push it to the serving host.
#
#   mirror/push.sh                      # default: ~/Library/Application Support/tibbers-mirror -> hetzner
#   MIRROR_HOST=hetzner MIRROR_DIR=/var/lib/tibbers-mirror/public mirror/push.sh
#
# Runs where u.gg accepts the address (a residential connection; the VM's
# datacenter range is refused whatever client it uses), on a schedule --
# see lol.tibbers.mirror.plist for launchd. The host only serves; install
# it with `sudo mirror/install.sh --push`.
#
# The push is in two steps so that a client never sees a manifest or status
# ahead of the files they describe: the data tree first, then the two root
# documents.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${MIRROR_HOST:-hetzner}"
REMOTE="${MIRROR_DIR:-/var/lib/tibbers-mirror/public}"
LOCAL="${MIRROR_LOCAL:-$HOME/Library/Application Support/tibbers-mirror}"
INTERVAL="${MIRROR_INTERVAL_HOURS:-4}"

mkdir -p "$LOCAL/public"
python3 "$HERE/ugg_mirror.py" sync --out "$LOCAL/public" --state "$LOCAL/state.json" \
    --interval-hours "$INTERVAL" "$@"

# Data first, root documents last. --delete keeps the host at the local
# tree's window of patches; --chmod keeps everything world-readable for Caddy.
rsync -az --delete --chmod=D755,F644 \
    "$LOCAL/public/lol/" "$HOST:$REMOTE/lol/"
rsync -az --chmod=F644 \
    "$LOCAL/public/manifest.json" "$LOCAL/public/status.json" "$HOST:$REMOTE/"
echo "pushed to $HOST:$REMOTE"
