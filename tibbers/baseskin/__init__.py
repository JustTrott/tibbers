#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Make a skin mod match the base skin's identity before it is used.

The LeagueToolkit patcher (ltk_patcher-30 and newer) rejects an overlay whose
skin0 is another skin's data -- "base-skin check: skin0 skeleton is one of ...
other skins' meshes". A skin played as the base skin (the way every skin mod
works) trips that check, so ``fix_mod`` rewrites the mod so its base skin bins
carry the base skin's identity: the skeleton/mesh are rebuilt to be
byte-distinct (mesh.py), ``championSkinName`` becomes the base skin's, and the
skin's audio tags, bank units and bank paths are rebased (steps.py).

Ported from Rose's ``injection/baseskin/`` (Alban1911/Rose, MIT), itself ported
from RoseBin (tools/rosebin).
"""

from .package import FixResult, fix_mod
from .rebase import SIDECAR, STEPS, STEPS_VERSION, rebase_mod, sidecar_commit

__all__ = ["FixResult", "fix_mod", "rebase_mod", "sidecar_commit", "SIDECAR", "STEPS", "STEPS_VERSION"]
