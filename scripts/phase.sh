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
#   * whether the GAME process is up
#   * whether a Tibbers is running, on which port, and whether the patcher it
#     started would survive that Tibbers being restarted
#
# macOS and Windows both. The questions are the same on either; only the way a
# process is found differs, and each of the three sections below says how. On
# Windows the process table comes from one PowerShell call -- without it every
# answer here would be "League is not running", which is the one wrong answer
# that matters, because it is the answer that says restarting is safe.
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
# Which machine this is
# ---------------------------------------------------------------------------
#
# Everything below asks the same four questions; only the way a process is
# looked up differs. macOS reads `ps -axo command=` and matches Mach-O paths;
# Windows has no such listing in a POSIX shell, so one PowerShell call brings
# back the table and the rest of the script parses it the same way.

WINDOWS=0
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) WINDOWS=1 ;;
esac

PS_EXE=""
if [[ $WINDOWS -eq 1 ]]; then
    for candidate in powershell.exe pwsh.exe pwsh; do
        command -v "$candidate" >/dev/null 2>&1 && { PS_EXE="$candidate"; break; }
    done
fi

#: pid, name, executable and command line for the processes that could be a
#: client, a game, a Tibbers or a patcher -- tab separated, one per line.
#:
#: Filled once, here, and only read afterwards. Every consumer below reads it
#: through a pipe or a process substitution, and those run in a subshell: a
#: function that filled it lazily would be filling a copy that dies with the
#: subshell, and would pay for the query again on every use.
WIN_TABLE=""
win_table() { printf '%s\n' "$WIN_TABLE"; }

if [[ $WINDOWS -eq 1 && -n "$PS_EXE" ]]; then
    WIN_TABLE="$("$PS_EXE" -NoProfile -NonInteractive -Command '
        $names = @("LeagueClient.exe","LeagueClientUx.exe","League of Legends.exe",
                   "Tibbers.exe","tibbers.exe","python.exe","pythonw.exe",
                   "mod-tools.exe","ltk_patcher_host.exe")
        # Filtered in WQL rather than in the pipeline: the machine has hundreds
        # of processes and reading argv for all of them is most of the cost.
        $filter = ($names | ForEach-Object { "Name=""$_""" }) -join " OR "
        Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue |
            ForEach-Object {
                "{0}`t{1}`t{2}`t{3}" -f $_.ProcessId, $_.Name,
                    ($_.ExecutablePath  -replace "[`t`r`n]", " "),
                    ($_.CommandLine     -replace "[`t`r`n]", " ")
            }' 2>/dev/null | tr -d '\r')"
fi

#: The Windows path a process was started from, as a POSIX path.
to_posix() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -u "$1" 2>/dev/null
    else
        printf '%s' "$1" | sed 's|\\\\|/|g; s|^\([A-Za-z]\):|/\L\1|'
    fi
}

# ---------------------------------------------------------------------------
# The client install, and its lockfile
# ---------------------------------------------------------------------------
# A running client is authoritative (it may be installed anywhere); the two
# standard roots are the fallback. Same order as system.find_install().

