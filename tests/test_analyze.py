import pytest

from reschema.disasm.analyze import analyze_function

# Ground truth from the seed sources: (arity, returns-value, callees in order).
EXPECT = {
    "rot13_char": (1, True, []),
    "rot13": (1, False, ["rot13_char"]),
    "pw_hash": (1, True, []),
    "check_pw": (1, True, ["pw_hash"]),
    "clamp_i32": (3, True, []),
    "sum_range": (2, True, ["clamp_i32"]),
    "scale_buf": (3, False, ["clamp_i32"]),
    "xform_byte": (2, True, []),
}


@pytest.fixture(scope="module")
def manifest(built_corpus):
    return built_corpus


def test_matrix_arity_returns_callees(manifest):
    # Every compiler x opt x sym/stripped slot: guesses must match the seed
    # sources' call graphs and signatures (the corpus is the ground truth for
    # the heuristic).
    for slot in manifest:
        facts = analyze_function(slot["binary"], slot["functions"])
        for fn, (arity, ret, callees) in EXPECT.items():
            if fn not in facts:
                continue  # slot's seed only carries its own functions
            f = facts[fn]
            assert f["arity_guess"] == arity, (slot["task_id"], fn, "arity")
            assert f["returns_hint"] == ret, (slot["task_id"], fn, "returns")
            assert [c["name"] for c in f["callees"]] == callees, (
                slot["task_id"],
                fn,
                "callees",
            )


def test_facts_labeled_as_guess(manifest):
    slot = next(s for s in manifest if s["task_id"] == "calc::gcc-O2-sym")
    facts = analyze_function(slot["binary"], slot["functions"])
    assert "heuristic" in facts["scale_buf"]["labeled"]
