#!/bin/bash
#
# What is League doing right now -- so tooling can refuse to restart at the
# wrong moment.
#
# Answers three questions without needing Tibbers to be running, or Python, or
# the venv:
#
#   * the LCU gameflow phase (None / Lobby / ChampSelect / InProgress / ...),
#     read from the client's own lockfile exactly the way tibbers/lcu.py does
#   * whether the GAME process is up (pgrep -f "MacOS/LeagueofLegends")
#   * whether a Tibbers is running, on which port, and whether the patcher it
#     started would survive that Tibbers being restarted
#
#   scripts/phase.sh           # human-readable report
#   scripts/phase.sh --json    # one JSON object
#   scripts/phase.sh --quiet   # nothing on stdout; read the exit code
#
# Exit codes, so other scripts can branch:
#
#    0  safe   -- client idle, in a lobby, or post-game. Restart freely.
#   10  champ select -- NEVER restart: the picker and the arm are mid-flight.
#   20  in game  -- the game process is up (or the phase says InProgress or
#                   Reconnect). Restarting is only safe if the patcher is
#                   detached; `patcherDetached` in --json says whether it is.
#   30  no client -- League is not running at all. Restart freely.
#
# 30 is deliberately distinct from 0: "safe because nothing is happening" and
# "safe because League is not even open" are different answers to callers that
# want to skip work entirely.
set -uo pipefail

MODE=report
case "${1:-}" in
    --json)  MODE=json ;;
    --quiet) MODE=quiet ;;
    -h|--help) sed -n '2,/^[^#]/p' "$0" | sed -n 's/^# \{0,1\}//p'; exit 0 ;;
    "") ;;
    *) echo "phase.sh: unknown option $1" >&2; exit 64 ;;
esac

# ---------------------------------------------------------------------------
# The client install, and its lockfile
# ---------------------------------------------------------------------------
# A running client is authoritative (it may be installed anywhere); the two
# standard roots are the fallback. Same order as system.find_install().

client_dir=""
while read -r exe; do
    [[ -z "$exe" ]] && continue
    # .../LoL/LeagueClient.app/Contents/MacOS/LeagueClient   -> .../LoL
    # .../LoL/League of Legends.app/Contents/MacOS/LeagueClientUx -> .../LoL
    root="${exe%%/LeagueClient.app/*}"
    [[ "$root" == "$exe" ]] && root="${exe%%/League of Legends.app/Contents/MacOS/*}"
    if [[ "$root" != "$exe" && -d "$root/LeagueClient.app" ]]; then
        client_dir="$root"
        break
    fi
done < <(pgrep -f "MacOS/LeagueClient" | xargs -I{} ps -o comm= -p {} 2>/dev/null)

if [[ -z "$client_dir" ]]; then
    for root in "/Applications/League of Legends.app/Contents/LoL" \
                "$HOME/Applications/League of Legends.app/Contents/LoL"; do
        [[ -d "$root/LeagueClient.app" ]] && { client_dir="$root"; break; }
    done
fi

phase="None"
lcu_port=""
client_running=0
lockfile="${client_dir:+$client_dir/lockfile}"

if [[ -n "$lockfile" && -f "$lockfile" ]]; then
    # name:pid:port:password:protocol
    IFS=':' read -r _name _pid lcu_port password _proto < "$lockfile"
    if [[ -n "${lcu_port:-}" && -n "${password:-}" ]]; then
        client_running=1
        # The LCU serves a self-signed certificate; -k is why, and the
        # password from the lockfile is what actually authenticates.
        raw="$(curl -sk --max-time 3 -u "riot:${password}" \
               "https://127.0.0.1:${lcu_port}/lol-gameflow/v1/gameflow-phase" 2>/dev/null)"
        if [[ -n "$raw" ]]; then
            phase="${raw//\"/}"
        else
            # A lockfile with nobody answering is a client that has gone away
            # without cleaning up after itself.
            client_running=0
        fi
    fi
fi

# ---------------------------------------------------------------------------
# The game
# ---------------------------------------------------------------------------

game_pid="$(pgrep -f "MacOS/LeagueofLegends" | head -1)"
game_running=0
[[ -n "$game_pid" ]] && game_running=1

# ---------------------------------------------------------------------------
# Tibbers, and its patcher
# ---------------------------------------------------------------------------

tibbers_pid=""
tibbers_port=""
tibbers_where=""
# The installed bundle wins over anything running from source, and a dev
# instance is never reported as "the" Tibbers: deploy.sh restarts whatever
# this names, and naming a dev instance would make it kill the wrong process
# and leave the live one alone.
while read -r pid cmd; do
    [[ -z "${pid:-}" ]] && continue
    case "$cmd" in
        *Tibbers.app*)  where="/Applications/Tibbers.app" ;;
        *\ --dev*)      where="dev" ;;
        *)              where="source" ;;
    esac
    [[ "$where" == "dev" ]] && continue
    if [[ -z "$tibbers_pid" || "$where" == "/Applications/Tibbers.app" ]]; then
        tibbers_pid="$pid"
        tibbers_where="$where"
    fi
    [[ "$where" == "/Applications/Tibbers.app" ]] && break
