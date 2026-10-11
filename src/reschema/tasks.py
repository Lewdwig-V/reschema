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
- inputs (the worker's workspace): `task.md` (the instructions, one variant
  per task kind: the MCP tool docstrings rewritten for the worker image's
  helpers), `family.json` (the memory dependency, see `family`), and what
  task_open shows today. Function task: `function.json` (address, signature
  guess, callees), `disasm.txt`, `abi_template.c`. Program task:
  `program.json` (seed metadata and the input mode).

`write_campaign` lays tasks out as the TOML files `warranted campaign run`
reads.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from string import Template

from warranted import (
    CampaignSpec,
    CampaignTask,
    DuplicateGuard,
    MemorySpec,
    RunConfig,
    TaskSpec,
)

from .checks.function_fuzz import MAX_BUFFER_LEN
from .checks.program_replay import MAX_ARG, MAX_ARGS, MAX_CASES, MAX_STDIN
from .corpus.generate import FUNCS, SEEDS
from .disasm.analyze import analyze_function, disasm_function
from .driver.calling import BAREGS
from .driver.spec import KINDS, Param
from .engine import HIDDEN_N, INPUT_MODE, _abi_template
from .exec import canonical
from .validate.function import N_FUZZ
from .validate.program import CFLAGS

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


_COMMON = """\
## Files

Everything in /work is read-only except /work/workspace/, which is kept from one
submission to the next; nothing else you write survives a submission. Write your
files there.

- `task.md`: this file.
- `family.json`: the program family's identity (bookkeeping, nothing to solve).
- `memory.json`: what earlier tasks of this family left. Entries of tier
  `verified` are models the checker accepted, with their source (and params for
  a function): reliable, reuse them. Notes are other workers' unchecked claims.
- `feedback-NNN.json`: the checker's verdict on each earlier submission.
- `responses/`: files the host delivers with large operation results.

You never get the binary itself; the host runs it for you (probing, below).
`gcc -fsyntax-only workspace/model.c` checks that your model compiles; nothing
compiled here can run.

## Probing

Probing commands print a request; the host runs it and shows each result in the
next observation's `host_results`, in order, as `{status, operation, result}`
(a large result also as files under `responses/<n>/<item>/`). Put up to 32 probes in one command: it costs one turn.
Each probe costs one unit of your probe budget; an identical repeat is free.
"""

_FUNCTION_MD = Template(
    """\
# Function task: model `$name`

Write a C model of the function `$name` from a compiled program that behaves
exactly like the original, declare its parameters in `params.json`, and submit.
Acceptance ends this task and records your model as a verified fact that the
family's later tasks (other functions, and the whole-program task) see in
their `memory.json`.

$common
Your task files: `function.json` (address, a heuristic signature guess: arity
and whether it returns a value, labeled a guess; and its callees),
`disasm.txt` (its disassembly), `abi_template.c` (a compile-ready starter with
a params.json sketch).

`experiment-function` calls the ORIGINAL `$name` once per case, using your
`workspace/params.json` (write it first; `--params FILE` uses another):

    experiment-function '{"c": 104}' '{"c": 65}'

A case gives one value per param name: an i32 as a number, a buffer_i32 as a
list of at most $max_buffer numbers (or a number: the length of an output
buffer), a cstring as hex bytes ending in a NUL (`"68690000"`, at most $max_arg
bytes). Each result is `{ret, mem}`: the return value and the final contents of
every buffer and cstring param (cstrings as hex). `experiment-function` is for
function tasks only. `experiment` (one whole-program run, see a program task's
task.md) also works here: `experiment '{"argv": ["hello"]}'`.

## Your model

`workspace/model.c` defines `$name` with the declared parameters, in order, as
C types: i32 is `int32_t`, buffer_i32 is `int32_t *`, cstring is `char *`;
the result is `int32_t`, or `void`. Declare it `__attribute__((sysv_abi))`.

`workspace/params.json` is a JSON list, one object per parameter in call order
(at most $max_params):

- `name`: a C identifier of at most 64 characters, unique.
- `kind`: one of $kinds.
- `range`: `[lo, hi]`, the i32 bounds the checker draws values from (default
  $default_range). For a buffer_i32, the range of its cells.
- `length_param`: for a buffer_i32, the name of the i32 param holding its
  length; that param's `hi` must be at most $max_buffer.
- `ret`: on the first param only: `"i32"` (default) or `"void"` (compare memory
  only). A void declaration needs at least one buffer_i32 or cstring param.

The declaration must admit at least 2 distinct inputs.

## Submitting

    submit

packs `workspace/model.c` and `workspace/params.json` into `/work/result.json`
and submits them. Run it as the whole command (its marker must be the first
line of output). You have $submissions submissions.

## Judgment

The checker compiles your model and calls it and the original on $n_fuzz cases
drawn at random from your ranges, fresh for every submission (you cannot choose
or replay them), comparing the return value and memory. A crash or hang in your
model is a rejection. A rejection's feedback names the stage (`spec` for a
malformed declaration, `compile`, ...) and the first diverging case with both
results. A model that differs from one already rejected only in comments or
whitespace, or barely at all, is refused without a check and still uses a
submission.
"""
)