client_dir=""
if [[ $WINDOWS -eq 1 ]]; then
    # Walk up from the running client to the directory that holds
    # LeagueClient.exe, exactly as tibbers/_system_windows.py does.
    while IFS=$'\t' read -r _pid name exe _cmd; do
        case "$name" in LeagueClient.exe|LeagueClientUx.exe) ;; *) continue ;; esac
        [[ -z "$exe" ]] && continue
        # Trimmed with parameter expansion rather than dirname: a subprocess
        # per level is most of this script's runtime on Windows.
        dir="$(to_posix "$exe")"
        while [[ "$dir" == */* ]]; do
            dir="${dir%/*}"
            [[ -z "$dir" ]] && break
            if [[ -f "$dir/LeagueClient.exe" ]]; then client_dir="$dir"; break; fi
        done
        [[ -n "$client_dir" ]] && break
    done < <(win_table)

    if [[ -z "$client_dir" ]]; then
        for root in "/c/Riot Games/League of Legends" \
                    "$(to_posix "${PROGRAMFILES:-C:\\Program Files}")/Riot Games/League of Legends"; do
            [[ -f "$root/LeagueClient.exe" ]] && { client_dir="$root"; break; }
        done
    fi
else
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

if [[ $WINDOWS -eq 1 ]]; then
    # The game keeps the spaces in its name here, unlike the macOS Mach-O.
    game_pid="$(win_table | awk -F'\t' '$2 == "League of Legends.exe" {print $1; exit}')"
else
    game_pid="$(pgrep -f "MacOS/LeagueofLegends" | head -1)"
fi
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
if [[ $WINDOWS -eq 1 ]]; then
    while IFS=$'\t' read -r pid name exe cmd; do
        case "$name" in
            Tibbers.exe|tibbers.exe) where="$(to_posix "$exe")" ;;
            python.exe|pythonw.exe)
                case "$cmd" in *main.py*) where="source" ;; *) continue ;; esac ;;
            *) continue ;;
        esac
        case "$cmd" in *" --dev"*) where="dev" ;; esac
        [[ "$where" == "dev" ]] && continue
        if [[ -z "$tibbers_pid" || "$where" != "source" ]]; then
            tibbers_pid="$pid"
            tibbers_where="$where"
        fi
        [[ "$where" != "source" ]] && break
    done < <(win_table)
else
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
fi

if [[ -n "$tibbers_pid" ]]; then
    if [[ $WINDOWS -eq 1 ]]; then
        # netstat is native and fast; the pid is the last column of a LISTENING
        # row, and the port is what follows the last colon of the local address.
        tibbers_port="$(netstat -ano 2>/dev/null | tr -d '\r' \
            | awk -v pid="$tibbers_pid" \
                  '$1 == "TCP" && $4 == "LISTENING" && $5 == pid {
                       sub(/.*:/, "", $2); print $2; exit }')"
    else
        # -a ANDs the selectors. Without it lsof ORs them and reports every
        # listening socket on the machine, so the first row is some other app's.
        tibbers_port="$(lsof -nP -a -p "$tibbers_pid" -iTCP -sTCP:LISTEN 2>/dev/null \
                        | awk 'NR>1 {sub(/.*:/, "", $9); print $9; exit}')"
    fi
fi

# The patcher: cslol's runoverlay, running as root. The holder shell's own
# command line repeats the whole pipeline, patcher included, so it has to be
# excluded or it counts as a second patcher.
if [[ $WINDOWS -eq 1 ]]; then
    # Windows splits the work: `mod-tools.exe` only builds the overlay, and the
    # injection is LTK's host, whose argv carries no `runoverlay` to match on.
    # So the host counts by name, and a holder -- whose own argv names the host
    # it spawned -- is excluded by its marker.
    patcher_pid="$(win_table | awk -F'\t' '
        $2 == "ltk_patcher_host.exe" && $4 !~ /tibbers-patcher-holder/ {print $1; exit}
        $2 == "mod-tools.exe" && $4 ~ /runoverlay/ && $4 !~ /tibbers-patcher-holder/ {print $1; exit}')"
else
    patcher_pid="$(ps -axo pid=,command= \
                   | grep 'mod-tools[^ ]* runoverlay ' \
                   | grep -v 'tibbers-patcher-holder' \
                   | awk '{print $1; exit}')"
fi

# Whether the patcher survives Tibbers being restarted is decided by who holds
# its stdin, not by the parent chain: the holder shell is started by the app
# and so IS its child, but in its own session, and the patcher keeps running
# when the app goes. The marker in the holder's command line is the signal.
patcher_detached=0
if [[ -n "$patcher_pid" ]]; then
    if [[ $WINDOWS -eq 1 ]]; then
        win_table | grep 'tibbers-patcher-holder' >/dev/null && patcher_detached=1
    elif ps -axo command= | grep '[t]ibbers-patcher-holder' >/dev/null; then
        # (not grep -q: under pipefail, -q exits at the first match, ps takes a
        # SIGPIPE, and the whole check fails whenever ps loses that race.)
        patcher_detached=1
    fi
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
