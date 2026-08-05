"""Differential validation of a candidate C function vs original machine code.

v1 compares per-field: spec-declared memory + return value (no syscalls); void specs
skip ret (eax is register garbage, mem is their channel). The fuzz draw is
fresh-entropy by default (mirrors submit_program: nothing precomputable); tests pin
`seed` for determinism.

Containment: agent source compiles and executes ONLY inside the level-B podman
worker (see ARCHITECTURE.md) — never in this process.
"""

from __future__ import annotations

import random
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..driver import podrun
from ..driver.calling import BAREGS, batch_call_original, gen_inputs
from ..driver.spec import Param

N_FUZZ = 64


@dataclass
class FnVerdict:
    ok: bool
    divergence: dict | None = None
    compared: int = 0
    skipped: int = 0
    seed: int | str | None = None  # effective fuzz seed (entropy-drawn if unpinned)


def _preview(case: dict) -> dict:
    return {
        k: (v if not isinstance(v, (bytes, list)) else str(v)[:80])
        for k, v in case.items()
    }


def _crash_text(crash: dict) -> str:
    if "signal" in crash:
        return f"signal {crash['signal']}"
    if "timeout" in crash:
        return "timeout"
    return str(crash)


def validate_function(
    binary: str,
    addr: int,
    func: str,
    params: list[Param],
    c_source: str,
    so_path: Path,
    seed: int | None = None,
    n_fuzz: int = N_FUZZ,
) -> FnVerdict:
    if len(params) > len(BAREGS):
        return FnVerdict(
            False,
            {
                "stage": "arity",
                "detail": f"{len(params)} params exceed {len(BAREGS)} register-passed args",
            },
        )
    if (
        params
        and params[0].ret == "void"
        and not any(p.kind in ("buffer_i32", "cstring") for p in params)
    ):
        # Exploit floor: a scalar-only void compares {} == {} — a no-op would pass.
        # Readback is direction-agnostic, so ANY buffer/cstring param counts as a channel.
        return FnVerdict(
            False,
            {
                "stage": "spec",
                "detail": 'ret "void" requires >=1 memory-channel param (buffer_i32/cstring); '
                "scalar-only compares nothing",
            },
        )
    effective_seed = seed if seed is not None else secrets.token_hex(16)
    rng = random.Random(effective_seed)
    # ret is function-level, carried on params[0]: void functions compare mem only
    # (eax is a register scrape; mem is their channel).
    fields = ("mem",) if params and params[0].ret == "void" else ("mem", "ret")
    kinds = {p.name: p.kind for p in params}

    # Originals (trusted corpus code) run on the host under qiling; a crash is not
    # a behavior spec — skip. One VM serves the whole round via snapshot/restore
    # (issue #41); per-case results are provably identical to fresh VMs
    # (test_batch_call_original_matches_fresh_per_case).
    # Model results come back in one worker round trip.
    cases = gen_inputs(params, rng, n_fuzz)
    kept: list[tuple[dict, dict]] = []
    skipped = 0
    for case, want in zip(cases, batch_call_original(binary, addr, params, cases)):
        if want["exit_code"] == -1:
            skipped += 1
            continue
        kept.append((case, want))
    if not kept:
        # No case compared: never pass vacuously.
        return FnVerdict(
            False,
            {
                "stage": "skip-starvation",
                "detail": f"original faulted on all {n_fuzz} fuzz cases",
                "seed": effective_seed,
            },
            skipped=skipped,
        )
    # ISSUE-61: compile AND fork-per-case ctypes execution run against a scratch
    # dir holding only the model source — the task dir (traces/ledger/accepts)
    # is never inside the container's writable mount.
    with tempfile.TemporaryDirectory(prefix="reschema-validate-") as scratch:
        try:
            r = podrun.run_worker(
                {
                    "mode": "validate",
                    "c_source": c_source,
                    "fname": func,
                    "params": [p.to_json() for p in params],
                    "cases": [
                        {
                            k: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
                            for k, v in case.items()
                        }
                        for case, _ in kept
                    ],
                },
                Path(scratch),
            )
        except (
            RuntimeError
        ) as e:  # missing podman/image: mandatory containment, no fallback
            return FnVerdict(False, {"stage": "infra", "detail": str(e)})
        if "stage" in r:
            return FnVerdict(
                False, r
            )  # compile/link/symbol/infra payloads pass through
        built = Path(scratch) / f"{func}.so"
        if built.exists():
            shutil.copy2(built, so_path)  # debug artifact parity with prior layout

    compared = 0
    for (case, want), got in zip(kept, r["results"]):
        compared += 1
        if "crash" in got:
            # Model crash/hang is the model's fault — reject, never wedge or die.
            return FnVerdict(
                False,
                {
                    "input": _preview(case),
                    "field": "crash",
                    "expected": "no crash",
                    "actual": _crash_text(got["crash"]),
                    "seed": effective_seed,
                },
                compared=compared,
                skipped=skipped,
            )
        got_cmp = {
            "ret": got["ret"],
            "mem": {
                k: (bytes.fromhex(v) if kinds[k] == "cstring" else v)
                for k, v in got["mem"].items()
            },
        }
        for field in fields:
            if want[field] != got_cmp[field]:
                return FnVerdict(
                    False,
                    {
                        "input": _preview(case),
                        "field": field,
                        "expected": str(want[field])[:400],
                        "actual": str(got_cmp[field])[:400],
                        "seed": effective_seed,
                    },
                    compared=compared,
                    skipped=skipped,
                )
    return FnVerdict(True, compared=compared, skipped=skipped, seed=effective_seed)
