"""Crash-gap census pins: on the pinned default-range draw, correct specs
never fault the corpus originals and every skip is a deterministic
pointer-as-i32 artifact; correct types over a wide declared range DO fault
(timeout), so faults are not purely a typing signal."""

import pytest

from reschema.corpus.generate import FUNCS
from reschema.driver.calling import batch_call_original
from reschema.driver.spec import Param
from reschema.validate.function import validate_function
from tools.crash_census import REF, _mistyped, census

# Stub tuned to scale_buf's mistyped survivors: the original leaves eax == 0
# on every n<=0 case, on every compiler/opt (measured 2026-10-09).
SCALE_STUB = (
    "#include <stdint.h>\n"
    "__attribute__((sysv_abi)) int32_t scale_buf(int32_t buf, int32_t n, "
    "int32_t factor) { return 0; }\n"
)


def _slot(manifest, func):
    return next(
        t
        for t in manifest
        if func in t["functions"]
        and t["opt"] == "-O2"
        and t["compiler"] == "gcc"
        and not t["stripped"]
    )


def test_ref_covers_every_corpus_function():
    assert set(REF) == {f for fns in FUNCS.values() for f in fns}


@pytest.mark.parametrize("func", sorted(REF))
def test_ref_specs_never_fault_originals(built_corpus, func):
    # SAMPLED tripwire, not proof (codex P2 on #140): one pinned draw at
    # default ranges. A seed that faults on this draw fails it and reopens the
    # ADR "Original faults stay skipped"; a fault outside the draw does not.
    for t in built_corpus:
        if func in t["functions"]:
            row = census(t, func, t["functions"][func], REF[func])
            assert row["skipped"] == 0, (t["task_id"], func, row)


def test_true_spec_faults_on_declared_range(built_corpus):
    # Deterministic known-fault case the sampled census misses: correct types,
    # agent-declared full-i32 range -> ~4e9 iterations -> timeout. Any skip
    # floor must NOT count this class (39/64 of a full-range draw time out).
    t = _slot(built_corpus, "sum_range")
    params = [Param("lo", "i32"), Param("hi", "i32")]
    case = {"lo": -(2**31), "hi": 2**31 - 1}
    (out,) = batch_call_original(
        t["binary"], t["functions"]["sum_range"]["addr"], params, [case]
    )
    assert out["exit_code"] == -1 and out["events"][-1]["sc"] == "timeout", out


def test_mistyped_pointer_artifacts_skip_deterministically(built_corpus):
    t = _slot(built_corpus, "scale_buf")
    row = census(
        t, "scale_buf", t["functions"]["scale_buf"], _mistyped(REF["scale_buf"])
    )
    # Partial thinning: junk-pointer cases skip; n<=0 cases survive (loop never derefs).
    assert 0 < row["skipped"] < row["n"], row
    assert row["nondeterministic"] == 0, row


@pytest.mark.parametrize(
    "buf_range",
    [
        (-100, 100),  # junk low addresses: UC_ERR_*_UNMAPPED
        # static -no-pie image: .text reads fine, the store hits
        # UC_ERR_WRITE_PROT (prose "Write to write-protected memory" — the
        # pre-errno classifier missed it and the stub was ACCEPTED on all 12)
        (0x401000, 0x401FFF),
    ],
)
def test_mistyped_spec_stub_rejected(built_corpus, tmp_path, buf_range):
    # Before the skip floor the gate ACCEPTED this on all 12 scale_buf slots
    # (fresh seeds). Any memory fault on a declared case is now a stage:spec
    # reject, whatever the source.
    t = _slot(built_corpus, "scale_buf")
    f = t["functions"]["scale_buf"]
    params = _mistyped(REF["scale_buf"])
    params[0] = Param("buf", "i32", range=buf_range)
    v = validate_function(
        t["binary"],
        f["addr"],
        "scale_buf",
        params,
        SCALE_STUB,
        seed=7,
        size=f["size"],
    )
    assert not v.ok and v.divergence["stage"] == "spec", v
    assert "buffer_i32" in v.divergence["detail"], v
