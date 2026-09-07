#!/usr/bin/env bash
# Install or update the tibbers u.gg mirror on the serving host (Ubuntu,
# Caddy already running).
#
#   sudo mirror/install.sh --push     # files arrive by rsync from a machine
#                                     # whose address u.gg accepts (push.sh)
#   sudo mirror/install.sh --sync     # this host fetches from u.gg itself
#                                     # (only if u.gg accepts its address)
#
# Idempotent: rerun after changing anything here. It lays the data directory
# down under /var/lib/tibbers-mirror, appends the Caddy site block to
# /etc/caddy/Caddyfile once (never replacing the file: other sites live
# there), and in --sync mode installs the script and the systemd timer.
# TLS is Caddy's job, automatic once data.tibbers.lol resolves here.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB=/usr/local/lib/tibbers-mirror
DATA=/var/lib/tibbers-mirror
CADDYFILE=/etc/caddy/Caddyfile
MODE="${1:-}"

case "$MODE" in
    --push|--sync) ;;
    *) echo "usage: sudo $0 --push | --sync" >&2; exit 2 ;;
esac
if [ "$(id -u)" -ne 0 ]; then
    echo "run as root (sudo $0 $MODE)" >&2
    exit 1
fi
for tool in caddy systemctl; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

# -- data directory ----------------------------------------------------------
if [ "$MODE" = "--push" ]; then
    # rsync arrives as the user who ran sudo; the tree is theirs.
    OWNER="${SUDO_USER:?run through sudo so the pushing user is known}"
else
    OWNER=tibbers-mirror
    id "$OWNER" >/dev/null 2>&1 \
        || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$OWNER"
fi
install -d -o "$OWNER" -g "$OWNER" -m 755 "$DATA" "$DATA/public"
# Caddy runs as its own user and only needs to read.
chmod 755 "$DATA" "$DATA/public"

# -- the fetcher, when this host does the fetching ----------------------------
if [ "$MODE" = "--sync" ]; then
    for tool in python3 curl; do
        command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
    done
    install -d -m 755 "$LIB"
    install -m 644 "$HERE/ugg_mirror.py" "$LIB/ugg_mirror.py"
    install -m 644 "$HERE/tibbers-mirror.service" /etc/systemd/system/tibbers-mirror.service
    install -m 644 "$HERE/tibbers-mirror.timer" /etc/systemd/system/tibbers-mirror.timer
    systemctl daemon-reload
    systemctl enable --now tibbers-mirror.timer
else
    systemctl disable --now tibbers-mirror.timer 2>/dev/null || true
fi

# -- caddy ---------------------------------------------------------------------
if ! grep -q "^data.tibbers.lol {" "$CADDYFILE"; then
    printf '\n' >> "$CADDYFILE"
    cat "$HERE/Caddyfile" >> "$CADDYFILE"
    echo "appended the data.tibbers.lol site to $CADDYFILE"
else
    # Replace the existing block in place, from our header comment to the
    # closing brace, so a rerun picks up changes without touching the rest.
    python3 - "$CADDYFILE" "$HERE/Caddyfile" <<'EOF'
import re, sys
path, ours = sys.argv[1], open(sys.argv[2]).read()
text = open(path).read()
pattern = re.compile(r"(?:^#[^\n]*\n)*^data\.tibbers\.lol \{\n.*?^\}\n", re.M | re.S)
new, n = pattern.subn(lambda m: ours, text, count=1)
if n != 1:
    sys.exit("could not find the data.tibbers.lol block to replace")
open(path, "w").write(new)
print("replaced the data.tibbers.lol site in", path)
EOF
fi
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null
systemctl reload caddy

if [ "$MODE" = "--sync" ] && [ ! -f "$DATA/public/status.json" ]; then
    echo "starting the first sync in the background (journalctl -fu tibbers-mirror)"
    systemctl start --no-block tibbers-mirror.service
fi
echo "installed ($MODE). data: $DATA/public, owner: $OWNER"
