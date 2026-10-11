"""`function-fuzz`: the Warranted checker for one function (M8 S3).

Decides exactly as `validate_function`, the gate it replaces (parity pinned by
tests/golden): the original runs on the host under qiling, the agent's C only in
a Warranted job (native_worker.py + spec.py as flat files, no mounts). The job's
output is the candidate's claim: it is shape-checked here and compared on the
host against ground truth the job never sees. Every non-pass is REJECTED — a
job can forge any stage, `infra` included, so only a host-side raise (missing
image, runner failure) becomes Warranted's infrastructure failure.

Task files: inputs `binary` (the corpus binary) and `function.json`
({name, addr, size}); candidate `model.c` and `params.json` (submit_function's
param list).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from warranted import FACTS_LIMIT, CheckContext, JobLimits, Verdict, VerdictStatus

from ..driver import podrun
from ..driver.native_worker import CASE_TIMEOUT_S
from ..driver.spec import Param
from ..validate.function import N_FUZZ, validate_function

_DRIVER = Path(__file__).resolve().parents[1] / "driver"
# podrun's level-B envelope; /work holds the source, the .so and per-load copies.
_LIMITS = JobLimits(memory_mb=1024, pids=128, scratch_mb=64)
_CAP = 4096  # per worker-reported string; keeps feedback far under FEEDBACK_LIMIT
# Buffers are drawn on the host before any job limit applies: a scalar that
# sizes one is capped, or a one-line spec could allocate 2**31 cells here.
MAX_BUFFER_LEN = 4096


def _reject(feedback: dict) -> Verdict:
    return Verdict(VerdictStatus.REJECTED, {"accepted": False, **feedback})


def _params(raw: bytes) -> list[Param]:
    """Param.from_json plus the types the fuzz draw relies on, so a malformed
    spec is a spec reject, never a host traceback."""
    specs = json.loads(raw)
    if type(specs) is not list or not all(type(d) is dict for d in specs):
        raise ValueError("params.json must be a list of param objects")
    params = [Param.from_json(d) for d in specs]
    scalars = {p.name for p in params if p.kind == "i32"}
    if len({p.name for p in params}) != len(params):
        raise ValueError("param names must be unique")
    for p in params:
        lo, hi = p.range
        if type(p.name) is not str or not (
            type(lo) is type(hi) is int and -(2**31) <= lo <= hi < 2**31
        ):
            raise ValueError(f"param {p.name!r}: name str, range [lo, hi] i32s")
        if p.ret not in ("i32", "void"):
            raise ValueError(f"param {p.name!r}: ret must be i32 or void")
        if p.length_param is not None and p.length_param not in scalars:
            raise ValueError(f"param {p.name!r}: length_param must name an i32")
    sizing = {p.length_param for p in params if p.length_param is not None}
    for p in params:
        if p.name in sizing and p.range[1] > MAX_BUFFER_LEN:
            raise ValueError(
                f"param {p.name!r}: sizes a buffer, range must stay <= {MAX_BUFFER_LEN}"
            )
    return params


def _value(got, mem: dict) -> bool:
    if type(got) is not dict or set(got) != {"ret", "mem"}:
        return False
    if type(got["ret"]) is not int or type(got["mem"]) is not dict:
        return False
    if set(got["mem"]) != set(mem):
        return False
    for name, v in got["mem"].items():
        if mem[name] == "cstring":
            try:
                bytes.fromhex(v)
            except (TypeError, ValueError):
                return False
        elif type(v) is not list or not all(type(x) is int for x in v):
            return False
    return True


def _crash(c) -> dict:
    """The worker's crash shapes pass through; anything else is capped text."""
    if c == {"timeout": True} or (
        type(c) is dict and set(c) == {"signal"} and type(c["signal"]) is int
    ):
        return c
    return {"error": str(c)[:200]}


