#!/bin/bash
#
# Build Tibbers.app -- a macOS menu bar application bundle.
#
# A frozen snapshot. main.py, the tibbers package, tools/, assets/ and the
# venv's third-party packages are all COPIED in, and the launcher resolves
# every path relative to itself, so the built app never reads this checkout.
#
# It used to run straight out of the checkout, which meant any edit here --
# including a half-finished one -- was live in the running app on its next
# page load. That is fine for scripts/dev.sh, which is meant to run from
# source; it is not fine for the copy someone is playing with.
#
# Only the skin library stays outside, in the data directory, because it is
# the user's and not the build's.
#
#   scripts/build_app.sh            # build into ./dist
#   scripts/build_app.sh --install  # and copy to /Applications
#   scripts/build_app.sh --package  # and write dist/Tibbers.zip + Tibbers.dmg
#                                   # (the two release assets)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${REPO_ROOT}/dist/Tibbers.app"
INSTALL=0
PACKAGE=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        --package) PACKAGE=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "macOS only." >&2
    exit 1
fi

if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    echo "No venv at ${REPO_ROOT}/.venv -- see README setup." >&2
    exit 1
fi

VERSION="$(sed -nE 's/^__version__ = "([^"]+)"/\1/p' "${REPO_ROOT}/tibbers/__init__.py")"
VERSION="${VERSION:-0.0.0}"

echo "==> Building ${APP} (${VERSION})"
rm -rf "$APP"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources" \
         "${APP}/Contents/Resources/app" "${APP}/Contents/Resources/lib"

cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>            <string>Tibbers</string>
  <key>CFBundleDisplayName</key>     <string>Tibbers</string>
  <key>CFBundleIdentifier</key>      <string>gg.tibbers.app</string>
  <key>CFBundleVersion</key>         <string>${VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>CFBundleExecutable</key>      <string>Tibbers</string>
  <key>CFBundleIconFile</key>        <string>Tibbers.icns</string>
  <key>NSHighResolutionCapable</key> <true/>
  <!-- Starts as a menu bar item with no Dock entry. It promotes itself to a
       regular app while a window is on screen and drops back afterwards. -->
  <key>LSUIElement</key>             <true/>
  <key>LSMinimumSystemVersion</key>  <string>11.0</string>
</dict>
</plist>
PLIST

# The framework's GUI interpreter, copied INTO the bundle.
#
# This is what makes the bundle mean anything. A venv's `python` re-execs
# itself through Python.app to get GUI access, so the running executable ends
# up outside this bundle and NSBundle.mainBundle() resolves to Python.app --
# at which point CFBundleName, the icon and LSUIElement here are all read from
# Python's plist instead of ours, and the app is called "Python" with a rocket
# for an icon. Running an interpreter that already lives in Contents/MacOS is
# what makes macOS treat this as the app it says it is.
#
# The finder is a function rather than a heredoc inside $( ). Bash 3.2 -- which
# is what /bin/bash still is on macOS -- does not understand heredocs inside a
# command substitution: it scans the region for the closing paren as plain
# text, and an apostrophe in a later comment then ends the file in the middle
# of a quote it never opened. The script parsed under Homebrew's bash 5 and
# failed with "unexpected EOF" for anyone who ran it by its shebang.
find_gui_python() {
    "${REPO_ROOT}/.venv/bin/python" - <<'FIND'
import os, sys
# Walk up from the real interpreter looking for the framework's GUI stub at
# .../Versions/3.x/Resources/Python.app/Contents/MacOS/Python. Searching beats
# counting directories, which differs between framework and non-framework
# builds.
tail = os.path.join("Resources", "Python.app", "Contents", "MacOS", "Python")
here = os.path.realpath(sys.executable)
found = ""
while True:
    parent = os.path.dirname(here)
    if parent == here:
        break
    here = parent
    candidate = os.path.join(here, tail)
    if os.path.exists(candidate):
        found = candidate
        break
print(found)
FIND
}
GUI_PYTHON="$(find_gui_python)"

SITE_PACKAGES="$("${REPO_ROOT}/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])')"

if [[ -n "$GUI_PYTHON" ]]; then
    cp "$GUI_PYTHON" "${APP}/Contents/MacOS/python"
    PY_CMD='"${HERE}/python"'
else
    echo "    note: no framework GUI interpreter found; falling back to the venv" >&2
    echo "          (the app will report itself as Python)" >&2
    PY_CMD="\"${REPO_ROOT}/.venv/bin/python\""
fi

# The application payload, copied in.
#
# Excluding __pycache__ so a stale .pyc from the checkout cannot ship, and
# excluding pip/setuptools/PyObjCTest from the packages because they are 28 of
# the 49 MB and nothing here imports them.
echo "==> Copying the application into the bundle"
rsync -a --exclude '__pycache__' \
      "${REPO_ROOT}/main.py" "${REPO_ROOT}/tibbers" "${REPO_ROOT}/assets" \
      "${APP}/Contents/Resources/app/"

# mod-tools is fetched, not committed, so its absence is a warning rather than
# a failure -- the bundle is still buildable, it just cannot inject until
# scripts/fetch_modtools.sh has been run and the app rebuilt.
if [[ -x "${REPO_ROOT}/tools/mod-tools" ]]; then
    rsync -a "${REPO_ROOT}/tools" "${APP}/Contents/Resources/app/"
else
    echo "    warning: tools/mod-tools is missing, so the built app cannot" >&2
    echo "             inject. Run scripts/fetch_modtools.sh and rebuild." >&2
fi

rsync -a --exclude '__pycache__' --exclude 'pip' --exclude 'pip-*' \
      --exclude 'setuptools' --exclude 'setuptools-*' --exclude 'pkg_resources' \
      --exclude 'PyObjCTest' --exclude '_distutils_hack' \
      "${SITE_PACKAGES}/" "${APP}/Contents/Resources/lib/"

echo "    payload: $(du -sh "${APP}/Contents/Resources" | cut -f1)"

# The launcher is a script with a shebang, NOT a shell script that execs the
# interpreter. That distinction decides whether the app gets a menu bar item.
#
# LaunchServices registers the app against the process it launched. A shell
# launcher execs a second binary over that process, the registration no longer
# matches what is running, and the window server then refuses the app any menu
# bar space: the status item is created, reports isVisible true, and is given a
# zero-height window forever. A shebang is resolved by the kernel during
# LaunchServices' own exec, so only one image is ever loaded and the identity
# holds. Launching from a terminal has no registration to invalidate, which is
# why the shell version worked there and only there.
#
# Every path in it is derived from the launcher's own location, so the bundle
# can be copied anywhere and still runs its own copy of the code. The shebang
# is the one exception -- the kernel will not resolve a relative interpreter --
# and it is rewritten on install to name the interpreter inside the installed
# bundle.
write_launcher() {
    local app="$1" python_path="$2"
    cat > "${app}/Contents/MacOS/Tibbers" <<LAUNCHER
#!${python_path}
import os, runpy, sys

CONTENTS = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
APP = os.path.join(CONTENTS, "Resources", "app")
sys.path.insert(0, os.path.join(CONTENTS, "Resources", "lib"))
sys.path.insert(0, APP)
runpy.run_path(os.path.join(APP, "main.py"), run_name="__main__")
LAUNCHER
    chmod +x "${app}/Contents/MacOS/Tibbers"
}

if [[ -n "$GUI_PYTHON" ]]; then
    write_launcher "$APP" "${APP}/Contents/MacOS/python"
else
    write_launcher "$APP" "${REPO_ROOT}/.venv/bin/python"
fi

# The Tibbers icon, upscaled into an iconset. Riot only ships it at 64x64, so
# the large Dock sizes are resampled; the menu bar uses the original.
python3 - "$APP" "${APP}/Contents/Resources/app/assets/tibbers.png" <<'ICON'
import subprocess, sys, tempfile
from pathlib import Path
app, source = Path(sys.argv[1]), Path(sys.argv[2])
try:
    from PIL import Image
except ImportError:
    sys.exit(0)
if not source.is_file():
    sys.exit(0)

src = Image.open(source).convert("RGBA")
with tempfile.TemporaryDirectory() as tmp:
    iconset = Path(tmp) / "Tibbers.iconset"
    iconset.mkdir()
    for size in (16, 32, 64, 128, 256, 512, 1024):
        # macOS insets app icons; matching that keeps it from looking oversized
        # next to every other icon in the Dock.
        pad = round(size * 0.055)
        inner = size - pad * 2
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(src.resize((inner, inner), Image.LANCZOS), (pad, pad))
        canvas.save(iconset / f"icon_{size}x{size}.png")
        if size <= 512:
            big = size * 2
            pad2 = round(big * 0.055)
            c2 = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            c2.paste(src.resize((big - pad2 * 2, big - pad2 * 2), Image.LANCZOS),
                     (pad2, pad2))
            c2.save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset),
                    "-o", str(app / "Contents/Resources/Tibbers.icns")],
                   check=False)
