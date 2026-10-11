"""ReSchema as a Warranted domain (M8, docs/proposals/m8-rebuild-on-warranted.md).

Checkers `function-fuzz` (S3) and `program-replay` (S4), the `experiment`
operation and the duplicate guard's normalisation (S5; task specs in
reschema.tasks), the worker image (S6).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from reschema import toolchain
from reschema.checks import candidate_files
from reschema.checks.function_fuzz import FunctionFuzz
from reschema.checks.program_replay import ProgramReplay
from reschema.operations import Experiment, ExperimentFunction

_PACKAGE = Path(__file__).resolve().parent

# A literal (kept, through its closing quote or EOF), or noise: a line
# comment, a block comment (through */ or EOF), whitespace.
_NOISE = re.compile(
    r'("(?:\\.|[^"\\])*"?|\'(?:\\.|[^\'\\])*\'?)|//[^\n]*|/\*.*?(?:\*/|\Z)|\s+',
    re.DOTALL,
)


def norm_source(src: str) -> str:
    """Comment/whitespace-stripped fingerprint, string-literal aware.

    A heuristic, not a parse: deterministic and crash-free on any input.
    `//`/`/* */` sequences INSIDE literals survive; outside they die."""
    return _NOISE.sub(lambda m: m[1] or "", src)


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
        whitespace-stripped (`norm_source`, the old flail guard's), plus the declaration
        (params.json or cases.json) in canonical JSON. Warranted counts every
        rejection, declaration rejects included, so a corrected declaration
        must be a new candidate, not a repeat of the source. Fingerprinted as
        files (`candidate_files`), so the worker's workspace is not part of it;
        a malformed result.json fingerprints as its raw bytes."""
        files = candidate_files(candidate)
        if type(files) is str:
            return b"\1" + candidate.get("result.json", b"")
        src = files.get("model.c", b"").decode(errors="replace")
        out = [norm_source(src).encode()]
        for name in ("params.json", "cases.json"):
            raw = files.get(name)
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
        # The toolchain image is the worker image too (S6: the Containerfile
        # COPYs tools/worker/'s helpers onto PATH), pinned by its local ID.
        return toolchain.image_id()
