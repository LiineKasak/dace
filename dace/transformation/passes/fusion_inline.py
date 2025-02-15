# Copyright 2019-2022 ETH Zurich and the DaCe authors. All rights reserved.
"""
Contains implementations of SDFG inlining and state fusion passes.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from dace import SDFG
from dace.sdfg.utils import fuse_states, inline_sdfgs
from dace.transformation import pass_pipeline as ppl


@dataclass(unsafe_hash=True)
class FuseStates(ppl.Pass):
    """
    Fuses all possible states of an SDFG (and all sub-SDFGs).
    """
    permissive: bool = False  #: If True, ignores some race conditions checks
    progress: Optional[bool] = None  #: Whether to print progress, or None for default (print after 5 seconds)

    def should_reapply(self, modified: ppl.Modifies) -> bool:
        return modified & (ppl.Modifies.States | ppl.Modifies.InterstateEdges)

    def modifies(self) -> ppl.Modifies:
        return ppl.Modifies.States

    def apply_pass(self, sdfg: SDFG, _: Dict[str, Any]) -> Optional[int]:
        """
        Fuses all possible states of an SDFG (and all sub-SDFGs).
        :param sdfg: The SDFG to transform.
    
        :return: The total number of states fused, or None if did not apply.
        """
        fused = fuse_states(sdfg, self.permissive, self.progress)
        return fused or None

    def report(self, pass_retval: int) -> str:
        return f'Fused {pass_retval} states.'


@dataclass(unsafe_hash=True)
class InlineSDFGs(ppl.Pass):
    """
    Inlines all possible nested SDFGs (and sub-SDFGs).
    """
    permissive: bool = False  #: If True, ignores some checks on inlining
    progress: Optional[bool] = None  #: Whether to print progress, or None for default (print after 5 seconds)
    multistate: bool = True  #: If True, include multi-state inlining

    def should_reapply(self, modified: ppl.Modifies) -> bool:
        return modified & (ppl.Modifies.NestedSDFGs | ppl.Modifies.States)

    def modifies(self) -> ppl.Modifies:
        return ppl.Modifies.States | ppl.Modifies.NestedSDFGs

    def apply_pass(self, sdfg: SDFG, _: Dict[str, Any]) -> Optional[int]:
        """
        Fuses all possible states of an SDFG (and all sub-SDFGs).
        :param sdfg: The SDFG to transform.
    
        :return: The total number of states fused, or None if did not apply.
        """
        inlined = inline_sdfgs(sdfg, self.permissive, self.progress, self.multistate)
        return inlined or None

    def report(self, pass_retval: int) -> str:
        return f'Inlined {pass_retval} SDFGs.'
