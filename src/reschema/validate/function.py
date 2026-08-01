"""Differential validation of a candidate C function vs original machine code.

v1 compares per-field: spec-declared memory + return value (no syscalls); void specs
skip ret (eax is register garbage, mem is their channel). The fuzz draw is
fresh-entropy by default (mirrors submit_program: nothing precomputable); tests pin
`seed` for determinism.
"""

from __future__ import annotations

import ctypes
import os
import random
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..driver.calling import BAREGS, call_model_native, call_original, gen_inputs
from ..driver.spec import Param

N_FUZZ = 64


@dataclass
class FnVerdict:
    ok: bool
    divergence: dict | None = None
    compared: int = 0
    skipped: int = 0
    seed: int | str | None = None  # effective fuzz seed (entropy-drawn if unpinned)


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
            {"stage": "arity", "detail": f"{len(params)} params exceed {len(BAREGS)} register-passed args"},
        )
    if params and params[0].ret == "void" and not any(
        p.kind in ("buffer_i32", "cstring") for p in params
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
    tmp = so_path.with_suffix(".c")
    tmp.write_text(c_source)
    try:
        r = subprocess.run(
            ["gcc", "-O1", "-shared", "-fPIC", str(tmp), "-o", str(so_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return FnVerdict(False, {"stage": "compile", "stderr": f"compile infra: {type(e).__name__}: {e}"})
    if r.returncode != 0:
        return FnVerdict(False, {"stage": "compile", "stderr": r.stderr})
    # Symbol check against a temp copy, not the submission path: dlopen path-caches,
    # so a resubmission to the same so_path would pass hasattr on the FIRST image and
    # blow up as a bare AttributeError later (same ponytail leak as call_model_native).
    fd, so_tmp = tempfile.mkstemp(suffix=".so")
    os.close(fd)
    try:
        # RTLD_NOW: gcc -shared permits unresolved externals; eager binding surfaces them
        # at load as a catchable OSError (lazy binding can abort the process at first call).
        lib = ctypes.CDLL(shutil.copyfile(str(so_path), so_tmp), mode=os.RTLD_NOW)
    except OSError as e:
        os.unlink(so_tmp)
        return FnVerdict(False, {"stage": "link", "detail": f"{type(e).__name__}: {e}"})
    os.unlink(so_tmp)
    if not hasattr(lib, func):
        return FnVerdict(False, {"stage": "symbol", "detail": f"'{func}' not defined by submission"})
    effective_seed = seed if seed is not None else secrets.token_hex(16)
    rng = random.Random(effective_seed)
    # ret is function-level, carried on params[0]: void functions compare mem only
    # (eax is a register scrape; mem is their channel).
    fields = ("mem",) if params and params[0].ret == "void" else ("mem", "ret")
    compared = skipped = 0
    for case in gen_inputs(params, rng, n_fuzz):
        want = call_original(binary, addr, params, case)
        if want["exit_code"] == -1:
            # Original faults on adversarial input: a crash is not a behavior spec.
            skipped += 1
            continue
        compared += 1
        got = call_model_native(str(so_path), func, params, case)
        for field in fields:
            if want[field] != got[field]:
                return FnVerdict(
                    False,
                    {
                        "input": {
                            k: (v if not isinstance(v, (bytes, list)) else str(v)[:80])
                            for k, v in case.items()
                        },
                        "field": field,
                        "expected": str(want[field])[:400],
                        "actual": str(got[field])[:400],
                        "seed": effective_seed,
                    },
                    compared=compared,
                    skipped=skipped,
                )
    if compared == 0:
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
    return FnVerdict(True, compared=compared, skipped=skipped, seed=effective_seed)
