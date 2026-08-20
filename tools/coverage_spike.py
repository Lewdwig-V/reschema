"""ISSUE-121: coverage-substrate measurement spike — measurement ONLY.

The one number that decides whether any block-level instrumentation ships:
wall-clock ratio of `batch_call_original` with vs without a minimal
AFL-style edge-fold `hook_block` riding the 64 executions the function gate
already pays for per submission.

- ratio ≈ 1  → the substrate can free-ride: round-one coverage costs nothing
- ratio large → every substrate design must ship a seeded 1-of-k sampling
  dial (`coverage: sampled 1/k` honesty marker) from day one

Also pinned here (the spike's own negative gate): ± hook, per-case payloads
must be BIT-IDENTICAL — a probe that perturbs a verdict is not a probe, it
is a behavior change.

Cost shape: one corpus slot (syscall-free = snapshot path), fixed seed,"""

from __future__ import annotations

import statistics
import sys
import time

from reschema.driver.calling import batch_call_original, gen_inputs
from reschema.driver.spec import Param

MASK = 1 << 15  # 32KiB bitmap, AFL-flavored sparse collisions


def _hook_recorder():
    bits = bytearray(MASK)
    prev = [0]
    hits = [0]

    def cb(ql, address, size):
        idx = (address ^ (prev[0] >> 1)) & (MASK - 1)
        bits[idx] = 1
        prev[0] = address
        hits[0] += 1

    registered = [None]

    def hook(ql, case_index):
        """Register ONCE PER VM (qiling registrations persist across snapshot
        restores but never across _boot_vm) and zero the bitmap once at that
        vm's first case — the union over the batch is THE edge map the spike
        measures. Never stack a registration per case (codex P2 on #124)."""
        if registered[0] is not ql:  # fresh batch VM
            registered[0] = ql
            ql.hook_block(cb)
            bits[:] = b"\x00" * len(bits)

    return hook, bits, hits


def _timed(batch_args, hook):
    t0 = time.perf_counter()
    if hook is None:
        outs = batch_call_original(*batch_args)
    else:
        outs = batch_call_original(*batch_args, pre_case_hook=hook)
    return time.perf_counter() - t0, outs


def main() -> int:
    from reschema.engine import load_manifest

    task = next(t for t in load_manifest() if t["task_id"] == "calc::gcc-O2-sym")
    params = [
        Param("lo", "i32", range=(-20, 10)),
        Param("hi", "i32", range=(10, 30)),
    ]
    fn = task["functions"]["sum_range"]
    cases = gen_inputs(params, __import__("random").Random(41), 64)
    args = (task["binary"], fn["addr"], params, cases)

    hook, bits, hits = _hook_recorder()
    plain_t, plain_out = _timed(args, None)
    hook_t, hook_out = _timed(args, hook)
    plain2_t, _ = _timed(args, None)
    hook2_t, _ = _timed(args, hook)

    # SPIKE'S NEGATIVE GATE: probe must not perturb verdicts
    assert [o.items() for o in plain_out] == [o.items() for o in hook_out], (
        "hooked batch diverged from plain batch — probe is perturbing verdicts"
    )

    median_plain = statistics.median([plain_t, plain2_t])
    median_hook = statistics.median([hook_t, hook2_t])
    ratio = median_hook / median_plain if median_plain else float("inf")
    edges = sum(bits)
    print(f"batch wall-clock, 64 cases ({args[0].split('/')[-1]}::sum_range):")
    print(f"  plain:   {plain_t:.3f}s / {plain2_t:.3f}s  (median {median_plain:.3f}s)")
    print(f"  hooked:  {hook_t:.3f}s / {hook2_t:.3f}s  (median {median_hook:.3f}s)")
    print(f"  ratio:   {ratio:.2f}x")
    print(f"  hook:    {hits[0]} block hits, {edges} distinct edges of {MASK}")
    verdict = (
        "FREE RIDE: substrate can attach to the paid executions"
        if ratio < 1.5
        else "DIAL REQUIRED: ship seeded 1-of-k subsampling with the honesty marker"
    )
    print(f"  VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
