#!/bin/bash
#
# Ship a change to the copy of Tibbers you are actually using, without
# disturbing the game you are actually playing.
#
#   scripts/deploy.sh              # build, install to /Applications, restart
#   scripts/deploy.sh --static     # UI files only: no restart at all
#   scripts/deploy.sh --force      # do it even during champ select
#   scripts/deploy.sh --dry-run    # say what it would do, change nothing
#
# The restart is quiet on purpose. `open -a` without -g steals focus and flips
# the activation policy, and the app itself used to activate as its run loop
# came up (pywebview calls activateIgnoringOtherApps_ before NSApp.run, even
# with every window hidden). --quiet suppresses that, keeps the app an
# accessory, and puts back whatever windows were on screen, where they were,
# without raising them.
#
# When it refuses
# ---------------
# It asks scripts/phase.sh first.
#
#   ChampSelect  refused. The picker is up, a skin is being armed, and the
#                overlay is being built. --force overrides.
#   InProgress   ALLOWED -- but only because the patcher is now started
#                detached and survives the app being restarted. If the running
#                Tibbers is an older build whose patcher is its own child,
#                this refuses instead, because restarting would drop the skin
#                mid-game.
#   anything else  just does it.
#
# --static copies tibbers/static into the installed bundle's own snapshot and
# asks the running app to reload its pages. No process restart, so it is
# allowed in every phase -- including champ select.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Overridable so the script can be exercised against a bundle that is not the
# one the user is playing with. Deploying for real never sets it.
APP="${TIBBERS_APP:-/Applications/Tibbers.app}"
PHASE="${REPO_ROOT}/scripts/phase.sh"

STATIC=0
FORCE=0
DRY=0
for arg in "$@"; do
    case "$arg" in
        --static)  STATIC=1 ;;
        --force)   FORCE=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,/^[^#]/p' "$0" | sed -n 's/^# \{0,1\}//p'; exit 0 ;;
        *) echo "deploy.sh: unknown option $arg" >&2; exit 64 ;;
    esac
done

run() {  # everything that changes something goes through here
    if [[ $DRY -eq 1 ]]; then
        echo "    would run: $*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Where are we in a game?
# ---------------------------------------------------------------------------

set +e
STATE_JSON="$("$PHASE" --json)"
PHASE_CODE=$?
set -e

read_json() {  # one flat key out of phase.sh --json, no jq dependency
    echo "$STATE_JSON" | sed -n "s/.*\"$1\":\([^,}]*\).*/\1/p" | tr -d '"'
}

GAME_PHASE="$(read_json phase)"
LIVE_PID="$(read_json tibbersPid)"
LIVE_PORT="$(read_json tibbersPort)"
PATCHER_PID="$(read_json patcherPid)"
PATCHER_DETACHED="$(read_json patcherDetached)"

echo "==> ${GAME_PHASE}: $(read_json verdict)"

if [[ $STATIC -eq 0 ]]; then
    case "$PHASE_CODE" in
      10)
        if [[ $FORCE -eq 0 ]]; then
            echo "    REFUSED: champ select. A restart now drops the picker and" >&2
            echo "    whatever is being armed. Wait, use --static, or --force." >&2
            exit 10
        fi
        echo "    --force: restarting during champ select anyway"
        ;;
      20)
        if [[ "$PATCHER_PID" == "null" ]]; then
            echo "    a game is running, but no patcher is -- nothing to lose"
        elif [[ "$PATCHER_DETACHED" != "true" && $FORCE -eq 0 ]]; then
            echo "    REFUSED: a game is running and the patcher (pid ${PATCHER_PID})" >&2
            echo "    is a child of the running Tibbers, so restarting would kill it" >&2
            echo "    and drop the skin. That Tibbers predates detached patchers;" >&2
            echo "    deploy once between games and this stops being a problem." >&2
            echo "    Use --static for UI changes, or --force to accept the loss." >&2
            exit 20
        else
            echo "    a game is running -- allowed, because the patcher is"
            echo "    detached and keeps serving the skin across the restart"
        fi
        ;;
    esac
fi

