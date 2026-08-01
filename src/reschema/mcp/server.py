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
    """Build the synthetic corpus; return task IDs."""
    from ..corpus.generate import build

    try:
        return [t["task_id"] for t in build()]
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


@server.tool()
def task_open(task_id: str, function: str | None = None) -> dict:
    """Open a task: metadata (and, for function mode, disasm slice)."""
    try:
        st = TaskStore(task_id)
        if function:
            return open_function_task(st, function)
        m = st.meta
        return {"task_id": task_id, "seed": m["seed"], "compiler": m["compiler"],
                "opt": m["opt"], "stripped": m["stripped"], "functions": m["functions"],
                "input": "stdin" if m["seed"] in STDIN_DRIVEN else "argv"}
    except KeyError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


@server.tool()
def experiment(task_id: str, argv: list[str] | None = None, stdin: str = "",
               function: str | None = None, params: list[dict] | None = None,
               case: dict | None = None) -> dict:
    """Run a ground-truth experiment: record binary behavior (or call a function)."""
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
def submit_model(task_id: str, c_source: str, function: str | None = None,
                 params: list[dict] | None = None, seed: int | None = None,
                 n_fuzz: int | None = None) -> dict:
    """Submit a C world-model. Program mode replays traces + hidden tests; function
    mode differential-fuzzes. seed passes through; n_fuzz is floored at N_FUZZ here
    (the agent may not tune its own judge budget down); None = engine default."""
    try:
        st = TaskStore(task_id)
        if function:
            # Budget floor lives at the agent boundary only: internal callers
            # (engine/tests) keep n_fuzz as given. Read N_FUZZ at call time.
            kw = {"seed": seed} | (
                {} if n_fuzz is None else {"n_fuzz": max(N_FUZZ, min(n_fuzz, 4 * N_FUZZ))}
            )
            return submit_function(st, function, params or [], c_source, **kw)
        return submit_program(st, c_source)
    except KeyError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


@server.tool()
def status(task_id: str) -> dict:
    """Replay metrics + accepted functions ledger."""
    try:
        st = TaskStore(task_id)
        return {"task_id": task_id, "recorded_cases": len(st.recorded()), "ledger": st.ledger()}
    except KeyError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 — catch-all at the tool boundary: faults become structured answers
        return _internal(e)


def main():
    server.run()  # stdio


if __name__ == "__main__":
    main()
