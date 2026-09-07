#!/usr/bin/env bash
# Install or update the tibbers u.gg mirror on a Debian/Ubuntu host.
#
#   sudo mirror/install.sh            # from a checkout on the host
#
# Idempotent: rerun after changing anything in this directory. It puts the
# script under /usr/local/lib/tibbers-mirror, the data under
# /var/lib/tibbers-mirror, installs the systemd timer and the nginx site, and
# starts the first sync in the background (it takes a while; follow it with
# `journalctl -fu tibbers-mirror`). TLS is a separate, one-time step:
#
#   sudo certbot --nginx -d data.tibbers.lol
#
# once DNS points here.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB=/usr/local/lib/tibbers-mirror
DATA=/var/lib/tibbers-mirror
USER_NAME=tibbers-mirror

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root (sudo $0)" >&2
    exit 1
fi

for tool in python3 curl nginx systemctl; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

id "$USER_NAME" >/dev/null 2>&1 \
    || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$USER_NAME"

install -d -o "$USER_NAME" -g "$USER_NAME" -m 755 "$DATA" "$DATA/public"
install -d -m 755 "$LIB"
install -m 644 "$HERE/ugg_mirror.py" "$LIB/ugg_mirror.py"

install -m 644 "$HERE/tibbers-mirror.service" /etc/systemd/system/tibbers-mirror.service
install -m 644 "$HERE/tibbers-mirror.timer" /etc/systemd/system/tibbers-mirror.timer
systemctl daemon-reload
systemctl enable --now tibbers-mirror.timer

# nginx: only lay the site down when certbot has not rewritten it yet, so a
# rerun does not throw the TLS blocks away.
SITE=/etc/nginx/sites-available/tibbers-mirror
if [ ! -f "$SITE" ] || ! grep -q "ssl_certificate" "$SITE"; then
    install -m 644 "$HERE/nginx.conf" "$SITE"
fi
ln -sf "$SITE" /etc/nginx/sites-enabled/tibbers-mirror
nginx -t
systemctl reload nginx

if [ ! -f "$DATA/public/status.json" ]; then
    echo "starting the first sync in the background"
    systemctl start --no-block tibbers-mirror.service
fi

echo "installed. timer: $(systemctl list-timers tibbers-mirror.timer --no-pager | sed -n 2p)"
