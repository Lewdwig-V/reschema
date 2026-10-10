"""#112 re-grade job (3B entry): re-judge ledger accepts under the CURRENT
verifier and emit a verdict diff. Measurement only — no adjudication, no
ledger/memory writes; a flip is data, not a verdict on the old judge.

Accepts are enumerated newest first: tasks by ledger mtime (accepts carry no
timestamps), entries within a task newest first (a re-accept moves to the
end of `accepted`; ledgers written before #144 kept a re-accepted function
at its first position). `k` caps the count.

- function accepts replay `validate_function` with the audit seed and n_fuzz,
  so any flip is the judge's change, not a new draw. Params come from
  `audit[func]["params"]` (written since #143) or, for older entries, the
  family memory's `verified_fact` with the same task, source and audit seed.
- the program accept re-runs `engine.program_gate` on `program_source` with
  the audit `hidden_seed` (default) or fresh entropy (`fresh=True`), against
  the accept-time recorded-case snapshot (`audit["program"]["recorded"]`).

A flip must isolate a JUDGE change, so everything else the verdict depended
on is pinned, and an accept whose pins cannot be honored is emitted as
`new_verdict: "unreplayable"` with a reason — never dropped, never a flip:

  no program_source | no audit seed | no audit hidden_seed | no params |
  no recorded snapshot | recorded cases changed | binary changed |
  toolchain changed | canonicalizer changed | unknown task |
  unknown function | bad ledger | bad stored data: <error> |
  bad stored data: corrupt memory (<n> lines) | infra | hidden-starvation

The last two come from the judge (an environment outage or an unjudged
draw). Legacy accepts without a binary digest or toolchain image ID still
replay, flagged `binary_verified: false` / `toolchain_verified: false`.

Failure boundaries: each accept is PREPARED (every read of stored data) and
then JUDGED. A prepare failure is a row; judge exceptions raise (engine bug).
Environment faults — a missing tasks dir, an unreadable or stale manifest, a
missing corpus binary or toolchain image — fail the whole job loudly, never
per-row noise.

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

from .driver import podrun
from .driver.spec import Param
from .engine import (
    PROGRAM_NO_VERDICT_STAGES,
    TASKS,
    TaskStore,
    binary_digest,
    case_digest,
    case_key,
    load_manifest,
    program_gate,
)
from .exec.canonical import CANONICALIZER_VERSION
from .memory import scan_family
from .validate.function import validate_function


class Unreplayable(Exception):
    """A stored accept that cannot be replayed faithfully (reason = str(e))."""


def _src_hash(src: str) -> str:
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def _is_fn_entry(x: object) -> bool:
    # compose's shape: exactly one {func: source} pair per entry
    return (
        isinstance(x, dict)
        and len(x) == 1
        and all(isinstance(k, str) and isinstance(v, str) for k, v in x.items())
    )


def accepts() -> Iterator[tuple[str, dict | None, str]]:
    """(task_id, ledger, unit) newest first; unit is a function name or
    "program". An unreadable or malformed ledger yields (task_id, None,
    "ledger") once — never a silent drop."""
    if not TASKS.is_dir():  # wrong RESCHEMA_HOME must not read as "0 accepts"
        raise FileNotFoundError(f"no tasks dir at {TASKS}")

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:  # vanished/broken: sorts last, then fails the read below
            return float("-inf")

    for p in sorted(TASKS.glob("*/ledger.json"), key=mtime, reverse=True):
        task_id = p.parent.name.replace("__", "::")
        try:
            led = json.loads(p.read_text())
            # every engine-written ledger carries the key: absent is
            # corruption, never "0 accepts" (#148)
            entries = led["accepted"]
            if not isinstance(entries, list) or not all(
                x == "program" or _is_fn_entry(x) for x in entries
            ):
                raise ValueError("malformed accepted")
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            yield task_id, None, "ledger"
            continue
        for x in reversed(entries):
            yield task_id, led, "program" if x == "program" else next(iter(x))


def _fn_params(store: TaskStore, func: str, src: str, audit: dict) -> list | None:
    if "params" in audit:
        return audit["params"]
    entries, bad = scan_family(store.meta["seed"], fn=func)
    facts = [
        e
        for e in entries
        if e.get("tier") == "verified_fact"
        and e.get("task_id") == store.meta["task_id"]
        and e.get("c_source") == src
        and e.get("audit_seed") == audit.get("seed")
    ]
    if facts:
        return facts[-1]["params"]
    if bad:  # the fact may be one of them: unreadable data, not absent data
        raise Unreplayable(f"bad stored data: corrupt memory ({bad} lines)")
    return None


def _check_binary(audit: dict, current: str, row: dict) -> None:
    # The replay must judge the SAME original: a corpus rebuild that changed
    # the binary makes a flip the program's change, not the judge's.
    stored = audit.get("binary")
    row["binary_verified"] = stored is not None
    if stored is not None and stored != current:
        raise Unreplayable("binary changed")


def _check_toolchain(audit: dict, current: str, row: dict) -> None:
    # The same tag may name a rebuilt image whose gcc/clang compiles the
    # accepted source differently: a toolchain change, not a judge flip.
    stored = audit.get("toolchain")
    row["toolchain_verified"] = stored is not None
    if stored is not None and stored != current:
        raise Unreplayable("toolchain changed")


def _int(x: object, what: str) -> int:
    # stored values reach the judge, which runs OUTSIDE the prepare net:
    # type-check here so a malformed ledger is a row, not a batch abort
    if isinstance(x, bool) or not isinstance(x, int):
        raise TypeError(f"{what} {x!r} is not an int")
    return x


def _prep_function(
    led: dict,
    func: str,
    meta: dict | None,
    current_bin: str | None,
    toolchain: str,
    row: dict,
) -> dict:
    # `row` is filled as identity becomes known, so an unreplayable row still
    # names the accepted revision (source_hash, seed) it could not test.
    src = next(x[func] for x in led["accepted"] if _is_fn_entry(x) and func in x)
    row["source_hash"] = _src_hash(src)
    audit = led.get("audit", {}).get(func, {})
    row["seed"] = seed = audit.get("seed")
    if seed is None:
        raise Unreplayable("no audit seed")
    if isinstance(seed, bool) or not isinstance(seed, (int, str)):
        raise TypeError(f"audit seed {seed!r} is not an int or str")
    if meta is None:  # ledger for a slot the current manifest lacks
        raise Unreplayable("unknown task")
    _check_binary(audit, current_bin, row)
    _check_toolchain(audit, toolchain, row)
    store = TaskStore(meta["task_id"])
    params = _fn_params(store, func, src, audit)
    if params is None:
        raise Unreplayable("no params")
    fmeta = meta["functions"].get(func)
    if fmeta is None:  # removed/renamed since the accept
        raise Unreplayable("unknown function")
    return {
        "binary": meta["binary"],
        "addr": fmeta["addr"],
        "func": func,
        "params": [Param.from_json(p) for p in params],
        "c_source": src,
        "seed": seed,
        "n_fuzz": _int(audit["n_fuzz"], "audit n_fuzz"),
        "size": fmeta["size"],
    }


def _judge_function(row: dict, job: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="reschema-regrade-") as d:
        v = validate_function(so_path=Path(d) / f"{job['func']}.so", **job)
    row = {**row, "compared": v.compared, "skipped": v.skipped}
    if v.ok:
        return {**row, "new_verdict": "accept"}
    if v.divergence.get("stage") == "infra":  # environment, not the judge
        return {**row, "new_verdict": "unreplayable", "reason": "infra"}
    return {**row, "new_verdict": "reject", "divergence": v.divergence}


def _prep_program(
    led: dict,
    fresh: bool,
    meta: dict | None,
    current_bin: str | None,
    toolchain: str,
    row: dict,
) -> dict:
    src = led.get("program_source")
    if src is None:  # accepts before #118 kept no body
        raise Unreplayable("no program_source")
    row.update(source_hash=_src_hash(src), fresh=fresh)
    audit = led.get("audit", {}).get("program", {})
    seed = audit.get("hidden_seed")
    row["seed"] = None if fresh else seed
    if seed is None and not fresh:
        # a fresh draw cannot reproduce the original gate: not a judge flip
        raise Unreplayable("no audit hidden_seed")
    if seed is not None and not isinstance(seed, str):
        raise TypeError(f"audit hidden_seed {seed!r} is not a str")
    # Replay the accept-time recorded set: experiments after the accept add
    # traces (and shift the hidden dedupe), which is new evidence, not a judge
    # change. Entries are [argv[1:], stdin_hex, content digest]: a case that
    # vanished OR was edited since the accept is changed evidence.
    snap = audit.get("recorded")
    if snap is None:  # pre-#144 accepts
        raise Unreplayable("no recorded snapshot")
    if meta is None:
        raise Unreplayable("unknown task")
    _check_binary(audit, current_bin, row)
    _check_toolchain(audit, toolchain, row)
    # stored traces are canonicalized at record time: under other rules
    # their expected output is stale format, not a judge change
    if audit.get("canonicalizer") != CANONICALIZER_VERSION:
        raise Unreplayable("canonicalizer changed")
    store = TaskStore(meta["task_id"])
    by_key = {}
    for t in store.recorded():
        argv, stdin = case_key(t)
        by_key[(tuple(argv), stdin)] = t
    rec = []
    for argv, stdin, digest in snap:
        t = by_key.get((tuple(argv), stdin))
        if t is None or case_digest(t) != digest:
            raise Unreplayable("recorded cases changed")
        rec.append(t)
    return {
        "store": store,
        "c_source": src,
        "hidden_seed": None if fresh else seed,
        "rec": rec,
    }


def _judge_program(row: dict, job: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="reschema-regrade-") as d:
        fail, seed = program_gate(model=Path(d) / "model", **job)
    # an early (compile/recorded) outcome draws no seed: keep the audit one
    row = {**row, "seed": seed if seed is not None else row.get("seed")}
    if fail is None:
        return {**row, "new_verdict": "accept"}
    stage = fail.get("stage", fail["reason"])
    if stage in PROGRAM_NO_VERDICT_STAGES:  # the source was never judged
        return {**row, "new_verdict": "unreplayable", "reason": stage}
    return {**row, "new_verdict": "reject", "divergence": fail}


def regrade(
    k: int | None = None, fresh: bool = False, task_ids: set[str] | None = None
) -> list[dict]:
    # Environment, read OUTSIDE the per-accept net: a stale/corrupt manifest
    # or a missing corpus binary fails the job once, loudly.
    manifest = {t["task_id"]: t for t in load_manifest()}
    toolchain = podrun.image_id()
    bin_digests: dict[str, str] = {}
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
        meta = manifest.get(task_id)
        current_bin = None
        if meta is not None:
            if task_id not in bin_digests:
                bin_digests[task_id] = binary_digest(meta["binary"])
            current_bin = bin_digests[task_id]
        row: dict = {}
        try:
            if unit == "program":
                job = _prep_program(led, fresh, meta, current_bin, toolchain, row)
            else:
                job = _prep_function(led, unit, meta, current_bin, toolchain, row)
        except Exception as e:  # noqa: BLE001 - stored data only; judges run below
            reason = (
                str(e)
                if isinstance(e, Unreplayable)
                else f"bad stored data: {type(e).__name__}: {e}"
            )
            rows.append(
                {**base, **row, "new_verdict": "unreplayable", "reason": reason}
            )
            continue
        if unit == "program":
            rows.append({**base, **_judge_program(row, job)})
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
