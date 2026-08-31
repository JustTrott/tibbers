#!/bin/bash
#
# Run Tibbers from source, in a way that cannot touch the copy you are
# playing with. This is the default path for any work on the app.
#
#   scripts/dev.sh                       # window, port 7778, own home
#   scripts/dev.sh --no-window           # headless; use a browser tab
#   scripts/dev.sh --no-window --mock    # ...and replay champ select
#   scripts/dev.sh --demo 202            # one champion's real skins
#   scripts/dev.sh --port 7790           # pick the port yourself
#
# What keeps it apart from the live instance:
#
#   TIBBERS_HOME   .dev/home-<port>, so the library, the artcache, the
#                  preferences and above all work/overlay are its own. A
#                  second instance sharing the real home would run mkoverlay
#                  over the directory the live patcher is serving from.
#   port           never 7777. Derived per instance, so two dev instances do
#                  not collide with each other either.
#   --no-inject    no overlay is built and no patcher is started. The app
#                  refuses to inject with TIBBERS_HOME set anyway; this says
#                  it twice, and the guard is in the injector rather than in
#                  this script.
#
# .dev/ is gitignored. Delete it to start from nothing.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${REPO_ROOT}/.venv/bin/python"

PORT=""
HOME_DIR=""
NO_WINDOW=0
PASS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)      PORT="$2"; shift 2 ;;
        --home)      HOME_DIR="$2"; shift 2 ;;
        --no-window) NO_WINDOW=1; shift ;;
        -h|--help)   sed -n '2,/^[^#]/p' "$0" | sed -n 's/^# \{0,1\}//p'; exit 0 ;;
        *)           PASS+=("$1"); shift ;;
    esac
done

if [[ ! -x "$PYTHON" ]]; then
    echo "No venv at ${REPO_ROOT}/.venv -- see the README setup." >&2
    exit 1
fi

free_port() {  # first free port from $1 upward, skipping 7777
    "$PYTHON" - "$1" <<'PY'
import socket, sys
start = int(sys.argv[1])
for port in range(start, start + 40):
    if port == 7777:
        continue
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
    print(port)
    break
else:
    raise SystemExit("no free port")
PY
}

if [[ -z "$PORT" ]]; then
    PORT="$(free_port 7778)"
elif [[ "$PORT" == "7777" ]]; then
    echo "dev.sh: 7777 belongs to the live instance. Pick another port." >&2
    exit 64
fi

[[ -z "$HOME_DIR" ]] && HOME_DIR="${REPO_ROOT}/.dev/home-${PORT}"
mkdir -p "$HOME_DIR"

ARGS=(--home "$HOME_DIR" --port "$PORT" --dev --no-inject)
[[ $NO_WINDOW -eq 1 ]] && ARGS+=(--no-ui)
ARGS+=("${PASS[@]+"${PASS[@]}"}")

URL="http://127.0.0.1:${PORT}"
cat <<BANNER

  picker     ${URL}/
  settings   ${URL}/settings
  state      ${URL}/api/state
  home       ${HOME_DIR}
  injector   disabled -- no overlay is built, no patcher is started
  reload     the picker reloads itself when tibbers/static changes
BANNER

MOCKING=0
[[ " ${PASS[*]-} " == *" --mock "* ]] && MOCKING=1

if [[ $MOCKING -eq 1 ]]; then
    cat <<MOCK
  mock       ${URL}/mock -- its own window, so it never covers the picker

    curl -s 127.0.0.1:${PORT}/api/mock -d '{"action":"queue","value":"arena"}'
MOCK
    cat <<'MOCK'

    launch_client  close_client   phase <None|Lobby|ChampSelect|InProgress|...>
    hover <id>     lock           start_game     end_game
    role <lane>    enemy [id]     bans           queue <rift|aram|arena|urf|...>
    download <0-100|complete|idle>                availability <all|some|none>
    patcher <idle|watching|found|patching|error>  script

MOCK
fi

# With a window, the mock client is opened beside it in the browser: the
# controls have to be reachable without covering the page they are driving.
# Headless runs print the URL and leave the choice of tab to you.
if [[ $MOCKING -eq 1 && $NO_WINDOW -eq 0 ]]; then
    ( sleep 2; open "${URL}/mock" >/dev/null 2>&1 || true ) &
fi

exec "$PYTHON" "${REPO_ROOT}/main.py" "${ARGS[@]}"