done < <(ps -axo pid=,command= \
         | grep -E "Tibbers\.app/Contents/MacOS/Tibbers|[p]ython[0-9.]* .*main\.py" \
         | grep -v grep)

if [[ -n "$tibbers_pid" ]]; then
    # -a ANDs the selectors. Without it lsof ORs them and reports every
    # listening socket on the machine, so the first row is some other app's.
    tibbers_port="$(lsof -nP -a -p "$tibbers_pid" -iTCP -sTCP:LISTEN 2>/dev/null \
                    | awk 'NR>1 {sub(/.*:/, "", $9); print $9; exit}')"
fi

# The patcher: cslol's runoverlay, running as root. The holder shell's own
# command line repeats the whole pipeline, patcher included, so it has to be
# excluded or it counts as a second patcher.
patcher_pid="$(ps -axo pid=,command= \
               | grep 'mod-tools[^ ]* runoverlay ' \
               | grep -v 'tibbers-patcher-holder' \
               | awk '{print $1; exit}')"

# Whether the patcher survives Tibbers being restarted is decided by who holds
# its stdin, not by the parent chain: the holder shell is started by the app
# and so IS its child, but in its own session, and the patcher keeps running
# when the app goes. The marker in the holder's command line is the signal.
patcher_detached=0
if [[ -n "$patcher_pid" ]] && \
   ps -axo command= | grep '[t]ibbers-patcher-holder' >/dev/null; then
   # (not grep -q: under pipefail, -q exits at the first match, ps takes a
   # SIGPIPE, and the whole check fails whenever ps loses that race.)
    patcher_detached=1
fi

# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

if [[ "$phase" == "ChampSelect" ]]; then
    code=10; verdict="champ select -- do not restart"
elif [[ $game_running -eq 1 || "$phase" == "InProgress" || "$phase" == "Reconnect" ]]; then
    code=20
    if [[ -n "$patcher_pid" && $patcher_detached -eq 1 ]]; then
        verdict="in game -- patcher is detached, a restart keeps the skin"
    elif [[ -n "$patcher_pid" ]]; then
        verdict="in game -- patcher is a child of Tibbers, a restart DROPS the skin"
    else
        verdict="in game -- no patcher running"
    fi
elif [[ $client_running -eq 0 ]]; then
    code=30; verdict="League is not running"
else
    code=0; verdict="idle (${phase}) -- safe to restart"
fi

case "$MODE" in
  quiet) ;;
  json)
    printf '{"phase":"%s","clientRunning":%s,"lcuPort":%s,"gameRunning":%s,' \
        "$phase" "$([[ $client_running -eq 1 ]] && echo true || echo false)" \
        "${lcu_port:-null}" \
        "$([[ $game_running -eq 1 ]] && echo true || echo false)"
    printf '"gamePid":%s,"tibbersPid":%s,"tibbersPort":%s,"tibbersFrom":"%s",' \
        "${game_pid:-null}" "${tibbers_pid:-null}" "${tibbers_port:-null}" \
        "${tibbers_where:-}"
    printf '"patcherPid":%s,"patcherDetached":%s,"verdict":"%s","code":%s}\n' \
        "${patcher_pid:-null}" \
        "$([[ $patcher_detached -eq 1 ]] && echo true || echo false)" \
        "$verdict" "$code"
    ;;
  *)
    printf 'phase     %s\n' "$phase"
    printf 'client    %s\n' \
        "$([[ $client_running -eq 1 ]] && echo "running (LCU port ${lcu_port})" || echo "not running")"
    printf 'game      %s\n' \
        "$([[ $game_running -eq 1 ]] && echo "running (pid ${game_pid})" || echo "not running")"
    if [[ -n "$tibbers_pid" ]]; then
        printf 'tibbers   running (pid %s, port %s, %s)\n' \
            "$tibbers_pid" "${tibbers_port:-?}" "$tibbers_where"
    else
        printf 'tibbers   not running\n'
    fi
    if [[ -n "$patcher_pid" ]]; then
        printf 'patcher   running (pid %s, %s)\n' "$patcher_pid" \
            "$([[ $patcher_detached -eq 1 ]] && echo "detached" || echo "child of Tibbers")"
    else
        printf 'patcher   not running\n'
    fi
    printf 'verdict   %s (exit %s)\n' "$verdict" "$code"
    ;;
esac

exit $code
