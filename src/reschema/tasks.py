"""Warranted TaskSpecs for a corpus slot (M8 S5, decision D2 in
docs/proposals/m8-rebuild-on-warranted.md).

One task per function (`<slot>::fn::<name>`, check `function-fuzz`) and one per
program (`<slot>::program`, check `program-replay`), all sharing the seed's
memory scope. A campaign orders a slot's function tasks before its program
task, so accepted functions reach the program task as verified facts.

Task files:
- private (checkers and the experiment operation only): `binary`, the corpus
  ELF, and `task.json`, {"seed": name} plus, for a function task,
  "function": {name, addr, size}. The worker never holds the binary's bytes:
  it could embed and exec the original.
- inputs (the worker's workspace): `family.json` (the memory dependency, see
  `family`), and what task_open shows today. Function task: `function.json`
  (address, signature guess, callees), `disasm.txt`, `abi_template.c`.
  Program task: `program.json` (seed metadata and the input mode).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from warranted import (
    CampaignSpec,
    CampaignTask,
    DuplicateGuard,
    MemorySpec,
    RunConfig,
    TaskSpec,
)

from .corpus.generate import FUNCS, SEEDS
from .disasm.analyze import analyze_function, disasm_function
from .engine import INPUT_MODE, _abi_template
from .exec import canonical

SUBMISSIONS = 8
# The flail guard's thresholds (engine DUP_*), as Warranted's guard; the
# normalisation is ReSchemaDomain.normalize (engine._norm_source).
GUARD = DuplicateGuard(
    exact_repeats=2,
    near_repeats=3,
    near_edit_floor=24,
    near_edit_percent=6,
    window=8,
)


def _json(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def family(seed: str) -> bytes:
    """The family's identity for memory applicability: a change to the seed's
    C source, its function set or the canonicaliser marks earlier facts stale.
    A digest, never the source. ReSchema declares no ABI beyond the source
    (the abi template is a per-slot guess), so the function set is names."""
    return _json(
        {
            "seed": seed,
            "source_sha256": hashlib.sha256(
                (SEEDS / f"{seed}.c").read_bytes()
            ).hexdigest(),
            "functions": FUNCS[seed],
            "canonicalizer": canonical.CANONICALIZER_VERSION,
        }
    )


def _task(meta: dict, suffix, objective, inputs, task, check, submissions, checks):
    return TaskSpec(
        f"{meta['task_id']}::{suffix}",
        objective,
        {"family.json": family(meta["seed"]), **inputs},
        {
            "binary": Path(meta["binary"]).read_bytes(),
            "task.json": _json({"seed": meta["seed"], **task}),
        },
        (check,),
        submissions,
        check_budget=checks,
        duplicate_guard=GUARD,
        memory=MemorySpec(meta["seed"], ("family.json",)),
    )


def function_task(
    meta: dict, name: str, *, submissions=SUBMISSIONS, check_budget=SUBMISSIONS
) -> TaskSpec:
    f = meta["functions"][name]
    facts = analyze_function(meta["binary"], meta["functions"])[name]
    return _task(
        meta,
        f"fn::{name}",
        f"Model the function {name}: write model.c and params.json, then submit.",
        {
            "function.json": _json(
                {
                    "function": name,
                    "address": hex(f["addr"]),
                    "signature_guess": {
                        k: facts[k] for k in ("arity_guess", "returns_hint", "labeled")
                    },
                    "callees": facts["callees"],
                }
            ),
            "disasm.txt": disasm_function(
                meta["binary"], f["addr"], f["size"]
            ).encode(),
            "abi_template.c": _abi_template(name, facts).encode(),
        },
        {"function": {"name": name, "addr": f["addr"], "size": f["size"]}},
        "function-fuzz",
        submissions,
        check_budget,
    )


def program_task(
    meta: dict, *, submissions=SUBMISSIONS, check_budget=SUBMISSIONS
) -> TaskSpec:
    shown = ("seed", "compiler", "opt", "stripped", "functions")
    return _task(
        meta,
        "program",
        "Model the whole program: write model.c and cases.json, then submit.",
        {
            "program.json": _json(
                {k: meta[k] for k in shown}
                | {"input": "stdin" if meta["seed"] in INPUT_MODE else "argv"}
            )
        },
        {},
        "program-replay",
        submissions,
        check_budget,
    )


def slot_tasks(meta: dict) -> list[TaskSpec]:
    """A slot's function tasks, then its program task (D2's campaign order)."""
    return [*(function_task(meta, n) for n in meta["functions"]), program_task(meta)]


def campaign(
    campaign_id: str,
    metas: list[dict],
    configs: Mapping[str, RunConfig],
    split: str = "training",
) -> CampaignSpec:
    return CampaignSpec(
        campaign_id,
        tuple(CampaignTask(t, split) for m in metas for t in slot_tasks(m)),
        configs,
    )
