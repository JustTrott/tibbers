"""Whether the installed game is still one our patcher can patch.

The macOS patcher does not inject a payload at some address it computes and
hopes for; it overwrites a specific function in the game, `wad_verify`, with
272 bytes: the bypass, the fopen hook, and the prefix. It finds that function
by scanning `__text` for `MOV W3, #0x126; MOV W4, #0x100` and following the
`BL` after it -- Riot's code, not ours, and free to change under us.

Two ways it can change, and both end the same way if nobody looks: the game
is patched anyway and dies at launch, before it writes a line of its own log,
which reads to the player as "League doesn't open any more" with nothing to
go on. So the same two assumptions are checked here against the binary on
disk, before a patcher is ever started:

    the scan finds exactly one match, and it lands on a call
    the function it names has room for the payload

A failure is not an error to recover from. It means this League build wants a
new port of `tools/patcher/`, and until then the honest thing is to leave the
game alone and say so.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional, Tuple

# MOV W3, #0x126 ; MOV W4, #0x100 -- the two constants set up for the call.
PATTERN = bytes((0xC3, 0x24, 0x80, 0x52, 0x04, 0x20, 0x80, 0x52))

# WAD_VERIFY_SIZE in tools/patcher/patcher_macos_arm64.cpp: the patcher writes
# this many bytes at the function, so the function has to be at least this big
# or the write lands in whatever follows it.
PAYLOAD_ROOM = 272

RET = 0xD65F03C0
CPU_TYPE_ARM64 = 0x0100000C
FAT_MAGICS = (0xCAFEBABE, 0xCAFEBABF)
GAME_BINARY = "LeagueofLegends.app/Contents/MacOS/LeagueofLegends"


def binary_path(game_dir: Path) -> Path:
    """The game executable inside a `.../LoL/Game` directory."""
    return Path(game_dir) / GAME_BINARY


# ---------------------------------------------------------------------------
# Mach-O, only as far as __text
# ---------------------------------------------------------------------------

def _arm64_slice(data: bytes) -> Optional[bytes]:
    """The arm64 image, out of a fat binary or as the whole file."""
    if len(data) < 8:
        return None
    if struct.unpack(">I", data[:4])[0] in FAT_MAGICS:
        count = struct.unpack(">I", data[4:8])[0]
        for i in range(count):
            head = data[8 + i * 20:28 + i * 20]
            if len(head) < 20:
                return None
            cputype, _sub, offset, size, _align = struct.unpack(">5I", head)
            if cputype == CPU_TYPE_ARM64:
                return data[offset:offset + size]
        return None
    if struct.unpack("<I", data[:4])[0] == 0xFEEDFACF:
        return data
    return None


def _text_section(image: bytes) -> Optional[Tuple[int, bytes]]:
    """(virtual address, bytes) of __TEXT,__text."""
    try:
        ncmds = struct.unpack("<I", image[16:20])[0]
    except struct.error:
        return None
    at = 32
    for _ in range(ncmds):
        try:
            cmd, size = struct.unpack("<II", image[at:at + 8])
        except struct.error:
            return None
        if cmd == 0x19:  # LC_SEGMENT_64
            segname = image[at + 8:at + 24].rstrip(b"\0")
            nsects = struct.unpack("<I", image[at + 64:at + 68])[0]
            sect = at + 72
            for _ in range(nsects):
                sectname = image[sect:sect + 16].rstrip(b"\0")
                if segname == b"__TEXT" and sectname == b"__text":
                    addr, length = struct.unpack("<QQ", image[sect + 32:sect + 48])
                    offset = struct.unpack("<I", image[sect + 48:sect + 52])[0]
                    return addr, image[offset:offset + length]
                sect += 80
        at += size
    return None


def scan_text(text: bytes, text_addr: int) -> Tuple[bool, str]:
    """Find the patch target in a __text section and measure its room.

    Split out from the file handling so it can be tested on bytes that are
    not a 33 MB game.
    """
    hits = []
    at = text.find(PATTERN)
    while at != -1:
        hits.append(at)
        at = text.find(PATTERN, at + 1)

    if not hits:
        return False, "the patcher's scan pattern is gone from this build"
    if len(hits) > 1:
        return False, (f"the patcher's scan pattern matches {len(hits)} places "
                       f"in this build; it patches the first, which may not be "
                       f"the right function")

    call_at = hits[0] + len(PATTERN)
    if call_at + 4 > len(text):
        return False, "the patcher's scan pattern is at the very end of __text"

    word = struct.unpack("<I", text[call_at:call_at + 4])[0]
    if word & 0xFC000000 not in (0x94000000, 0x14000000):
        return False, "what follows the patcher's scan pattern is no longer a call"

    delta = struct.unpack("<i", struct.pack("<I", (word << 6) & 0xFFFFFFFF))[0] >> 6
    target = text_addr + call_at + delta * 4
    start = target - text_addr
    if not 0 <= start < len(text):
        return False, "the call after the scan pattern leaves __text"

    # Where the function ends. Its own RET is the only boundary visible
    # without symbols, and taking the first one is the cautious reading: an
    # early return would make this look smaller than it is, and refusing a
    # function that would have fit costs skins, not a game.
    end = None
    for off in range(start, min(start + PAYLOAD_ROOM + 64, len(text) - 3), 4):
        if struct.unpack("<I", text[off:off + 4])[0] == RET:
            end = off + 4
            break
    if end is None:
        return False, (f"the function the patcher overwrites does not return "
                       f"within {PAYLOAD_ROOM + 64} bytes; this is not the "
                       f"function it was written for")

    room = end - start
    if room < PAYLOAD_ROOM:
        return False, (f"the function the patcher overwrites is {room} bytes "
                       f"in this build, and the payload needs {PAYLOAD_ROOM}")
    return True, f"patch target at {target:#x}, {room} bytes for {PAYLOAD_ROOM}"


def check(game_dir: Path) -> Tuple[bool, str]:
    """Whether this install can be patched. (ok, one line saying why.)"""
    binary = binary_path(game_dir)
    try:
        data = binary.read_bytes()
    except OSError as exc:
        return False, f"could not read the game binary: {exc}"

    image = _arm64_slice(data)
    if image is None:
        # An x86_64-only install is patched by the amd64 build, which has its
        # own target and is not what this checks.
        return True, "no arm64 slice in the game binary; not checked"

    found = _text_section(image)
    if found is None:
        return False, "no __text section in the game binary"

    return scan_text(found[1], found[0])