# ---------------------------------------------------------------------------
# --static: the UI only, with no process restart
# ---------------------------------------------------------------------------
#
# The bundle is a frozen snapshot: it carries its own copy of tibbers/static
# and serves pages from that, never from this checkout. So a UI change is two
# steps -- copy the files into the snapshot, then ask the open windows to
# reload -- and neither one restarts the process, which is what makes this
# safe during champ select.

if [[ $STATIC -eq 1 ]]; then
    SNAPSHOT="${APP}/Contents/Resources/app/tibbers/static"
    if [[ ! -d "$SNAPSHOT" ]]; then
        echo "    ${APP} has no static snapshot -- it is an older bundle that" >&2
        echo "    ran out of the checkout. Do a full deploy once." >&2
        exit 1
    fi
    echo "==> copying tibbers/static into ${SNAPSHOT}"
    run rsync -a --delete --exclude '__pycache__' \
        "${REPO_ROOT}/tibbers/static/" "${SNAPSHOT}/"

    if [[ "$LIVE_PORT" == "null" || -z "$LIVE_PORT" ]]; then
        echo "    no Tibbers running -- copied, nothing to reload"
        exit 0
    fi
    echo "==> asking the running Tibbers (:${LIVE_PORT}) to reload its pages"
    if [[ $DRY -eq 1 ]]; then
        echo "    would POST http://127.0.0.1:${LIVE_PORT}/api/reload"
        exit 0
    fi
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST --max-time 5 \
            -H 'Content-Type: application/json' -d '{}' \
            "http://127.0.0.1:${LIVE_PORT}/api/reload" || echo 000)"
    case "$code" in
      200) echo "    reloaded" ;;
      404) echo "    that Tibbers predates /api/reload -- its open windows will"
           echo "    not refresh on their own. A full deploy fixes that once." ;;
      *)   echo "    reload request failed (HTTP ${code})" >&2; exit 1 ;;
    esac
    exit 0
fi

# ---------------------------------------------------------------------------
# Build, install, restart
# ---------------------------------------------------------------------------

# One build. `build_app.sh --install` builds into dist/ and then copies that
# result to /Applications, so calling it twice -- once bare, once with
# --install -- rebuilt the whole bundle from scratch for nothing, including
# the rsync of site-packages and the icon render.
echo "==> building and installing to ${APP}"
run "${REPO_ROOT}/scripts/build_app.sh" --install

if [[ "$LIVE_PID" != "null" && -n "$LIVE_PID" ]]; then
    echo "==> stopping the running Tibbers (pid ${LIVE_PID})"
    # SIGTERM, then SIGKILL. Neither takes the patcher with it: it is in its
    # own session, held open by its own sleep.
    run kill -TERM "$LIVE_PID" 2>/dev/null || true
    if [[ $DRY -eq 0 ]]; then
        for _ in $(seq 1 16); do
            kill -0 "$LIVE_PID" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$LIVE_PID" 2>/dev/null; then
            echo "    still up after 8s; SIGKILL"
            kill -KILL "$LIVE_PID" 2>/dev/null || true
            sleep 1
        fi
    fi
    if [[ "$PATCHER_PID" != "null" && "$PATCHER_DETACHED" == "true" ]]; then
        if [[ $DRY -eq 1 ]] || kill -0 "$PATCHER_PID" 2>/dev/null; then
            echo "    patcher ${PATCHER_PID} still running, as intended"
        else
            echo "    WARNING: patcher ${PATCHER_PID} went with the app" >&2
        fi
    fi
fi

echo "==> relaunching, quietly"
# -g keeps it out of the foreground; --quiet stops the app activating itself
# and restores the windows that were open, where they were.
run open -g -a "$APP" --args --quiet

if [[ $DRY -eq 1 ]]; then
    echo "==> dry run: nothing was changed"
    exit 0
fi

PORT="${LIVE_PORT}"
[[ "$PORT" == "null" || -z "$PORT" ]] && PORT=7777
for _ in $(seq 1 30); do
    if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/api/state" >/dev/null; then
        echo "==> up on :${PORT}"
        "$PHASE"
        exit 0
    fi
    sleep 1
done

echo "    it did not answer on :${PORT} within 30s." >&2
echo "    Check ~/Library/Application Support/tibbers/tibbers.log" >&2
exit 1
