# LTF bridge in Tibbers

Vendored unchanged from Alban1911/Rose at
`3dc5231d5d0290f2bb6c5f6cd459d645a266dfd6`:

- `LeagueToolFlop-inj.cpp`
- `LeagueToolFlop_dll.cpp`
- `VENDORED.md` (upstream's notes, paths refer to Rose)

The Python TCP client `tibbers/ltk_bridge.py` is from the same commit;
only its logger import is adapted. `scripts/build_ltf.py` is adapted to
write into `tools/ltf`. Rose's MIT license is retained in `LICENSE.Rose`.
Upstream credits the original LTF author as `lol123love`; those source
credits are preserved. No LTK patcher binaries are included here.

The Tibbers session integration is in `tibbers/ltk_injector.py`.

Tibbers additionally passes PatcherConfig.flags=4 (OPT_OUT_AH_V1) in CmdStartPatcher, matching its original Windows backend. This makes LTK's base-skin scan advisory for generated skin0 mods. The manager and patcher binaries are unchanged.