ICON

# Ad-hoc signature: unsigned bundles are refused outright on Apple Silicon.
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

echo "    built: ${APP}"

if [[ $PACKAGE -eq 1 ]]; then
    # Two release assets from the one bundle. Tibbers.zip is what the in-app
    # updater fetches (update.py names it), so it must keep its name. The
    # .dmg is the human download: opening it shows the app beside an
    # Applications shortcut, so installing is one drag. Both are built with
    # ditto/hdiutil rather than zipfile so symlinks and executable bits survive.
    DIST="${REPO_ROOT}/dist"
    echo "==> Packaging"
    rm -f "${DIST}/Tibbers.zip" "${DIST}/Tibbers.dmg"
    ditto -c -k --keepParent "$APP" "${DIST}/Tibbers.zip"
    echo "    ${DIST}/Tibbers.zip"

    STAGE="$(mktemp -d)"
    RW_DMG="${STAGE}.rw.dmg"
    MOUNT=""
    trap '[[ -n "$MOUNT" ]] && hdiutil detach -quiet "$MOUNT" 2>/dev/null; rm -rf "$STAGE" "$RW_DMG"' EXIT
    cp -R "$APP" "${STAGE}/"
    ln -s /Applications "${STAGE}/Applications"

    # The window people see when they open the image: the app on the left,
    # Applications on the right, an arrow between them and one line saying
    # what to do. The picture is drawn here at 2x with the app's own face and
    # palette; the layout (icon size, positions, window size) is written into
    # the volume's .DS_Store by Finder itself, which is the only thing that
    # writes that file reliably. Without Pillow the image is still built,
    # just as a plain folder.
    mkdir -p "${STAGE}/.background"
    if python3 - "${STAGE}/.background/tibbers.png" \
            "${REPO_ROOT}/tibbers/static/fonts/BeaufortforLOL-Bold.ttf" <<'BG'
