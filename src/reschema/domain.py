"""ReSchema as a Warranted domain (M8, docs/proposals/m8-rebuild-on-warranted.md).

Checkers `function-fuzz` (S3) and `program-replay` (S4), the `experiment`
operation and the duplicate guard's normalisation (S5; task specs in
reschema.tasks). The worker image lands in S6.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from reschema.checks.function_fuzz import FunctionFuzz
from reschema.checks.program_replay import ProgramReplay
from reschema.driver import podrun
from reschema.engine import _norm_source
from reschema.operations import Experiment, ExperimentFunction

_PACKAGE = Path(__file__).resolve().parent


class ReSchemaDomain:
    name = "reschema"
    version = "0.1"
    checkers: Mapping = MappingProxyType(
        {
            "function-fuzz": FunctionFuzz(),
            "program-replay": ProgramReplay(),
        }
    )
    operations: Mapping = MappingProxyType(
        {"experiment": Experiment(), "experiment_function": ExperimentFunction()}
    )
    # Warranted pins class files only; checkers reach the recorder, canonicaliser,
    # grammars and corpus through imports, which it does not follow. Pin the
    # whole package: over-pinning costs a spurious identity change, under-pinning
    # lets checker code drift under an unchanged identity.
    sources = (_PACKAGE,)

    @staticmethod
    def normalize(candidate) -> bytes:
        """The duplicate guard's fingerprint: the model source, comment- and
        whitespace-stripped (the old flail guard's), plus the declaration
        (params.json or cases.json) in canonical JSON. Warranted counts every
        rejection, declaration rejects included, so a corrected declaration
        must be a new candidate, not a repeat of the source."""
        src = candidate.get("model.c", b"").decode(errors="replace")
        out = [_norm_source(src).encode()]
        for name in ("params.json", "cases.json"):
            raw = candidate.get(name)
            if raw is None:
                continue
            try:
                raw = json.dumps(
                    json.loads(raw), sort_keys=True, separators=(",", ":")
                ).encode()
            except (ValueError, RecursionError):
                pass  # unparseable: its raw bytes are the fingerprint
            out.append(raw)
        return b"\0".join(out)

    @property
    def worker_image(self) -> str:
        # ponytail: the toolchain image's local ID until S6 ships the worker image.
        return podrun.image_id()
