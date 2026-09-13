#!/bin/bash
#
# The lobby relay (worker/) end to end, against `wrangler dev` on this
# machine: members join a room, one picks and the others see it, and every
# refusal the relay promises is tried. Nothing is deployed, no Cloudflare
# account is needed, and nothing here goes near tibbers or League.
#
#   scripts/lobby_smoke.sh                 # starts wrangler dev, stops it after
#   scripts/lobby_smoke.sh --port 8790     # ...on another port
#   scripts/lobby_smoke.sh --url URL       # against a relay already running
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER="${REPO_ROOT}/worker"
PORT=8787
URL=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --url)  URL="$2"; shift 2 ;;
        *)      echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if ! command -v node >/dev/null; then
    echo "needs node 22 or newer (for its built-in WebSocket)" >&2
    exit 1
fi

if [[ -z "$URL" ]]; then
    URL="http://127.0.0.1:${PORT}"
    if curl -s -o /dev/null "$URL/"; then
        echo "something is already listening on ${PORT}; pass --port or --url" >&2
        exit 1
    fi
    [[ -d "${WORKER}/node_modules" ]] || (cd "$WORKER" && npm ci --silent)

    LOG="$(mktemp "${TMPDIR:-/tmp}/lobby-dev.XXXXXX")"
    (cd "$WORKER" && WRANGLER_SEND_METRICS=false \
        exec node_modules/.bin/wrangler dev --ip 127.0.0.1 --port "$PORT") \
        >"$LOG" 2>&1 &
    DEV=$!
    trap 'kill "$DEV" 2>/dev/null; wait "$DEV" 2>/dev/null; rm -f "$LOG"' EXIT

    for _ in $(seq 120); do
        curl -s -o /dev/null "$URL/" && break
        if ! kill -0 "$DEV" 2>/dev/null; then
            cat "$LOG" >&2
            exit 1
        fi
        sleep 0.5
    done
    if ! curl -s -o /dev/null "$URL/"; then
        echo "wrangler dev did not come up in a minute:" >&2
        cat "$LOG" >&2
        exit 1
    fi
fi

node "${WORKER}/smoke.mjs" "$URL"
