#!/bin/bash
#
# Build the macOS cslol `mod-tools` binaries into tools/.
#
# This is the piece that makes a macOS port tractable at all. On Windows,
# cslol's fopen hook lives in a closed-source, license-restricted
# `cslol-dll.dll` that Rose cannot ship -- which is why the Windows README
# tells users to supply and code-sign it themselves. On macOS the equivalent
# hook is compiled into mod-tools itself (patcher_macos_arm64.cpp /
# patcher_macos_amd64.cpp in cslol-manager), so there is no separate DLL and
# no signing certificate to obtain.
#
# It used to download cslol's release build. It cannot any more: since League
# 16.18 the released patcher kills the game it patches, because it allocates
# a page in the game process and the anti-cheat reacts to that. So we build
# cslol at a pinned commit with our own copies of those two files, which put
# the payload inside a function they already overwrite instead of allocating.
# `tools/patcher/README.md` is the whole story.
#
# Usage:
#   scripts/fetch_modtools.sh    # both builds; there is nothing to choose
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="${REPO_ROOT}/tools"
PATCHER_DIR="${TOOLS_DIR}/patcher"

CSLOL_REPO="https://github.com/LeagueToolkit/cslol-manager.git"
CSLOL_COMMIT="23f230858bc2359ce279e07ed129d482fe3b00bf"  # 2026-04-15, its last

# The upstream files our copies replace, as they were when the port was made.
# Checked before the copy: if upstream ever moves, the port has to be re-read
# against the new original rather than pasted over it blind.
PORTED=(
    "patcher_macos_arm64.cpp:9a4f8cfe51f971261b9fa2c02e6c1fd93aa436e66dbd3a72cf7447a9c1518170"
    "patcher_macos_amd64.cpp:e3549f74e6238f0a7d417aa67a8e41f6f77446dfef13da611cda8bd32ace0109"
)

# Both builds are installed, and neither is redundant. The cslol patcher's
# shellcode is architecture-specific and has to match the GAME process, which
# is not always this machine's architecture: a client started under Rosetta
# passes that on to the game it spawns, so an arm64 Mac can still be running
# an x86_64 game. `system.select_modtools` reads the process's Rosetta flag at
# injection time and picks the matching build.
BUILDS=("arm64:mod-tools" "x86_64:mod-tools-x86_64")

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script builds the macOS patcher and must run on macOS." >&2
    exit 1
fi

for tool in git cmake cc; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "${tool} is required to build mod-tools." >&2
        [[ "$tool" == "cmake" ]] && echo "  brew install cmake" >&2
        [[ "$tool" == "cc"    ]] && echo "  xcode-select --install" >&2
        exit 1
    fi
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SRC="${TMP}/cslol"

echo "==> Fetching cslol-manager @ ${CSLOL_COMMIT:0:8}"
mkdir -p "$SRC"
git -C "$SRC" init -q
git -C "$SRC" remote add origin "$CSLOL_REPO"
# A commit can be fetched by name only if the server offers it; a shallow
# clone of the default branch is the fallback, and it is verified either way.
if ! git -C "$SRC" fetch -q --depth 1 origin "$CSLOL_COMMIT" 2>/dev/null; then
    git -C "$SRC" fetch -q --depth 1 origin
fi
git -C "$SRC" checkout -q FETCH_HEAD
GOT="$(git -C "$SRC" rev-parse HEAD)"
if [[ "$GOT" != "$CSLOL_COMMIT" ]]; then
    echo "cslol-manager is at ${GOT}, not the pinned ${CSLOL_COMMIT}." >&2
    echo "Upstream moved. Re-read tools/patcher/ against it, then bump the pin." >&2
    exit 1
fi

echo "==> Applying our patcher"
UPSTREAM_PATCHER="${SRC}/cslol-tools/lib/lol/patcher"
for entry in "${PORTED[@]}"; do
    NAME="${entry%%:*}"
    WANT="${entry##*:}"
    HAVE="$(shasum -a 256 "${UPSTREAM_PATCHER}/${NAME}" | awk '{print $1}')"
    if [[ "$HAVE" != "$WANT" ]]; then
        echo "${NAME} upstream is not the file this port was made from:" >&2
        echo "  expected ${WANT}" >&2
        echo "  found    ${HAVE}" >&2
        echo "Re-read tools/patcher/${NAME} against it before building." >&2
        exit 1
    fi
    cp "${PATCHER_DIR}/${NAME}" "${UPSTREAM_PATCHER}/${NAME}"
    echo "    ${NAME}"
done

mkdir -p "$TOOLS_DIR"

for entry in "${BUILDS[@]}"; do
    ARCH="${entry%%:*}"
    OUTNAME="${entry##*:}"
    OUT="${TOOLS_DIR}/${OUTNAME}"
    BUILD="${TMP}/build-${ARCH}"

    echo "==> Building ${ARCH} -> ${OUTNAME}"
    # One dependency cache for both architectures: xxhash, zstd and libdeflate
    # are fetched at configure time, and there is no reason to pull them twice.
    cmake -S "${SRC}/cslol-tools" -B "$BUILD" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_OSX_ARCHITECTURES="$ARCH" \
        -DFETCHCONTENT_BASE_DIR="${TMP}/deps" >"${BUILD}.log" 2>&1 ||
        { echo "cmake configure failed; see ${BUILD}.log" >&2; cat "${BUILD}.log" >&2; exit 1; }
    cmake --build "$BUILD" --target mod-tools -j "$(sysctl -n hw.ncpu)" \
        >>"${BUILD}.log" 2>&1 ||
        { echo "build failed; tail of ${BUILD}.log:" >&2; tail -20 "${BUILD}.log" >&2; exit 1; }

    cp "${BUILD}/mod-tools" "$OUT"
    chmod +x "$OUT"

    echo "    installed: $OUT"
    file "$OUT" | sed 's/^/    /'
done

echo
echo "Both builds installed. Tibbers selects the one matching the game process"
echo "architecture at injection time (native arm64, or x86_64 under Rosetta)."
