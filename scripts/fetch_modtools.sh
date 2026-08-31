#!/bin/bash
#
# Fetch the macOS cslol `mod-tools` binaries into tools/.
#
# This is the piece that makes a macOS port tractable at all. On Windows,
# cslol's fopen hook lives in a closed-source, license-restricted
# `cslol-dll.dll` that Rose cannot ship -- which is why the Windows README
# tells users to supply and code-sign it themselves. On macOS the equivalent
# hook is compiled into mod-tools itself (patcher_macos_arm64.cpp /
# patcher_macos_amd64.cpp in cslol-manager), so there is no separate DLL and
# no signing certificate to obtain.
#
# Usage:
#   scripts/fetch_modtools.sh    # both builds; there is nothing to choose
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${REPO_ROOT}/tools/mod-tools"
API="https://api.github.com/repos/LeagueToolkit/cslol-manager/releases/latest"

# Both builds are installed, and neither is redundant. The cslol patcher's
# shellcode is architecture-specific and has to match the GAME process, which
# is not always this machine's architecture: a client started under Rosetta
# passes that on to the game it spawns, so an arm64 Mac can still be running
# an x86_64 game. `system.select_modtools` reads the process's Rosetta flag at
# injection time and picks the matching build.
ASSETS=("cslol-manager-macos.tar.xz:mod-tools"
        "cslol-manager-macos-intel.tar.xz:mod-tools-x86_64")

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script fetches the macOS build and must run on macOS." >&2
    exit 1
fi

echo "==> Resolving latest cslol-manager release"
RELEASE_JSON="$(curl -fsSL "$API")"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

TOOLS_DIR="$(dirname "$DEST")"
mkdir -p "$TOOLS_DIR"

for entry in "${ASSETS[@]}"; do
    ASSET="${entry%%:*}"
    OUTNAME="${entry##*:}"
    OUT="${TOOLS_DIR}/${OUTNAME}"

    URL="$(printf '%s' "$RELEASE_JSON" | python3 -c "
import json, sys
release = json.load(sys.stdin)
want = '${ASSET}'
for asset in release['assets']:
    if asset['name'] == want:
        print(asset['browser_download_url'])
        break
else:
    sys.exit('asset not found in latest release: ' + want)
")"

    echo "==> Downloading ${ASSET} -> ${OUTNAME}"
    rm -rf "${TMP}/x"; mkdir -p "${TMP}/x"
    curl -fsSL -o "${TMP}/bundle.tar.xz" "$URL"
    tar -xf "${TMP}/bundle.tar.xz" -C "${TMP}/x"

    SRC="$(find "${TMP}/x" -name 'mod-tools' -type f | head -1)"
    if [[ -z "$SRC" ]]; then
        echo "mod-tools not found inside ${ASSET}" >&2
        exit 1
    fi

    cp "$SRC" "$OUT"
    chmod +x "$OUT"
    # Downloads carry a quarantine flag that blocks execution until cleared.
    xattr -d com.apple.quarantine "$OUT" 2>/dev/null || true

    echo "    installed: $OUT"
    file "$OUT" | sed 's/^/    /'
done

echo
echo "Both builds installed. Tibbers selects the one matching the game process"
echo "architecture at injection time (native arm64, or x86_64 under Rosetta)."
