"""`experiment`: the Warranted host-mediated operation that replaces the MCP
`experiment` tool (M8 S5, docs/proposals/m8-rebuild-on-warranted.md).

The host runs the corpus binary (a private task file the worker never holds)
on worker-chosen argv/stdin under the qiling recorder, double-recorded, and
shows the canonical trace. One `probe` unit per executed request. Traces are
hints only (D3): no checker reads them; a case the worker wants judged is
nominated in the candidate's cases.json and re-recorded by the checker.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from warranted import OperationContext, OperationResult, Outcome

from .checks.program_replay import MAX_ARG, MAX_ARGS, MAX_STDIN, _capped
from .engine import _record_stable

# warranted._operations.SHOWN_LIMIT (not exported in v0.3.0); measured compact,
# while _capped measures indented JSON, so a capped trace always fits.
SHOWN_LIMIT = 64 * 1024
_USAGE = {"probe": 1}


class Experiment:
    version = "1"
    effect = "none"  # host recording in a private temp rootfs; nothing else changes
    shared = False
    reusable = True  # double-recorded canonical traces: equal input, equal trace
    inputs = ("binary",)
    units = frozenset({"probe"})

    def parse(self, arguments):
        """Canonical {argv, stdin, quiet}: argv excludes the program, stdin is
        plain text (today's tool contract), both within the limits a nominated
        case must meet, so anything probed can be nominated."""
        if type(arguments) is not dict or not set(arguments) <= {
            "argv",
            "stdin",
            "quiet",
        }:
            raise ValueError('experiment takes {"argv"?, "stdin"?, "quiet"?}')
        argv = arguments.get("argv", [])
        stdin = arguments.get("stdin", "")
        quiet = arguments.get("quiet", False)
        if (
            type(argv) is not list
            or len(argv) > MAX_ARGS
            or not all(type(a) is str and len(a) <= MAX_ARG for a in argv)
            or any("\0" in a for a in argv)
        ):
            raise ValueError(
                f"argv is a list of at most {MAX_ARGS} strings of {MAX_ARG} chars"
            )
        if type(stdin) is not str or len(stdin.encode()) > MAX_STDIN:
            raise ValueError(f"stdin is a string of at most {MAX_STDIN} bytes")
        if type(quiet) is not bool:
            raise ValueError("quiet is a boolean")
        return {"argv": argv, "stdin": stdin, "quiet": quiet}

    def reservation(self, arguments):
        return _USAGE

    def execute(self, ctx: OperationContext, arguments) -> OperationResult:
        with tempfile.TemporaryDirectory(prefix="reschema-experiment-") as d:
            prog = Path(d) / "prog"  # canonical argv[0] is the basename, as today
            prog.write_bytes(ctx.inputs["binary"])
            t = _record_stable(prog, arguments["argv"], arguments["stdin"].encode())
        if t is None:
            return OperationResult(
                Outcome.FAILED,
                _USAGE,
                shown={"error": "flaky: two recordings of this input differ"},
            )
        shown = t
        if arguments["quiet"]:
            shown = {k: v for k, v in t.items() if k != "events"}
        if len(json.dumps(shown)) <= SHOWN_LIMIT - 1024:
            return OperationResult(Outcome.SUCCEEDED, _USAGE, shown=shown)
        # Oversized: the events go, every field is capped, the whole trace
        # travels as a read-only file beside the response.
        brief = {k: v for k, v in t.items() if k != "events"}
        brief["events_omitted"] = len(t["events"])
        return OperationResult(
            Outcome.SUCCEEDED,
            _USAGE,
            shown=_capped(brief),
            files={"trace.json": json.dumps(t).encode()},
        )
