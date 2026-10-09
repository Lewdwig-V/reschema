"""#112 re-grade job (3B entry): re-judge ledger accepts under the CURRENT
verifier and emit a verdict diff. Measurement only — no adjudication, no
ledger/memory writes; a flip is data, not a verdict on the old judge.

Accepts are enumerated newest first: tasks by ledger mtime (accepts carry no
timestamps), entries within a task newest first (a re-accept moves to the
end of `accepted`; ledgers written before #144 kept a re-accepted function
at its first position). `k` caps the count. Infra failures and unjudged
program draws (`PROGRAM_NO_VERDICT_STAGES`) are unreplayable, not flips:
an environment outage must never read as a judge regression.

- function accepts replay `validate_function` with the audit seed and n_fuzz,
  so any flip is the judge's change, not a new draw. Params come from
  `audit[func]["params"]` (written since #143) or, for older entries, the
  family memory's `verified_fact` with the same source and audit seed.
- the program accept re-runs `engine.program_gate` on `program_source` with
  the audit `hidden_seed` (default) or fresh entropy (`fresh=True`), against
  the accept-time recorded-case snapshot (`audit["program"]["recorded"]`).

Each accept is prepared (all stored-data reads) then judged. Anything that
cannot be replayed faithfully (no source, no audit seed, no params, no
recorded snapshot or a changed one, unknown task or function, or any stored
data that fails to load or decode) is
emitted as `new_verdict: "unreplayable"` with a reason — never dropped.

stdout: one JSON line per accept. stderr: totals.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from .driver.spec import Param
from .engine import (
    PROGRAM_NO_VERDICT_STAGES,
    TASKS,
    TaskStore,
    _fn_meta,
    case_key,
    program_gate,
)
from .exec.canonical import CANONICALIZER_VERSION
from .memory import read_family
from .validate.function import N_FUZZ, validate_function


def _src_hash(src: str) -> str:
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def accepts() -> Iterator[tuple[str, dict | None, str]]:
    """(task_id, ledger, unit) newest first; unit is a function name or
    "program". An unreadable ledger yields (task_id, None, "ledger") once."""
    ledgers = sorted(
        TASKS.glob("*/ledger.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for p in ledgers:
        task_id = p.parent.name.replace("__", "::")
        try:
            led = json.loads(p.read_text())
            entries = list(reversed(led.get("accepted", [])))
        except (OSError, ValueError, TypeError, AttributeError):
            yield task_id, None, "ledger"
            continue
        for x in entries:
            if x == "program":
                yield task_id, led, "program"
            elif isinstance(x, dict):
                for func in x:
                    yield task_id, led, func


def _fn_params(store: TaskStore, func: str, src: str, audit: dict) -> list | None:
    if "params" in audit:
        return audit["params"]
    facts = [
        e
        for e in read_family(store.meta["seed"], fn=func)
        if e.get("tier") == "verified_fact"
        and e.get("task_id") == store.meta["task_id"]
        and e.get("c_source") == src
        and e.get("audit_seed") == audit.get("seed")
    ]
    return facts[-1]["params"] if facts else None


class Unreplayable(Exception):
    """A stored accept that cannot be replayed faithfully (reason = str(e))."""


# Each accept is PREPARED (every read of stored data: ledger fields, audit,
# params, memory, traces) and then JUDGED. Any prepare failure is a row;
# the judge calls run outside that net, so an engine bug still raises.


def _prep_function(store: TaskStore, led: dict, func: str) -> tuple[dict, dict]:
    src = next(x[func] for x in led["accepted"] if isinstance(x, dict) and func in x)
    audit = led.get("audit", {}).get(func, {})
    row = {"source_hash": _src_hash(src), "seed": audit.get("seed")}
    if audit.get("seed") is None:
        raise Unreplayable("no audit seed")
    params = _fn_params(store, func, src, audit)
    if params is None:
        raise Unreplayable("no params")
    if func not in store.meta["functions"]:  # removed/renamed since the accept
        raise Unreplayable("unknown function")
    fmeta = _fn_meta(store, func)
    job = {
        "binary": store.meta["binary"],
        "addr": fmeta["addr"],
        "func": func,
        "params": [Param.from_json(p) for p in params],
        "c_source": src,
        "seed": audit["seed"],
        "n_fuzz": int(audit.get("n_fuzz", N_FUZZ)),
        "size": fmeta["size"],
    }
    return row, job


def _judge_function(row: dict, job: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="reschema-regrade-") as d:
        v = validate_function(so_path=Path(d) / f"{job['func']}.so", **job)
    row = {**row, "compared": v.compared, "skipped": v.skipped}
    if v.ok:
        return {**row, "new_verdict": "accept"}
    if v.divergence.get("stage") == "infra":  # environment, not the judge
        return {**row, "new_verdict": "unreplayable", "reason": "infra"}
    return {**row, "new_verdict": "reject", "divergence": v.divergence}


def _prep_program(store: TaskStore, led: dict, fresh: bool) -> tuple[dict, dict]:
    src = led.get("program_source")
    if src is None:  # accepts before #118 kept no body
        raise Unreplayable("no program_source")
    audit = led.get("audit", {}).get("program", {})
    if audit.get("hidden_seed") is None and not fresh:
        # a fresh draw cannot reproduce the original gate: not a judge flip
        raise Unreplayable("no audit hidden_seed")
    # Replay the accept-time recorded set: experiments after the accept add
    # traces (and shift the hidden dedupe), which is new evidence, not a judge
    # change. No snapshot (pre-#144) or a vanished case => not reproducible.
    snap = audit.get("recorded")
    if snap is None:
        raise Unreplayable("no recorded snapshot")
    by_key = {json.dumps(case_key(t)): t for t in store.recorded()}
    want = [json.dumps(k) for k in snap]
    if any(k not in by_key for k in want):
        raise Unreplayable("recorded cases changed")
    job = {
        "c_source": src,
        "hidden_seed": None if fresh else audit["hidden_seed"],
        "rec": [by_key[k] for k in want],
    }
    return {"source_hash": _src_hash(src), "fresh": fresh}, job


def _judge_program(store: TaskStore, row: dict, job: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="reschema-regrade-") as d:
        fail, seed = program_gate(store, model=Path(d) / "model", **job)
    row = {**row, "seed": seed}
    if fail is None:
        return {**row, "new_verdict": "accept"}
    stage = fail.get("stage", fail["reason"])
    if stage in PROGRAM_NO_VERDICT_STAGES:  # the source was never judged
        return {**row, "new_verdict": "unreplayable", "reason": stage}
    return {**row, "new_verdict": "reject", "divergence": fail}


def regrade(
    k: int | None = None, fresh: bool = False, task_ids: set[str] | None = None
) -> list[dict]:
    rows = []
    for task_id, led, unit in accepts():
        if task_ids is not None and task_id not in task_ids:
            continue
        if k is not None and len(rows) >= k:
            break
        base = {"task_id": task_id, "unit": unit, "old_verdict": "accept"}
        if led is None:
            rows.append(
                {
                    **base,
                    "old_verdict": "unknown",
                    "new_verdict": "unreplayable",
                    "reason": "bad ledger",
                }
            )
            continue
        try:
            store = TaskStore(task_id)
        except KeyError:  # ledger for a slot the current manifest lacks
            rows.append(
                {**base, "new_verdict": "unreplayable", "reason": "unknown task"}
            )
            continue
        try:
            if unit == "program":
                row, job = _prep_program(store, led, fresh)
            else:
                row, job = _prep_function(store, led, unit)
        except Unreplayable as e:
            rows.append({**base, "new_verdict": "unreplayable", "reason": str(e)})
            continue
        except Exception as e:  # noqa: BLE001 - stored data only; judges run below
            reason = f"bad stored data: {type(e).__name__}: {e}"
            rows.append({**base, "new_verdict": "unreplayable", "reason": reason})
            continue
        if unit == "program":
            rows.append({**base, **_judge_program(store, row, job)})
        else:
            rows.append({**base, **_judge_function(row, job)})
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--k", type=int, default=None, help="newest K accepts (default all)"
    )
    ap.add_argument(
        "--fresh", action="store_true", help="program: fresh hidden entropy"
    )
    ap.add_argument("--task", action="append", help="restrict to task_id (repeatable)")
    a = ap.parse_args(argv)
    rows = regrade(a.k, a.fresh, set(a.task) if a.task else None)
    for r in rows:
        print(json.dumps({"canonicalizer": CANONICALIZER_VERSION, **r}))
    flips = Counter((r["old_verdict"], r["new_verdict"]) for r in rows)
    print(
        json.dumps(
            {"total": len(rows), **{f"{o}->{n}": c for (o, n), c in flips.items()}}
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
