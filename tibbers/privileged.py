#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optional passwordless elevation for `mod-tools runoverlay`.

Without this, every skin raises the macOS authorization dialog, because
runoverlay calls task_for_pid() on the game and the kernel grants a foreign
task port only to root.

Installing the helper costs one authorization prompt, once. After that,
injection runs without prompting.

Why it is built this way
------------------------
A `NOPASSWD` sudoers rule pointed straight at `mod-tools` would be a local root
escalation for *any* process on the machine: the same binary also implements
`mkoverlay`, which writes to a destination given on the command line. So the
rule targets a small wrapper instead, and the wrapper:

* runs only `runoverlay` -- never a subcommand that writes,
* execs a *fixed*, root-owned copy of mod-tools rather than whatever is on the
  caller's PATH or in their project directory,
* refuses any overlay path outside the invoking user's own tibbers directory,
* refuses paths containing `..`.

The wrapper and the mod-tools copies are installed 0755 root:wheel. If a user
could rewrite either, the sudoers rule would hand them root, so ownership and
mode are re-verified before *every* privileged run and the code falls back to
prompting if anything has drifted.

One more bound worth noting: cslol's patcher hardcodes the process it attaches
to (`FindPid("/LeagueofLegends")`), so even the wrapper cannot be aimed at an
arbitrary process.
"""

from __future__ import annotations

import getpass
import platform
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

# /Library/PrivilegedHelperTools is the canonical macOS location for exactly
# this, is root-owned by default, and -- critically -- contains no spaces.
# sudoers treats an unescaped space as an argument separator, so a rule naming
# a path like "/Library/Application Support/..." parses as the command
# "/Library/Application" with arguments. visudo -c accepts it as valid syntax
# and it then never matches, which surfaces only as "sudo: a password is
# required" at the point of use.
HELPER_DIR = Path("/Library/PrivilegedHelperTools")
WRAPPER = HELPER_DIR / "tibbers-inject"
MODTOOLS_ARM = HELPER_DIR / "tibbers-mod-tools"
MODTOOLS_X86 = HELPER_DIR / "tibbers-mod-tools-x86_64"
SUDOERS_FILE = Path("/etc/sudoers.d/tibbers")

#: Removed on install. Earlier versions used a directory whose path contained
#: spaces (which sudoers cannot match) and a different project name.
LEGACY_PATHS = (
    Path("/Library/Application Support/lolskin"),
    Path("/Library/Application Support/tibbers"),
    Path("/Library/PrivilegedHelperTools/lolskin-inject"),
    Path("/Library/PrivilegedHelperTools/lolskin-mod-tools"),
    Path("/Library/PrivilegedHelperTools/lolskin-mod-tools-x86_64"),
    Path("/etc/sudoers.d/lolskin"),
)

WRAPPER_SOURCE = r'''#!/bin/sh
# tibbers privileged injection wrapper. Installed 0755 root:wheel.
#
#   tibbers-inject run  <arch> <overlay_dir> <config> <game_dir> <log>
#     (log is validated but redirection is the caller's job)
#   tibbers-inject stop
#
# Runs ONLY `mod-tools runoverlay`, only against an overlay inside the calling
# user's own tibbers directory, using a fixed root-owned mod-tools. This is
# what keeps the accompanying NOPASSWD sudoers rule from becoming a general
# root primitive.
set -eu

BASE="/Library/PrivilegedHelperTools"

if [ -z "${SUDO_USER:-}" ]; then
    echo "tibbers-inject: must be invoked through sudo" >&2
    exit 77
fi

case "${1:-}" in
  stop)
    /usr/bin/pkill -f "${BASE}/tibbers-mod-tools.* runoverlay" || true
    exit 0
    ;;
  run)
    ;;
  *)
    echo "tibbers-inject: unknown mode '${1:-}'" >&2
    exit 64
    ;;
esac

if [ "$#" -ne 6 ]; then
    echo "tibbers-inject: run expects 5 arguments, got $(($# - 1))" >&2
    exit 64
fi

ARCH="$2"
OVERLAY_DIR="$3"
CONFIG="$4"
GAME_DIR="$5"
LOG="$6"

case "$ARCH" in
  arm64)  MODTOOLS="${BASE}/tibbers-mod-tools" ;;
  x86_64) MODTOOLS="${BASE}/tibbers-mod-tools-x86_64" ;;
  *) echo "tibbers-inject: bad arch '${ARCH}'" >&2; exit 64 ;;
esac

CALLER_HOME=$(eval echo "~${SUDO_USER}")
ALLOWED="${CALLER_HOME}/Library/Application Support/tibbers/"

for p in "$OVERLAY_DIR" "$CONFIG" "$LOG"; do
    case "$p" in
        "$ALLOWED"*) ;;
        *) echo "tibbers-inject: path outside ${ALLOWED}" >&2; exit 77 ;;
    esac
    case "$p" in
        *..*) echo "tibbers-inject: path contains '..'" >&2; exit 77 ;;
    esac
done

if [ ! -x "$MODTOOLS" ]; then
    echo "tibbers-inject: mod-tools missing at ${MODTOOLS}" >&2
    exit 69
fi

# Foreground, inheriting stdin/stdout from the caller. runoverlay exits as
# soon as stdin reaches EOF (it treats that as "my parent died"), so it must
# NOT be detached here -- the caller holds the pipe open for as long as the
# patcher should live, and closes it to stop cleanly.
exec "$MODTOOLS" runoverlay "$OVERLAY_DIR" "$CONFIG" \
    "--game:${GAME_DIR}" --opts:configless
'''


def wrapper_digest() -> str:
    import hashlib
    return hashlib.sha256(WRAPPER_SOURCE.encode()).hexdigest()[:16]


def wrapper_is_current() -> bool:
    """Whether the installed wrapper matches this build.

    The wrapper is root-owned, so an upgrade cannot rewrite it silently --
    running a stale one would reintroduce whatever bug it was fixed for.
    """
    try:
        import hashlib
        installed_hash = hashlib.sha256(
            WRAPPER.read_bytes()).hexdigest()[:16]
        return installed_hash == wrapper_digest()
    except OSError:
        return False


def _sudoers_rule(user: str) -> str:
    if " " in str(WRAPPER):
        raise ValueError(
            f"wrapper path {WRAPPER} contains a space; sudoers would parse it "
            f"as separate arguments and the rule would never match"
        )
    return (
        "# Installed by tibbers. Grants exactly one command: the injection\n"
        "# wrapper, which validates its own arguments.\n"
        f"{user} ALL=(root) NOPASSWD: {WRAPPER}\n"
    )


def _applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_as_admin(script: str) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["osascript", "-e",
         f"do shell script {_applescript_string(script)} "
         f"with administrator privileges"],
        capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def rosetta_possible() -> bool:
    """Whether a game on this machine could be running translated.

    Only Apple Silicon runs x86_64 under Rosetta. On an Intel Mac the game is
    x86_64 natively, `is_translated` is never true, and `select_modtools`
    never reaches for the Intel build at all.
    """
    return platform.machine() == "arm64"


#: Passed as *arch* by a caller that will not exec a mod-tools build at all:
#: the wrapper's `stop` mode pkills by name. Such a call must not be turned
#: away over a missing Intel copy it was never going to run -- that would
#: raise an authorization prompt to stop a patcher the helper could have
#: stopped silently.
NO_BUILD = "stop"


def installed(arch: Optional[str] = None) -> bool:
    """Whether the helper is on disk and has the build that will be asked for.

    The wrapper chooses its mod-tools copy from the arch it is handed and
    exits 69 if that copy is not there. Reporting "installed" without it
    meant a helper that could only fail: injection took the passwordless
    path, the wrapper refused, and the app said the patcher did not start --
    mid champ select, where the honest answer was the ordinary prompt.

    Called without an *arch* -- the startup status line, `describe`, `stale`
    -- both builds are required wherever a translated game is possible, since
    there is no way yet to know which will be wanted and overclaiming is the
    expensive direction.
    """
    if not (WRAPPER.exists() and SUDOERS_FILE.exists()
            and MODTOOLS_ARM.exists()):
        return False
    if arch in (NO_BUILD, "arm64"):
        return True
    if arch == "x86_64" or rosetta_possible():
        return MODTOOLS_X86.exists()
    return True


def verify() -> Tuple[bool, str]:
    """Confirm the helper is still root-owned and not user-writable.

    Checked before every privileged run: if a user could rewrite these, the
    NOPASSWD rule would be a root escalation.
    """
    targets = [WRAPPER, MODTOOLS_ARM]
    if MODTOOLS_X86.exists():
        targets.append(MODTOOLS_X86)

    for path in targets:
        try:
            st = path.stat()
        except OSError as exc:
            return False, f"{path} unreadable ({exc})"
        if st.st_uid != 0:
            return False, f"{path} is not owned by root"
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False, f"{path} is group/world-writable"
        if not st.st_mode & stat.S_IXUSR:
            return False, f"{path} is not executable"

    for directory in (HELPER_DIR, HELPER_DIR.parent):
        try:
            dir_st = directory.stat()
        except OSError as exc:
            return False, f"{directory} unreadable ({exc})"
        if dir_st.st_uid != 0:
            return False, f"{directory} is not owned by root"
        # Sticky directories (drwxr-xr-t) are fine; only real group/other
        # write without the sticky bit lets someone swap the wrapper out.
        if (dir_st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                and not dir_st.st_mode & stat.S_ISVTX):
            return False, f"{directory} is group/world-writable"

    return True, "ok"


def available(arch: Optional[str] = None) -> bool:
    """Whether passwordless injection can be used right now.

    *arch* is the mod-tools build the caller is about to ask for; without it
    the check is the conservative one. Answering False here is not a failure
    -- it is what makes the caller fall back to prompting.
    """
    if not installed(arch):
        return False
    if not wrapper_is_current():
        return False
    ok, _ = verify()
    return ok


def stale() -> bool:
    """Installed, but from an older build."""
    return installed() and not wrapper_is_current()


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------

def install(tools_dir: Path, user: Optional[str] = None) -> Tuple[bool, str]:
    """Install the wrapper, root-owned mod-tools, and the sudoers rule.

    Raises one authorization prompt.
    """
    user = user or getpass.getuser()
    tools_dir = Path(tools_dir).resolve()

    arm = tools_dir / "mod-tools"
    x86 = tools_dir / "mod-tools-x86_64"
    if not arm.exists():
        return False, f"mod-tools not found at {arm}"

    # Staged in a private directory of our own rather than at a fixed name
    # in /tmp. What is staged here is copied to a root-owned wrapper AND to
    # the sudoers rule by an elevated cp, so anything that could substitute
    # a file between the write and that copy would be substituting root.
    # mkdtemp makes the directory 0700 and unguessable; the mode is set again
    # rather than assumed.
    staged = Path(tempfile.mkdtemp(prefix="tibbers-install-"))
    try:
        staged.chmod(0o700)
        staged_wrapper = staged / "tibbers-inject"
        staged_sudoers = staged / "sudoers"
        staged_wrapper.write_text(WRAPPER_SOURCE)
        staged_sudoers.write_text(_sudoers_rule(user))
    except OSError as exc:
        shutil.rmtree(staged, ignore_errors=True)
        return False, f"could not stage helper files: {exc}"

    q = lambda p: shlex.quote(str(p))  # noqa: E731
    steps = [
        *(f"/bin/rm -rf {q(old)}" for old in LEGACY_PATHS),
        f"/bin/mkdir -p {q(HELPER_DIR)}",
        f"/usr/sbin/chown root:wheel {q(HELPER_DIR)}",
        f"/bin/chmod 755 {q(HELPER_DIR)}",
        f"/bin/cp {q(arm)} {q(MODTOOLS_ARM)}",
        f"/usr/sbin/chown root:wheel {q(MODTOOLS_ARM)}",
        f"/bin/chmod 755 {q(MODTOOLS_ARM)}",
    ]
    if x86.exists():
        steps += [
            f"/bin/cp {q(x86)} {q(MODTOOLS_X86)}",
            f"/usr/sbin/chown root:wheel {q(MODTOOLS_X86)}",
            f"/bin/chmod 755 {q(MODTOOLS_X86)}",
        ]
    steps += [
        f"/bin/cp {q(staged_wrapper)} {q(WRAPPER)}",
        f"/usr/sbin/chown root:wheel {q(WRAPPER)}",
        f"/bin/chmod 755 {q(WRAPPER)}",
        # visudo -c validates before install: a malformed sudoers file can
        # lock the user out of sudo entirely.
        f"/usr/sbin/visudo -cf {q(staged_sudoers)}",
        f"/bin/cp {q(staged_sudoers)} {q(SUDOERS_FILE)}",
        f"/usr/sbin/chown root:wheel {q(SUDOERS_FILE)}",
        f"/bin/chmod 440 {q(SUDOERS_FILE)}",
    ]

    code, _out, err = _run_as_admin("; ".join(steps))

    shutil.rmtree(staged, ignore_errors=True)

    if code != 0:
        return False, err.strip() or "authorization cancelled"

    ok, reason = verify()
    if not ok:
        return False, f"installed but failed verification: {reason}"

    # Actually exercise the sudoers rule. A rule can be syntactically valid
    # yet never match (see the space problem above), and the failure would
    # otherwise appear only mid-champ-select as "a password is required".
    probe = subprocess.run(
        ["/usr/bin/sudo", "-n", str(WRAPPER), "stop"],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0:
        err = (probe.stderr or "").strip()
        return False, (
            f"helper installed but the sudoers rule does not grant it: {err}"
        )

    return True, "helper installed -- injection will no longer prompt"


def uninstall() -> Tuple[bool, str]:
    q = lambda p: shlex.quote(str(p))  # noqa: E731
    code, _out, err = _run_as_admin("; ".join([
        f"/bin/rm -f {q(SUDOERS_FILE)}",
        f"/bin/rm -f {q(WRAPPER)}",
        f"/bin/rm -f {q(MODTOOLS_ARM)}",
        f"/bin/rm -f {q(MODTOOLS_X86)}",
        f"/bin/rmdir {q(HELPER_DIR)} 2>/dev/null || true",
    ]))
    if code != 0:
        return False, err.strip() or "authorization cancelled"
    return True, "helper removed"


# ---------------------------------------------------------------------------
# Use
# ---------------------------------------------------------------------------

def start_runoverlay(arch: str, overlay: Path, config: Path,
                     game_dir: Path, log_path: Path,
                     detached: bool = True) -> subprocess.Popen:
    """Start the patcher through the helper, without prompting.

    runoverlay exits(0) the moment its stdin reaches EOF -- that is the
    shutdown it was designed for -- so *something* has to hold the write end
    open for as long as the patcher should live.

    ``detached=False`` makes that something this process: the returned Popen
    owns the pipe, and the patcher dies with the app. That is what made
    restarting the app mid-game drop the applied skin.

    ``detached=True`` hands the job to a `sleep` inside a shell in its own
    session, so the patcher survives the app exiting, being restarted, or
    being killed. Nothing about the elevated command changes -- same wrapper,
    same arguments, same fixed root-owned mod-tools. Only the owner of the
    pipe is different.
    """
    ok, reason = verify()
    if not ok:
        raise PermissionError(f"helper failed verification: {reason}")

    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    command = ["/usr/bin/sudo", "-n", str(WRAPPER), "run", arch,
               str(overlay), str(config), str(game_dir), str(log_path)]

    # The child gets its own duplicate of this descriptor at spawn, so the
    # parent's copy is closed the moment Popen returns. Held open, as it was,
    # every patcher start leaked one for the life of the app.
    with open(log_path, "wb") as log_file:
        if not detached:
            return subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )

        from .system import HOLDER_MARK

        inner = " ".join(shlex.quote(a) for a in command)
        # The leading `:` is a no-op whose only job is to put a findable
        # marker and the overlay path into the holder's own command line, so
        # a later run of the app can identify -- and clean up -- exactly its
        # own holder and not some other instance's.
        held = (f": {HOLDER_MARK} {shlex.quote(str(overlay))}; "
                f"exec /bin/sleep 2147483647 | exec {inner}")
        return subprocess.Popen(
            ["/bin/sh", "-c", held],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            # Its own session and process group: an app that is killed, or
            # killed by process group, cannot take the patcher with it.
            start_new_session=True,
        )


def stop_runoverlay() -> None:
    # NO_BUILD for the same reason its caller uses it: `stop` runs pkill, not
    # mod-tools, so a missing Intel copy is not a reason to leave a root
    # patcher running.
    if not available(NO_BUILD):
        return
    subprocess.run(
        ["/usr/bin/sudo", "-n", str(WRAPPER), "stop"],
        capture_output=True, text=True, timeout=30,
    )


def describe() -> str:
    lines = [f"helper:   {'installed' if installed() else 'not installed'}"]
    if installed():
        ok, reason = verify()
        lines.append(f"verified: {ok} ({reason})")
        lines.append(f"current:  {wrapper_is_current()}"
                     + ("" if wrapper_is_current()
                        else "  <- stale, re-run --install-helper"))
        lines.append(f"wrapper:  {WRAPPER}")
        lines.append(f"sudoers:  {SUDOERS_FILE}")
    return "\n".join(lines)
