#!/bin/bash
#
# One-shot setup: everything needed to go from a fresh clone to
# /Applications/Tibbers.app. Safe to re-run; it rebuilds from the current
# checkout each time.
#
#   scripts/setup.sh              # venv, deps, mod-tools, build + install
#   scripts/setup.sh --no-install # stop at dist/, don't touch /Applications
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

INSTALL=1
[[ "${1:-}" == "--no-install" ]] && INSTALL=0

echo "==> Python virtualenv (.venv)"
if [[ ! -x ".venv/bin/python" ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet psutil xxhash zstandard

echo "==> mod-tools (from cslol-manager)"
scripts/fetch_modtools.sh

if [[ $INSTALL -eq 1 ]]; then
    echo "==> Build and install to /Applications"
    scripts/build_app.sh --install
    echo
    echo "Done. Open Tibbers from Spotlight or /Applications."
else
    echo "==> Build to dist/ only"
    scripts/build_app.sh
    echo
    echo "Done. The app is in dist/Tibbers.app."
fi
