"""Crash-gap census pins: correct specs never fault the corpus originals;
every skip is a pointer-as-i32 spec artifact, deterministic on re-run."""

from reschema.corpus.generate import FUNCS
from tools.crash_census import REF, _sketch, census


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


def test_ref_specs_never_fault_originals(built_corpus):
    for func, params in REF.items():
        t = _slot(built_corpus, func)
        row = census(t, func, t["functions"][func], params)
        assert row["skipped"] == 0, (func, row)


def test_sketch_pointer_artifacts_skip_deterministically(built_corpus):
    t = _slot(built_corpus, "scale_buf")
    row = census(t, "scale_buf", t["functions"]["scale_buf"], _sketch(REF["scale_buf"]))
    # Partial thinning: junk-pointer cases skip; n<=0 cases survive (loop never derefs).
    assert 0 < row["skipped"] < row["n"], row
    assert row["nondeterministic"] == 0, row
