# The patcher we carry

`mod-tools` is cslol's, but its macOS patcher is ours as of League 16.18.

## Why

cslol patches the running game by allocating a page in it with
`mach_vm_allocate`, writing the fopen hook there, and pointing the game's
`fopen` import stub at it. On 2026-09-10 League 16.18 shipped and every
patched launch started dying about 140 ms in -- before the game wrote its
first log line, with an empty folder left in `Logs/GameLogs` and no crash
report. Unpatched launches were fine.

The game reacts to the allocation, not to the writes: Riot's anti-cheat kills
it when an external process calls `mach_vm_allocate` on its task port, while
`mach_vm_write` into pages that already exist is still allowed.

## What the files here do differently

They drop the allocation. Everything -- the `wad_verify` bypass and the fopen
hook -- is packed into `wad_verify`'s own body, which the patcher overwrites
anyway, and the redirect prefix becomes a symlink at `/tmp/c` pointing at the
overlay so the path fits in the few bytes left over. The hook itself is
unchanged: same `fopen` interception, same `.client` suffix test, same
fallback to the original filename when the overlay has no such file.

    payload   268 bytes
    room      272 bytes  (wad_verify's body on 16.18.8159717)

## Where they come from

    upstream   LeagueToolkit/cslol-manager @ 23f230858bc2359ce279e07ed129d482fe3b00bf
    the fix    DISCOCX/cslol-manager @ 7f6f562f4863fc68c556bccb9b31691d8fba1739
               ("bypass Vanguard mach_vm_allocate block on macOS", cslol PR #473)

The files are byte-identical to that fork's, so their provenance stays
checkable:

    b284d7408beae72490eed99d7729aec7782ed029fe0d7c6776ce6af6be4009c3  patcher_macos_arm64.cpp
    f8fd7072cd9ab52cfd01dd240ea8c0f2b6beaa829d645587f8fcf4ef808d0bc0  patcher_macos_amd64.cpp

PR #473 was closed unmerged -- "completely busted", which reads as a verdict
on the pull request (269 files, a whole-repo restructure with the fix buried
in it) rather than on this one file. Upstream cslol has not moved since; its
successor, LTK Manager, is Windows-only and its own macOS patcher PR was
closed by its author to be resubmitted in pieces that have not appeared.
So this is maintained here, by us.

`scripts/fetch_modtools.sh` copies these two files over the pinned upstream
checkout and builds `mod-tools` from it. It refuses if the upstream files it
is replacing are not the ones this port was derived from:

    9a4f8cfe51f971261b9fa2c02e6c1fd93aa436e66dbd3a72cf7447a9c1518170  patcher_macos_arm64.cpp
    e3549f74e6238f0a7d417aa67a8e41f6f77446dfef13da611cda8bd32ace0109  patcher_macos_amd64.cpp

## What to check when League patches

The arm64 payload needs `wad_verify` to be at least 272 bytes, and finds it by
scanning `__text` for `MOV W3, #0x126; MOV W4, #0x100` and following the `BL`
after it. Both assumptions are the game's, not ours, so both can change.
`tibbers/patchcheck.py` checks them against the installed binary before the
patcher is ever started, and injection is refused with a readable reason
rather than the game dying at launch. When that fires, this is the file to fix.

The amd64 build is compiled and installed but has never been tested on an
Intel Mac -- the fork's author said the same. It matters only for a game
running under Rosetta.
