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
    status_snapshot,
    submit_function,
    submit_program,
)
from ..memory import present, read_family
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


def _next_label(count: int) -> str:
    """4-digit-padded trace label (#47): recorded() globs trace_<label>.json
    and sorts lexicographically, so the pad must keep case order past 99."""
    return f"e{count:04d}"


@server.tool()
def corpus_build(
    seed_ids: list[str] | None = None, matrix: list[str] | None = None
) -> list[str] | dict:
    """Build corpus slots; return the built task IDs.

    Compiles inside the pinned toolchain container and merges the manifest with
    per-function addresses. Optional targeting: `seed_ids` (e.g. ["rot13"])
    and/or `matrix` slot selectors ("<cc>-<opt>-<sym|stripped>", e.g.
    ["gcc-O2-sym"]); omitted params build the full matrix (previous behavior).
    Returns the task_id list of what was built."""
    from ..corpus.generate import build

    try:
        return [t["task_id"] for t in build(seed_ids=seed_ids, matrix=matrix)]
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
      guess — declare the falsifiable spec yourself), known callees, an
      `abi_template` skeleton with the param-spec schema and compose rules,
      `memory` (family deduction-cache matches for this {seed, function}, each
      with provenance tier), and when the task has rejection history,
      `repair_directive` (two-pass coaching: abstract bit-logic repair FIRST,
      idiomatic/semantic refinement only after acceptance — guidance, not a
      verified fact).
    Memory presentation (both modes): on a non-empty cache, `memory_provenance`
      is constant harness framing (verified facts are harness-written, not
      agent-claimed); when the cache holds a verified_fact, `ready_to_submit`
      carries its source (plus params in function mode) as a copy-paste card.
    """
    try:
        st = TaskStore(task_id)
        if function:
            return open_function_task(st, function)
        m = st.meta
        mem = read_family(m["seed"], fn="__main__")
        return {
            "task_id": task_id,
            "seed": m["seed"],
            "compiler": m["compiler"],
            "opt": m["opt"],
            "stripped": m["stripped"],
            "functions": m["functions"],
            "input": "stdin" if m["seed"] in STDIN_DRIVEN else "argv",
            "memory": mem,
            # #92/#93: presentation tier, additive over the raw memory list
            **present(mem),
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
    quiet: bool = False,
) -> dict:
    """Run a ground-truth experiment and GET the canonical trace back.

    Program mode: supply argv (plain strings, args after the program) and/or
    stdin (plain text, NOT hex). The returned trace records the run:
    `argv` returned with "prog" prepended (your args are argv[1:]),
    `stdout`/`stderr`/`stdin_hex` all as hex strings, `exit_code` (-1 on crash/
    timeout, with a trailing fault event), `files_written` as {path: hex}
    (never touches the host fs), and the syscall `events` timeline (canonical:
    addresses -> ADDR_n, write-intent fds -> FD_n).
    `quiet=true` trims the RESPONSE (drops the events timeline — most of the
    token weight) when you only need the io/files/exit summary; storage keeps
    full events regardless (the replay gate needs them).
    Experiments persist as the task's recorded cases and are replayed at
    submit time — record the behavior you claim to model.
    Function mode: dispatch a single call against the ORIGINAL function with
    your declared params and `case` values (cstring values as hex); returns
    {ret, mem} ground truth in the same layout the validator compares.
    Function experiments persist no trace file and count exactly one probe in
    the ledger."""
    try:
        st = TaskStore(task_id)
        if function:
            try:
                return experiment_function(st, function, params or [], case or {})
            except ValueError as e:
                return _spec_err(e)
        label = _next_label(len(st.recorded()))
        t = st.record_case(label, argv or [], stdin.encode())
        return {k: v for k, v in t.items() if k != "events"} if quiet else t
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
    notes: list[str] | None = None,
) -> dict:
    """Submit a C world-model for judgment. COMPARISON CONTRACT:

    Program mode (no function): your source is compiled, then replayed in two
    STAGES: `recorded` (your stored experiment traces), then `hidden` (8 fresh
    inputs drawn with fresh entropy per submission — passes recorded only tell
    you nothing yet). Per case the gate compares, byte-exact after
    canonicalization: `stdout`, `stderr`, `exit_code`, `files_written` paths +
    bytes, and the write-family event SHAPE (fd, count per syscall).
    Rejections report the FIRST divergence per stage, by design: extraction of
    the recorded corpus is priced in submissions, and the hidden gate
    backstops overfitting the revealed prefix.
    Reasons for rejection: compile, io-mismatch, files-mismatch,
    event-divergence/event-length, hidden-starvation — behavior divergences
    (io/files/events) come with a structured `divergence` payload; mechanical
    rejects (compile/spec/starvation) come with a `detail` message instead.
    Flail guard (both modes): a source whose comment/whitespace-stripped
    shape was already gate-REJECTED twice on this task is refused BEFORE the
    gate spend with `reason: "duplicate"` — repair attempts reach the gate;
    verbatim-ish resubmission loops don't.
    Function mode: your source is compiled and differential-fuzzed against the
    ORIGINAL function on per-call {ret, mem} over N_FUZZ random cases drawn
    with fresh entropy every submission (seed= pins the draw for determinism;
    n_fuzz raises the budget but is FLOORED at N_FUZZ=64 at this boundary —
    you may not tune your own judge down). A model that segfaults or hangs a
    case is rejected as a crash. Wrong memory direction or a no-op against a
    void spec with no memory channel is rejected too. The spec must admit at
    least 2 distinct inputs (empty params or all-fixed ranges are refused at
    the spec stage — never pass vacuously).
    Accepted models enter the task ledger (see status) and compose per-TU at
    compose time: helpers used by one function must be `static`.
    `notes` (optional strings) are recorded in the family deduction cache as
    unverified_hypothesis entries, PROMOTED only if THIS submission is
    accepted — later family slots see them via task_open's injected `memory`.
    Accepted models also auto-write a verified_fact entry (your params =>
    source mapping) other family slots can reuse verbatim."""
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
            return submit_function(
                st, function, params or [], c_source, notes=notes, **kw
            )
        return submit_program(st, c_source, notes=notes)
    except KeyError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


@server.tool()
def status(task_id: str) -> dict:
    """Progress, readiness, and validation telemetry for a task.

    - `readiness`: recorded_cases vs the hidden-gate minimum (8 fresh inputs);
      below minimum is not ready for a meaningful submit
    - `coverage`: accepted_functions/total_functions in the manifest, program
      marker
    - `recent`: last submissions as `{mode, outcome, function?, stage?}`
      (capped at 16; stage for rejects is the gate stage that caught it)
    - `ledger`: `submissions`/`rejections` counters (program and function
      paths both accounted), `accepted` entries — "program" markers and
      `{<function>: source}` dicts — and `audit` seeds. The ledger persists
      across runs by design (accepted work is cumulative task state, not a
      session artifact); do not read a clean ledger as a fresh task."""
    try:
        st = TaskStore(task_id)
        return status_snapshot(st)
    except KeyError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


def main():
    server.run()  # stdio


if __name__ == "__main__":
    main()
