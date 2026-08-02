"""ReSchema MCP tool server: strict judge, thin wrapper over engine (spec §5).

Dispatch only — no business logic here; the engine's structured dict returns are the
contract. compose() is deliberately NOT exposed.

mcp is pinned at >=2,<3: FastMCP (1.x API, which the plan's snippet targeted) is gone;
MCPServer + @tool is the current equivalent. The plan also passed hidden_seed/modes
to submit_program — engine has neither kwarg (hidden inputs take fresh entropy and
stdin-vs-argv lives in STDIN_DRIVEN), so the call is plain.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..engine import (
    STDIN_DRIVEN,
    TaskStore,
    experiment_function,
    open_function_task,
    submit_function,
    submit_program,
)
from ..validate.function import N_FUZZ

server = MCPServer("reschema")


def _err(e: KeyError) -> dict:
    """Unknown task/function is a structured answer, not a thrown fault."""
    return {"error": "not_found", "detail": e.args[0]}


def _spec_err(e: ValueError) -> dict:
    """Spec misuse (bad param decls) — an error class of its own, never not_found."""
    return {"error": "spec", "detail": str(e)}


def _internal(e: Exception) -> dict:
    """Anything else (corrupt ledger/manifest, flaky RuntimeError, ...) — structured."""
    return {"error": "internal", "detail": f"{type(e).__name__}: {e}"}


@server.tool()
def corpus_build() -> list[str] | dict:
    """Build the synthetic corpus; return task IDs.

    Compiles the seed matrix (gcc+clang x O0/O1/O2 x sym/stripped) inside the
    pinned toolchain container and writes the manifest with per-function
    addresses. Returns the task_id list ("<seed>::<cc>-<opt>-<sym|stripped>")
    usable with task_open/experiment/submit_model/status."""
    from ..corpus.generate import build

    try:
        return [t["task_id"] for t in build()]
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


@server.tool()
def task_open(task_id: str, function: str | None = None) -> dict:
    """Open a task and learn its contract.

    TWO MODES, selected by whether `function` is given:
    - program mode (no function): whole-binary task. Returns seed metadata and
      `input`: "argv" or "stdin" — where ground-truth input goes for this seed.
    - function mode (function given): one function in the binary. Returns the
      disasm slice, a heuristic signature_guess (arity/returns, labeled a
      guess — declare the falsifiable spec yourself), known callees, and an
      `abi_template` skeleton with the param-spec schema and compose rules."""
    try:
        st = TaskStore(task_id)
        if function:
            return open_function_task(st, function)
        m = st.meta
        return {
            "task_id": task_id,
            "seed": m["seed"],
            "compiler": m["compiler"],
            "opt": m["opt"],
            "stripped": m["stripped"],
            "functions": m["functions"],
            "input": "stdin" if m["seed"] in STDIN_DRIVEN else "argv",
        }
    except KeyError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


@server.tool()
def experiment(
    task_id: str,
    argv: list[str] | None = None,
    stdin: str = "",
    function: str | None = None,
    params: list[dict] | None = None,
    case: dict | None = None,
) -> dict:
    """Run a ground-truth experiment and GET the full canonical trace back.

    Program mode: supply argv (plain strings, args after the program) and/or
    stdin (plain text, NOT hex). The returned trace records the run:
    `argv` returned with "prog" prepended (your args are argv[1:]),
    `stdout`/`stderr`/`stdin_hex` all as hex strings, `exit_code` (-1 on crash/
    timeout, with a trailing fault event), `files_written` as {path: hex}
    (never touches the host fs), and the syscall `events` timeline (canonical:
    addresses -> ADDR_n, write-intent fds -> FD_n).
    Experiments persist as the task's recorded cases and are replayed at
    submit time — record the behavior you claim to model.
    Function mode: dispatch a single call against the ORIGINAL function with
    your declared params and `case` values (cstring values as hex); returns
    {ret, mem} ground truth in the same layout the validator compares."""
    try:
        st = TaskStore(task_id)
        if function:
            try:
                return experiment_function(st, function, params or [], case or {})
            except ValueError as e:
                return _spec_err(e)
        # ponytail: zero-padded — recorded() globs trace_<label>.json and sorts
        # lexicographically, so e10 must not precede e2 (upgrade: e04d at 100)
        label = f"e{len(st.recorded()):02d}"
        return st.record_case(label, argv or [], stdin.encode())
    except KeyError as e:  # unknown task (TaskStore) or function (_fn_meta)
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


@server.tool()
def submit_model(
    task_id: str,
    c_source: str,
    function: str | None = None,
    params: list[dict] | None = None,
    seed: int | None = None,
    n_fuzz: int | None = None,
) -> dict:
    """Submit a C world-model for judgment. COMPARISON CONTRACT:

    Program mode (no function): your source is compiled, then replayed against
    every recorded trace and 8 freshly-drawn hidden inputs (unguessable, new
    entropy per submission). Per case the gate compares, byte-exact after
    canonicalization: `stdout`, `stderr`, `exit_code`, `files_written` paths +
    bytes, and the write-family event SHAPE (fd, count per syscall). Reasons
    for rejection: compile, io-mismatch, files-mismatch, event-divergence/
    event-length, hidden-starvation; every rejection comes with a structured
    divergence payload.
    Function mode: your source is compiled and differential-fuzzed against the
    ORIGINAL function on per-call {ret, mem} over N_FUZZ random cases drawn
    with fresh entropy every submission (seed= pins the draw for determinism;
    n_fuzz raises the budget but is FLOORED at N_FUZZ=64 at this boundary —
    you may not tune your own judge down). A model that segfaults or hangs a
    case is rejected as a crash. Wrong memory direction or a no-op against a
    void spec with no memory channel is rejected too.
    Accepted models enter the task ledger (see status) and compose per-TU at
    compose time: helpers used by one function must be `static`."""
    try:
        st = TaskStore(task_id)
        if function:
            # Budget floor lives at the agent boundary only: internal callers
            # (engine/tests) keep n_fuzz as given. Read N_FUZZ at call time.
            kw = {"seed": seed} | (
                {}
                if n_fuzz is None
                else {"n_fuzz": max(N_FUZZ, min(n_fuzz, 4 * N_FUZZ))}
            )
            return submit_function(st, function, params or [], c_source, **kw)
        return submit_program(st, c_source)
    except KeyError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


@server.tool()
def status(task_id: str) -> dict:
    """Progress and accounting for a task.

    Returns recorded_cases (how many stored traces submit_model replays
    against) and the ledger: `submissions`/`rejections` counters (program and
    function paths both accounted),     `accepted` entries — "program" markers and
    `{<function>: source}` dicts — and `audit` seeds. The ledger persists
    across runs by design (accepted work is cumulative task state, not a
    session artifact); do not read a clean ledger as a fresh task."""
    try:
        st = TaskStore(task_id)
        return {
            "task_id": task_id,
            "recorded_cases": len(st.recorded()),
            "ledger": st.ledger(),
        }
    except KeyError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


def main():
    server.run()  # stdio


if __name__ == "__main__":
    main()
