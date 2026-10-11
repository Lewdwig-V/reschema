"""`experiment`: the Warranted host-mediated operation that replaces the MCP
`experiment` tool (M8 S5, docs/proposals/m8-rebuild-on-warranted.md).

The host runs the corpus binary (a private task file the worker never holds)
on worker-chosen argv/stdin under the qiling recorder, double-recorded, and
shows the canonical trace. One `probe` unit per executed request. Traces are
hints only (D3): no checker reads them; a case the worker wants judged is
nominated in the candidate's cases.json and re-recorded by the checker.

`experiment_function` (function tasks) is the MCP tool's function mode: one
call of the task's original function, the target fixed by private task.json.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from warranted import OperationContext, OperationResult, Outcome

from .checks.function_fuzz import MAX_BUFFER_LEN, _params
from .checks.program_replay import MAX_ARG, MAX_ARGS, MAX_STDIN, _capped
from .driver.calling import BAREGS, _guard_arity, call_original
from .driver.spec import Param
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


_I32 = range(-(2**31), 2**31)


def _case(case, params) -> dict:
    """The worker's one case, checked before any host allocation: every param
    named exactly once, i32s in range, buffers of at most MAX_BUFFER_LEN cells
    (a list, or an int length for an out buffer), cstrings NUL-terminated hex of
    at most MAX_ARG bytes."""
    if type(case) is not dict or set(case) != {p.name for p in params}:
        raise ValueError("case needs exactly one value per param name")
    for p in params:
        v = case[p.name]
        if p.kind == "i32":
            ok = type(v) is int and v in _I32
        elif p.kind == "buffer_i32":
            ok = (type(v) is int and 0 <= v <= MAX_BUFFER_LEN) or (
                type(v) is list
                and len(v) <= MAX_BUFFER_LEN
                and all(type(x) is int and x in _I32 for x in v)
            )
        else:  # cstring
            try:
                b = bytes.fromhex(v) if type(v) is str else None
            except ValueError:
                b = None
            ok = b is not None and 0 < len(b) <= MAX_ARG and b.endswith(b"\0")
        if not ok:
            raise ValueError(
                f"case value for {p.name!r} ({p.kind}): i32 in range, buffer_i32 a "
                f"list or length of at most {MAX_BUFFER_LEN} cells, cstring "
                f"NUL-terminated hex of at most {MAX_ARG} bytes"
            )
    return dict(case)


class ExperimentFunction:
    """One call of the task's original function on a worker-chosen case. The
    target (name, addr, size) comes from the private task.json, never from the
    worker. Operations are per domain in Warranted, so program tasks offer it
    too: their task.json names no function, and the call is FAILED, uncharged."""

    version = "1"
    effect = "none"  # qiling on the host, fresh empty rootfs per call
    shared = False
    reusable = True  # called twice on fresh VMs; a mismatch is FAILED, not shown
    inputs = ("binary", "task.json")
    units = frozenset({"probe"})

    def parse(self, arguments):
        if type(arguments) is not dict or set(arguments) != {"params", "case"}:
            raise ValueError('experiment_function takes exactly {"params", "case"}')
        try:
            params = _params(json.dumps(arguments["params"]).encode())
            _guard_arity(params)
        except NotImplementedError:
            raise ValueError(f"at most {len(BAREGS)} params") from None
        except (LookupError, ValueError, TypeError, RecursionError) as e:
            raise ValueError(f"params: {e}") from None
        return {
            "params": [p.to_json() for p in params],
            "case": _case(arguments["case"], params),
        }

    def reservation(self, arguments):
        return _USAGE

    def execute(self, ctx: OperationContext, arguments) -> OperationResult:
        fn = json.loads(ctx.inputs["task.json"]).get("function")
        if fn is None:
            # Operations are per domain, so program tasks list this one too; a
            # call there runs nothing and is not charged.
            return OperationResult(
                Outcome.FAILED,
                {"probe": 0},
                shown={"error": "experiment_function needs a function task"},
            )
        params = [Param.from_json(p) for p in arguments["params"]]
        case = {
            p.name: bytes.fromhex(v) if p.kind == "cstring" else v
            for p in params
            for v in (arguments["case"][p.name],)
        }
        with tempfile.TemporaryDirectory(prefix="reschema-experiment-") as d:
            prog = Path(d) / "prog"
            prog.write_bytes(ctx.inputs["binary"])
            a, b = (
                _hexed(call_original(str(prog), fn["addr"], params, case))
                for _ in range(2)
            )
        if a != b:
            return OperationResult(
                Outcome.FAILED,
                _USAGE,
                shown={"error": "flaky: two calls on this case differ"},
            )
        if len(json.dumps(a)) > SHOWN_LIMIT - 1024:
            a = _capped(a)  # a fault message is the only unbounded field
        return OperationResult(Outcome.SUCCEEDED, _USAGE, shown=a)


def _hexed(t: dict) -> dict:
    """cstring mem comes back as bytes: plain hex, as today's tool shows it."""
    mem = {
        k: v.hex() if isinstance(v, bytes | bytearray) else v
        for k, v in t["mem"].items()
    }
    return {**t, "mem": mem}
