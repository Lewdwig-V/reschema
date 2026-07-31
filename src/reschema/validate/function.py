"""Differential validation of a candidate C function vs original machine code.

v1 compares per-field: spec-declared memory + return value (no syscalls). The
fuzz draw is fresh-entropy by default (mirrors submit_program: nothing
precomputable); tests pin `seed` for determinism.
"""

from __future__ import annotations

import ctypes
import random
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..driver.calling import call_model_native, call_original, gen_inputs
from ..driver.spec import Param

N_FUZZ = 64


@dataclass
class FnVerdict:
    ok: bool
    divergence: dict | None = None


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
    if not hasattr(ctypes.CDLL(str(so_path)), func):
        return FnVerdict(False, {"stage": "symbol", "detail": f"'{func}' not defined by submission"})
    rng = random.Random(seed if seed is not None else secrets.token_hex(16))
    compared = 0
    for case in gen_inputs(params, rng, n_fuzz):
        want = call_original(binary, addr, params, case)
        if want["exit_code"] == -1:
            # Original faults on adversarial input: a crash is not a behavior spec.
            continue
        compared += 1
        got = call_model_native(str(so_path), func, params, case)
        # mem first: for void functions eax is register garbage; memory is the
        # meaningful channel. Scalar functions have empty mem and fall to "ret".
        for field in ("mem", "ret"):
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
                    },
                )
    if compared == 0:
        # No case compared: never pass vacuously.
        return FnVerdict(
            False,
            {"stage": "skip-starvation", "detail": f"original faulted on all {n_fuzz} fuzz cases"},
        )
    return FnVerdict(True)
