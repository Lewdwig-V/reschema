import pytest

from reschema.disasm.analyze import disasm_function


@pytest.fixture(scope="module")
def manifest(built_corpus):
    return built_corpus


def test_slice_content(manifest):
    t = next(
        x
        for x in manifest
        if x["seed"] == "rot13" and x["opt"] == "-O2" and not x["stripped"]
    )
    fn = t["functions"]["rot13"]
    txt = disasm_function(t["binary"], fn["addr"], fn["size"])
    lines = txt.splitlines()
    assert len(lines) > 1, "slice should be non-trivial"
    assert lines[0].startswith(f"0x{fn['addr']:x}:\t")
    assert all("\t" in line for line in lines)  # addr:\tmnemonic\top_str
    last_addr = int(lines[-1].split(":")[0], 16)
    assert fn["addr"] <= last_addr < fn["addr"] + fn["size"]


def test_all_slots_all_functions_slicable(manifest):
    for x in manifest:
        for name, fn in x["functions"].items():
            txt = disasm_function(x["binary"], fn["addr"], fn["size"])
            lines = txt.splitlines()
            assert lines, f"empty slice for {x['task_id']}::{name}"
            assert lines[0].startswith(f"0x{fn['addr']:x}:\t")


def test_sym_and_stripped_slices_identical(manifest):
    # Addresses are captured pre-strip, so both variants slice the same .text.
    seen = {(x["seed"], x["compiler"], x["opt"], x["stripped"]): x for x in manifest}
    for (seed, cc, opt, stripped), sym_slot in seen.items():
        if stripped:
            continue
        stripped_slot = seen.get((seed, cc, opt, True))
        assert stripped_slot is not None
        for fn in sym_slot["functions"].values():
            assert disasm_function(
                sym_slot["binary"], fn["addr"], fn["size"]
            ) == disasm_function(stripped_slot["binary"], fn["addr"], fn["size"])


def test_addr_outside_text_raises(manifest):
    t = manifest[0]
    with pytest.raises(ValueError, match="outside .text"):
        disasm_function(t["binary"], 0x10, 16)


def test_addr_plus_size_beyond_text_raises(manifest):
    # addr valid but the end of the slice runs past .text: same structured error.
    t = manifest[0]
    fn = next(iter(t["functions"].values()))
    with pytest.raises(ValueError, match="outside .text"):
        disasm_function(t["binary"], fn["addr"], 10**9)
