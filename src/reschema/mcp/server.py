"""ReSchema MCP tool server: strict judge, thin wrapper over engine (spec §5).

Dispatch only — no business logic here; the engine's structured dict returns are the
contract. compose() is deliberately NOT exposed.

mcp is pinned at 2.0: FastMCP (1.x API, which the plan's snippet targeted) is gone;
MCPServer + @tool is the current equivalent. The plan also passed hidden_seed/modes
to submit_program — engine has neither kwarg (hidden inputs take fresh entropy and
stdin-vs-argv lives in STDIN_DRIVEN), so the call is plain.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..engine import (
    TaskStore,
    experiment_function,
    open_function_task,
    submit_function,
    submit_program,
)

server = MCPServer("reschema")


def _err(e: KeyError) -> dict:
    """Unknown task/function is a structured answer, not a thrown fault."""
    return {"error": "not_found", "detail": e.args[0]}


@server.tool()
def corpus_build() -> list[str]:
    """Build the synthetic corpus; return task IDs."""
    from ..corpus.generate import build

    return [t["task_id"] for t in build()]


@server.tool()
def task_open(task_id: str, function: str | None = None) -> dict:
    """Open a task: metadata (and, for function mode, disasm slice)."""
    try:
        st = TaskStore(task_id)
        if function:
            return open_function_task(st, function)
        m = st.meta
        return {"task_id": task_id, "seed": m["seed"], "compiler": m["compiler"],
                "opt": m["opt"], "stripped": m["stripped"], "functions": m["functions"]}
    except KeyError as e:
        return _err(e)


@server.tool()
def experiment(task_id: str, argv: list[str] | None = None, stdin: str = "",
               function: str | None = None, params: list[dict] | None = None,
               case: dict | None = None) -> dict:
    """Run a ground-truth experiment: record binary behavior (or call a function)."""
    try:
        st = TaskStore(task_id)
        if function:
            return experiment_function(st, function, params or [], case or {})
        # ponytail: zero-padded — recorded() globs trace_<label>.json and sorts
        # lexicographically, so e10 must not precede e2 (upgrade: e04d at 100)
        label = f"e{len(st.recorded()):02d}"
        return st.record_case(label, argv or [], stdin.encode())
    except KeyError as e:
        return _err(e)


@server.tool()
def submit_model(task_id: str, c_source: str, function: str | None = None,
                 params: list[dict] | None = None, seed: int | None = None,
                 n_fuzz: int | None = None) -> dict:
    """Submit a C world-model. Program mode replays traces + hidden tests; function
    mode differential-fuzzes. seed/n_fuzz pass through to the fuzzer; None = engine
    defaults (fresh entropy, full fuzz budget)."""
    try:
        st = TaskStore(task_id)
        if function:
            kw = {"seed": seed} | ({} if n_fuzz is None else {"n_fuzz": n_fuzz})
            return submit_function(st, function, params or [], c_source, **kw)
        return submit_program(st, c_source)
    except KeyError as e:
        return _err(e)


@server.tool()
def status(task_id: str) -> dict:
    """Replay metrics + accepted functions ledger."""
    try:
        st = TaskStore(task_id)
    except KeyError as e:
        return _err(e)
    return {"task_id": task_id, "recorded_cases": len(st.recorded()), "ledger": st.ledger()}


def main():
    server.run()  # stdio


if __name__ == "__main__":
    main()
