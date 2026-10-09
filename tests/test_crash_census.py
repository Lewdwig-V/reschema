"""Crash-gap census pins: correct specs never fault the corpus originals;
every skip is a pointer-as-i32 spec artifact, deterministic on re-run."""

import pytest

from reschema.corpus.generate import FUNCS
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
    # Precondition of a zero-threshold skip floor: the day a seed genuinely
    # faults under its true spec, this fails and forces the crash-preservation
    # decision (ARCHITECTURE.md ADR "Original faults stay skipped").
    for t in built_corpus:
        if func in t["functions"]:
            row = census(t, func, t["functions"][func], REF[func])
            assert row["skipped"] == 0, (t["task_id"], func, row)


def test_mistyped_pointer_artifacts_skip_deterministically(built_corpus):
    t = _slot(built_corpus, "scale_buf")
    row = census(
        t, "scale_buf", t["functions"]["scale_buf"], _mistyped(REF["scale_buf"])
    )
    # Partial thinning: junk-pointer cases skip; n<=0 cases survive (loop never derefs).
    assert 0 < row["skipped"] < row["n"], row
    assert row["nondeterministic"] == 0, row


@pytest.mark.xfail(
    strict=True, reason="open gap: skip-ratio floor not shipped (roadmap)"
)
def test_mistyped_spec_stub_rejected(built_corpus, tmp_path):
    # Today the gate ACCEPTS this on all 12 scale_buf slots (fresh seeds).
    # The floor turns it into a stage:spec reject; strict xfail then XPASSes
    # and fails the run until this marker is removed.
    t = _slot(built_corpus, "scale_buf")
    f = t["functions"]["scale_buf"]
    v = validate_function(
        t["binary"],
        f["addr"],
        "scale_buf",
        _mistyped(REF["scale_buf"]),
        SCALE_STUB,
        tmp_path / "m.so",
        seed=7,
        size=f["size"],
    )
    assert not v.ok and v.divergence["stage"] == "spec", v