import sys
from PIL import Image, ImageDraw, ImageFont
out, font_path = sys.argv[1], sys.argv[2]
W, H, S = 660, 412, 2                       # window content in points, at 2x
img = Image.new("RGB", (W * S, H * S), (17, 18, 23))            # --void
draw = ImageDraw.Draw(img)
gold, gold_dim, ink_low = (232, 206, 150), (134, 116, 78), (160, 158, 150)
# Arrow between the two icon slots. Finder draws a 128pt icon whose centre
# lands about 45pt below the "position" it is given, so y=150 puts the icons
# at ~195, which is where the arrow goes.
y = 195 * S
x0, x1 = 265 * S, 395 * S
draw.line([(x0, y), (x1 - 14 * S, y)], fill=gold, width=3 * S)
draw.polygon([(x1, y), (x1 - 22 * S, y - 11 * S), (x1 - 22 * S, y + 11 * S)], fill=gold)
try:
    big = ImageFont.truetype(font_path, 22 * S)
    small = ImageFont.truetype(font_path, 12 * S)
except OSError:
    big = small = ImageFont.load_default()
line1 = "Drag Tibbers into Applications"
line2 = "THEN RIGHT-CLICK IT ONCE AND CHOOSE OPEN"
w1 = draw.textlength(line1, font=big)
w2 = draw.textlength(line2, font=small)
draw.text(((W * S - w1) / 2, 300 * S), line1, font=big, fill=gold)
draw.text(((W * S - w2) / 2, 338 * S), line2, font=small, fill=ink_low)
img.save(out, dpi=(72 * S, 72 * S))
BG
    then
        STYLED=1
    else
        echo "    (no Pillow for python3: plain disk image)"
        STYLED=0
        rm -rf "${STAGE}/.background"
    fi

    hdiutil create -quiet -volname "Tibbers" -srcfolder "$STAGE" \
            -fs HFS+ -format UDRW -ov "$RW_DMG"
    if [[ $STYLED -eq 1 ]]; then
        # Not -nobrowse: Finder can only script a volume it can see. The
        # disk is addressed by its mount name, which is "Tibbers 1" if a
        # Tibbers image is already open, so a stale mount cannot be styled
        # by mistake.
        MOUNT="$(hdiutil attach -readwrite -noverify -noautoopen "$RW_DMG" \
                 | awk -F'\t' '/\/Volumes\//{print $NF}')"
        # Finder lays the window out and writes .DS_Store on close.
        osascript - "$(basename "$MOUNT")" >/dev/null <<'AS'
