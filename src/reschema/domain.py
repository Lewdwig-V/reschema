"""ReSchema as a Warranted domain (M8, docs/proposals/m8-rebuild-on-warranted.md).

Skeleton (S2): the checkers land in S3 (`function-fuzz`) and S4
(`program-replay`), the `experiment` operation in S5, the worker image in S6.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from reschema.checks.program_replay import ProgramReplay
from reschema.driver import podrun

_PACKAGE = Path(__file__).resolve().parent


class ReSchemaDomain:
    name = "reschema"
    version = "0.1"
    checkers: Mapping = MappingProxyType({"program-replay": ProgramReplay()})
    # Warranted pins class files only; checkers reach the recorder, canonicaliser,
    # grammars and corpus through imports, which it does not follow. Pin the
    # whole package: over-pinning costs a spurious identity change, under-pinning
    # lets checker code drift under an unchanged identity.
    sources = (_PACKAGE,)

    @property
    def worker_image(self) -> str:
        # ponytail: the toolchain image's local ID until S6 ships the worker image.
        return podrun.image_id()