def _claim(out, params: list[Param], n: int) -> dict:
    """The job's stdout as validate_function's worker result, or a `worker`
    stage. A short result list without a trailing crash is refused: it would
    compare fewer cases than were drawn (a forged `{"results": []}` passes
    vacuously otherwise)."""
    if type(out) is not dict:
        return {"stage": "worker", "detail": "worker output is not an object"}
    if "stage" in out:  # compile/link/symbol, as the worker reported it
        return {
            k: str(out[k])[:_CAP] for k in ("stage", "detail", "stderr") if k in out
        }
    res = out.get("results")
    mem = {p.name: p.kind for p in params if p.kind != "i32"}
    if type(res) is list and 0 < len(res) <= n:
        *body, last = res
        if type(last) is dict and set(last) == {"crash"}:
            last = {"crash": _crash(last["crash"])}
        elif len(res) < n or not _value(last, mem):
            last = None
        if last is not None and all(_value(g, mem) for g in body):
            return {"results": [*body, last]}
    return {"stage": "worker", "detail": f"worker results malformed for {n} cases"}


class FunctionFuzz:
    version = "1"
    isolated = True  # a private temp copy of the binary and contained jobs

    def __init__(self, n_fuzz: int = N_FUZZ):
        self.n_fuzz = n_fuzz

    def check(self, ctx: CheckContext) -> Verdict:
        fn = json.loads(ctx.inputs["function.json"])
        missing = sorted({"model.c", "params.json"} - set(ctx.candidate))
        if missing:
            return _reject({"reason": "spec", "detail": f"missing {missing}"})
        try:
            params = _params(ctx.candidate["params.json"])
            source = ctx.candidate["model.c"].decode()
        except (LookupError, ValueError, TypeError, RecursionError) as e:
            return _reject({"reason": "spec", "detail": str(e)[:_CAP]})

        def run_model(job: dict) -> dict:
            r = ctx.run_job(
                podrun.image_id(),
                # /tmp is noexec in a job: the worker dlopens copies from TMPDIR
                ["env", "TMPDIR=/work", "python", "native_worker.py"],
                {
                    n: (_DRIVER / n).read_bytes()
                    for n in ("native_worker.py", "spec.py")
                },
                stdin=json.dumps(job).encode(),
                timeout_seconds=120 + len(job["cases"]) * (CASE_TIMEOUT_S + 2),
                limits=_LIMITS,
            )
            if r.returncode != 0 or r.timed_out or r.truncated:
                return {
                    "stage": "worker",
                    "detail": f"job rc={r.returncode} timed_out={r.timed_out} "
                    f"truncated={r.truncated}: "
                    + r.stderr.decode(errors="replace")[-_CAP:],
                }
            try:
                out = json.loads(r.stdout)
            except (ValueError, RecursionError):
                return {"stage": "worker", "detail": "worker output is not JSON"}
            return _claim(out, params, len(job["cases"]))

        with tempfile.TemporaryDirectory(prefix="reschema-fnfuzz-") as d:
            binary = Path(d) / "binary"
            binary.write_bytes(ctx.inputs["binary"])
            v = validate_function(
                str(binary),
                fn["addr"],
                fn["name"],
                params,
                source,
                seed=ctx.draw_seed(),
                n_fuzz=self.n_fuzz,
                size=fn["size"],
                run_model=run_model,
            )
        if not v.ok:
            return _reject({"divergence": v.divergence})
        # D2: an accepted function is a verified fact for its family; params
        # and source ride along when they fit under FACTS_LIMIT, else digests.
        fact = {
            "function": fn["name"],
            "params": [p.to_json() for p in params],
            "source": source,
        }
        if len(json.dumps([fact], indent=2)) > FACTS_LIMIT // 2:
            fact = {
                "function": fn["name"],
                **{
                    f"{k}_sha256": hashlib.sha256(ctx.candidate[f]).hexdigest()
                    for k, f in (("params", "params.json"), ("source", "model.c"))
                },
            }
        return Verdict(
            VerdictStatus.PASSED,
            {"accepted": True, "compared": v.compared, "skipped": v.skipped},
            facts=(fact,),
        )
