#!/bin/bash
#
# Fetch the Python runtime that ships inside Tibbers.app, into runtime/python.
#
# The bundle used to carry Homebrew's framework stub as its interpreter: a
# 50 KB binary that links Python from /opt/homebrew by absolute path. It ran
# on the building machine and nowhere else. This is a relocatable CPython
# from python-build-standalone (the build uv installs): one statically
# linked executable plus the standard library, no path outside the bundle.
#
# Pinned to a release and a checksum. The version's MINOR must match the
# venv's, because build_app.sh copies the venv's site-packages -- compiled
# extensions (PyObjC, psutil, xxhash, zstandard) are built per minor version.
#
#   scripts/fetch_python.sh    # writes runtime/python, safe to re-run
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${REPO_ROOT}/runtime/python"

PBS_RELEASE="20260901"
PBS_VERSION="3.14.7"
PBS_SHA256="4632cb1a6edad9e73d3c81b6d2e69131637d995173e3e85005df14102b0592ba"
ASSET="cpython-${PBS_VERSION}+${PBS_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${ASSET}"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This fetches the macOS runtime and must run on macOS." >&2
    exit 1
fi

if [[ -x "${DEST}/bin/python3.14" ]] && \
   [[ "$("${DEST}/bin/python3.14" -c 'import platform; print(platform.python_version())')" == "$PBS_VERSION" ]]; then
    echo "runtime/python is already ${PBS_VERSION}"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> Downloading ${ASSET}"
curl -fsSL -o "${TMP}/${ASSET}" "$URL"
echo "${PBS_SHA256}  ${TMP}/${ASSET}" | shasum -a 256 -c - >/dev/null \
    || { echo "checksum mismatch for ${ASSET}" >&2; exit 1; }

echo "==> Unpacking"
tar -xzf "${TMP}/${ASSET}" -C "$TMP"
P="${TMP}/python"

# The app needs the interpreter and the standard library, nothing else.
# Headers, pkg-config, the embedding dylib and static lib, Tcl/Tk, IDLE,
# pip, the test suite: 40 MB that no import here reaches.
echo "==> Pruning"
(
    cd "$P"
    find bin -mindepth 1 ! -name 'python3.14' ! -name 'python3' ! -name 'python' -delete
    rm -rf include share lib/pkgconfig lib/libpython3.14.dylib \
           lib/libtcl* lib/tcl9* lib/tk9.0 lib/itcl* lib/thread* \
           lib/python3.14/tkinter lib/python3.14/idlelib \
           lib/python3.14/ensurepip lib/python3.14/test \
           lib/python3.14/turtledemo lib/python3.14/config-3.14-darwin \
           lib/python3.14/lib-dynload/_tkinter*
    rm -rf lib/python3.14/site-packages/*
    find . -name '__pycache__' -type d -prune -exec rm -rf {} +
)

# A quick run: the stdlib the app leans on, from the pruned tree.
"${P}/bin/python3.14" -c 'import ssl, zlib, sqlite3, json, plistlib, subprocess, ctypes, hashlib' \
    || { echo "the pruned runtime does not import its own stdlib" >&2; exit 1; }

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
mv "$P" "$DEST"
echo "    runtime/python: CPython ${PBS_VERSION}, $(du -sh "$DEST" | cut -f1)"
