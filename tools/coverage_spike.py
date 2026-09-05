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
from contextlib import contextmanager

from reschema.driver.calling import batch_call_original, gen_inputs
from reschema.driver.spec import Param

MASK = 1 << 15  # 32KiB bitmap, AFL-flavored sparse collisions


def _hook_recorder():
    """One batch's totals, with independently owned per-VM hook scopes."""
    bits = bytearray(MASK)
    hits = [0]

    @contextmanager
    def scope(ql):
        prev = 0
        callback_error = None

        def check_error():
            if callback_error is not None:
                raise RuntimeError("coverage hook failed") from callback_error

        def before_case():
            nonlocal prev
            check_error()
            prev = 0  # no edge from the preceding case's final block

        def cb(ql, address, size):
            nonlocal prev, callback_error
            try:
                bits[(address ^ (prev >> 1)) & (MASK - 1)] = 1
                prev = address
                hits[0] += 1
            except Exception as exc:
                callback_error = exc
                raise

        handle = ql.hook_block(cb)
        try:
            yield before_case
            # _run_case converts emulation exceptions to target-fault traces;
            # a broken probe must instead fail this whole measurement.
            check_error()
        finally:
            ql.hook_del(handle)

    return scope, bits, hits


def _timed(batch_args, *, instrumented=False):
    t0 = time.perf_counter()
    if instrumented:
        scope, bits, hits = _hook_recorder()
        outs = batch_call_original(*batch_args, hook_scope=scope)
        elapsed = time.perf_counter() - t0
        stats = {"block_hits": hits[0], "edge_bins": sum(bits)}
    else:
        outs = batch_call_original(*batch_args)
        elapsed = time.perf_counter() - t0
        stats = None
    assert outs and all(o["batch_mode"] == "batched-snapshot" for o in outs), (
        "coverage spike requires a nonempty snapshot batch"
    )
    return elapsed, outs, stats


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

    plain_t, plain_out, _ = _timed(args)
    hook_t, hook_out, stats = _timed(args, instrumented=True)
    plain2_t, plain2_out, _ = _timed(args)
    hook2_t, hook2_out, stats2 = _timed(args, instrumented=True)

    # SPIKE'S NEGATIVE GATE: probe must not perturb verdicts
    assert plain_out == hook_out == plain2_out == hook2_out, (
        "hooked batch diverged from plain batch — probe is perturbing verdicts"
    )
    assert stats == stats2, "identical hooked trials produced different coverage"

    median_plain = statistics.median([plain_t, plain2_t])
    median_hook = statistics.median([hook_t, hook2_t])
    ratio = median_hook / median_plain if median_plain else float("inf")
    print(f"batch wall-clock, 64 cases ({args[0].split('/')[-1]}::sum_range):")
    print(f"  plain:   {plain_t:.3f}s / {plain2_t:.3f}s  (median {median_plain:.3f}s)")
    print(f"  hooked:  {hook_t:.3f}s / {hook2_t:.3f}s  (median {median_hook:.3f}s)")
    print(f"  ratio:   {ratio:.2f}x")
    for trial, measured in enumerate((stats, stats2), 1):
        print(
            f"  hook {trial}: {measured['block_hits']} block hits, "
            f"{measured['edge_bins']} occupied edge bins of {MASK}"
        )
    verdict = (
        "FREE RIDE: substrate can attach to the paid executions"
        if ratio < 1.5
        else "DIAL REQUIRED: ship seeded 1-of-k subsampling with the honesty marker"
    )
    print(f"  VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