_PROGRAM_MD = Template(
    """\
# Program task: model the whole program

Write a complete C program (`main` and all) whose observable behaviour matches
the original program exactly, nominate the cases you claim it reproduces, and
submit. Acceptance completes the task.

$common
Your task file: `program.json` (seed, compiler, optimisation, whether stripped,
the functions with addresses, and `input`: whether the program reads its input
from `argv` or from `stdin`). Models of this program's functions accepted in
earlier tasks are in `memory.json`: build on them.

`experiment` runs the ORIGINAL program once per JSON object:

    experiment '{"argv": ["hello"]}' '{"stdin": "abc\\n", "quiet": true}'

`argv` is at most $max_args strings of at most $max_arg characters, without the
program name and without NUL; `stdin` is plain text of at most $max_stdin bytes;
`quiet` leaves the syscall events out of what you are shown. Each result is the
canonical trace: `argv` with "prog" prepended, `stdout`, `stderr` and
`stdin_hex` as hex, `exit_code` (-1 on a crash or timeout, with a fault event),
`files_written` as `{path: hex}`, and `events`, the syscall timeline with
addresses as `ADDR_n` and written-to descriptors as `FD_n`. A trace too large
to show arrives without its events, the whole trace in `trace.json` under
`responses/`. `experiment-function` is for function tasks only; here it fails,
uncharged.

## Your model and cases

`workspace/model.c` is the whole program; it is compiled with gcc
`$cflags`.

`workspace/cases.json` nominates up to $max_cases cases, a JSON list of
`{"argv": [str], "stdin_hex": str}` (argv without the program name, stdin as
hex; same limits as `experiment`). Probing does not nominate anything: only
cases in cases.json are judged as yours.

## Submitting

    submit

packs `workspace/model.c` and `workspace/cases.json` into `/work/result.json`
and submits them. Run it as the whole command (its marker must be the first
line of output). You have $submissions submissions.

## Judgment

The checker records every nominated case on the original itself, then replays
your model on them (stage `recorded`), then on $hidden_n further inputs drawn
fresh for every submission (stage `hidden`). Per case it compares, byte for
byte after canonicalisation: stdout, stderr, exit code, the files written, and
the shape of the write-family syscalls (descriptor and count). A rejection's
feedback gives its `reason` (compile, io-mismatch, files-mismatch,
event-divergence, event-length, flaky-case, ...) and stage, with the first
divergence of that stage. A model that differs from one already rejected only
in comments or whitespace, or barely at all, is refused without a check and
still uses a submission.
"""
)


def task_md(kind: str, submissions: int, name: str = "") -> bytes:
    """The worker's instructions, from the checkers' own limits."""
    values = {
        "common": _COMMON,
        "submissions": submissions,
        "max_arg": MAX_ARG,
    }
    if kind == "function":
        default = list(Param.__dataclass_fields__["range"].default)
        values |= {
            "name": name,
            "max_buffer": MAX_BUFFER_LEN,
            "max_params": len(BAREGS),
            "kinds": ", ".join(f"`{k}`" for k in KINDS),
            "default_range": f"`{default}`",
            "n_fuzz": N_FUZZ,
        }
        return _FUNCTION_MD.substitute(values).encode()
    values |= {
        "max_args": MAX_ARGS,
        "max_stdin": MAX_STDIN,
        "max_cases": MAX_CASES,
        "cflags": " ".join(CFLAGS),
        "hidden_n": HIDDEN_N,
    }
    return _PROGRAM_MD.substitute(values).encode()


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
        f"Read task.md. Model the function {name}: write workspace/model.c and "
        "workspace/params.json, then run submit.",
        {
            "task.md": task_md("function", submissions, name),
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
        "Read task.md. Model the whole program: write workspace/model.c and "
        "workspace/cases.json, then run submit.",
        {
            "task.md": task_md("program", submissions),
            "program.json": _json(
                {k: meta[k] for k in shown}
                | {"input": "stdin" if meta["seed"] in INPUT_MODE else "argv"}
            ),
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


def _toml(value) -> str:
    # JSON strings, ints and lists of them are TOML (ASCII task fields only)
    return json.dumps(value)


def write_task(task: TaskSpec, out: Path) -> Path:
    """`task` as a task TOML file beside its files in `out`; `TaskSpec.load`
    reads it back equal."""
    lines = [
        f"id = {_toml(task.id)}",
        f"objective = {_toml(task.objective)}",
        f"checks = {_toml(list(task.checks))}",
        "[budgets]",
        f"submissions = {task.submissions}",
    ]
    if task.check_budget is not None:
        lines.append(f"checks = {task.check_budget}")
    if task.duplicate_guard is not None:
        lines.append("[duplicate_guard]")
        guard = dataclasses.asdict(task.duplicate_guard)
        lines += [f"{k} = {_toml(v)}" for k, v in guard.items()]
    if task.memory is not None:
        lines += [
            "[memory]",
            f"scope = {_toml(task.memory.scope)}",
            f"depends = {_toml(list(task.memory.depends))}",
        ]
    for table, files in (("inputs", task.inputs), ("private", task.private)):
        lines.append(f"[{table}]")
        (out / table).mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (out / table / name).write_bytes(data)
            lines.append(f"{_toml(name)} = {_toml(f'{table}/{name}')}")
    path = out / "task.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def write_campaign(
    campaign_id: str,
    specs: Sequence[TaskSpec],
    configs: Mapping[str, str],
    out: Path,
    split: str = "training",
) -> Path:
    """`campaign.toml` for `warranted campaign run`, its tasks in order under
    `out/tasks/`; `configs` maps a name to a run-config path relative to `out`."""
    lines = [f"id = {_toml(campaign_id)}", "[configs]"]
    lines += [f"{_toml(k)} = {_toml(v)}" for k, v in configs.items()]
    for i, task in enumerate(specs):
        d = Path("tasks") / f"{i:03d}-{re.sub(r'[^A-Za-z0-9_.-]', '_', task.id)}"
        write_task(task, out / d)
        lines += ["[[tasks]]", f"path = {_toml(str(d / 'task.toml'))}"]
        lines.append(f"split = {_toml(split)}")
    path = out / "campaign.toml"
    path.write_text("\n".join(lines) + "\n")
    return path
