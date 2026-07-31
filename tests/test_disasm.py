import pytest

from reschema.corpus.generate import build
from reschema.disasm.slice import disasm_function


@pytest.fixture(scope="module")
def manifest():
    return build()


def test_slice_content(manifest):
    t = next(
        x
        for x in manifest
        if x["seed"] == "rot13" and x["opt"] == "-O2" and not x["stripped"]
    )
    addr = t["functions"]["rot13"]
    txt = disasm_function(t["binary"], addr)
    lines = txt.splitlines()
    assert lines, "slice is empty"
    assert lines[0].startswith(f"0x{addr:x}:\t")
    assert all("\t" in line for line in lines)  # addr:\tmnemonic\top_str
    assert "ret" in txt or "jmp" in txt  # ends at a function boundary


def test_all_slots_all_functions_slicable(manifest):
    for x in manifest:
        for name, addr in x["functions"].items():
            txt = disasm_function(x["binary"], addr)
            lines = txt.splitlines()
            assert lines, f"empty slice for {x['task_id']}::{name}"
            assert lines[0].startswith(f"0x{addr:x}:\t")


def test_sym_and_stripped_slices_identical(manifest):
    # Addresses are captured pre-strip, so both variants slice the same .text.
    seen = {(x["compiler"], x["opt"], x["stripped"]): x for x in manifest}
    for (cc, opt, stripped), sym_slot in seen.items():
        if stripped:
            continue
        stripped_slot = seen.get((cc, opt, True))
        assert stripped_slot is not None
        for addr in sym_slot["functions"].values():
            assert disasm_function(sym_slot["binary"], addr) == disasm_function(
                stripped_slot["binary"], addr
            )