on run argv
set volName to item 1 of argv
tell application "Finder"
    tell disk volName
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {240, 160, 900, 600}
        set opts to the icon view options of container window
        set arrangement of opts to not arranged
        set icon size of opts to 128
        set text size of opts to 13
        set background picture of opts to file ".background:tibbers.png"
        set position of item "Tibbers.app" of container window to {165, 150}
        set position of item "Applications" of container window to {495, 150}
        -- Dot-folders only show for people who display hidden files; park
        -- them outside the window so even then they do not clutter it.
        set position of item ".background" of container window to {1200, 160}
        try
            set position of item ".fseventsd" of container window to {1200, 320}
        end try
        close
        open
        update without registering applications
        delay 1
        close
    end tell
end tell
end run
AS
        # The volume shows the app's own icon in the Finder sidebar and on
        # the desktop, instead of a generic disk.
        if [[ -f "${APP}/Contents/Resources/Tibbers.icns" ]]; then
            cp "${APP}/Contents/Resources/Tibbers.icns" "${MOUNT}/.VolumeIcon.icns"
            SetFile -a C "$MOUNT" 2>/dev/null || true
        fi
        # "Tibbers", not "Tibbers.app", under the icon.
        if command -v SetFile >/dev/null; then
            SetFile -a E "${MOUNT}/Tibbers.app" 2>/dev/null || true
        fi
        sync
        hdiutil detach -quiet "$MOUNT"
        MOUNT=""
    fi
    hdiutil convert -quiet "$RW_DMG" -format UDZO -o "${DIST}/Tibbers.dmg"
    echo "    ${DIST}/Tibbers.dmg"
fi

if [[ $INSTALL -eq 1 ]]; then
    echo "==> Installing to /Applications"
    rm -rf "/Applications/Tibbers.app"
    cp -R "$APP" /Applications/
    # The shebang names the interpreter by absolute path, so it has to point
    # at the copy that will actually run.
    if [[ -f "/Applications/Tibbers.app/Contents/MacOS/python" ]]; then
        write_launcher "/Applications/Tibbers.app" \
                       "/Applications/Tibbers.app/Contents/MacOS/python"
        codesign --force --deep --sign - "/Applications/Tibbers.app" >/dev/null 2>&1 || true
    fi
    echo "    installed: /Applications/Tibbers.app"
fi

echo
echo "Launch it from the Dock, Spotlight, or:"
echo "  open ${APP}"
