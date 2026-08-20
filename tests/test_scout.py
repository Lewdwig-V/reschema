"""ISSUE-109A: the static-immediates scout slice.

The magic-value blind spot (#109): uniform `gen_inputs` over declared ranges
provably cannot reach sparse cmp-immediate branches, so a wrong-branch stub
can pass the function gate. The scout slice spends a harness-fixed share of
the SAME n_fuzz envelope on seeds carved fresh out of the original's own
.text — the binary is the pheromone: no cross-submission memory, no
entropy-policy violation, no symbolic execution.
"""

import pytest

from reschema.driver.spec import Param
from reschema.validate.function import validate_function
from reschema.validate.scout import scout_inputs, scrape_immediates

MAGIC_ORIG_C = r"""#include <stdint.h>
__attribute__((sysv_abi)) int32_t magic(int32_t x){
    if (x == 0x7E3A99B1) return 999;
    return 0;
}
int main(void){ return 0; }
"""
MAGIC_MAGIC = 0x7E3A99B1
# 0x8E3A99B1 >= 2^31: genuinely arrives under BOTH int32 readings
HI_MAGIC = 0x8E3A99B1
MAGIC_GOOD = MAGIC_ORIG_C
MAGIC_STUB = r"""#include <stdint.h>
__attribute__((sysv_abi)) int32_t magic(int32_t x){ (void)x; return 0; }
int main(void){ return 0; }
"""
MAGIC_PARAMS = [Param("x", "i32", range=(-10, 10))]


# --- scrape_immediates: carve constants out of the original's own .text ---


def test_scrape_immediates_finds_cmp_immediates_in_corpus(built_corpus):
    man = {t["task_id"]: t for t in built_corpus}
    t = man["check::gcc-O2-sym"]
    imms = scrape_immediates(
        t["binary"],
        t["functions"]["check_pw"]["addr"],
        t["functions"]["check_pw"]["size"],
    )
    assert 0x1F33E35F in imms  # check_pw's compare target constant
    imms_rot = scrape_immediates(
        man["rot13::gcc-O2-sym"]["binary"],
        man["rot13::gcc-O2-sym"]["functions"]["rot13_char"]["addr"],
        man["rot13::gcc-O2-sym"]["functions"]["rot13_char"]["size"],
    )
    # the a-z bound check compiles down to `cmp eax, 25` — 25 is the constant
    # form the threshold really has on this build; pin evidence, not hopes of 97
    assert 25 in imms_rot


def test_scout_inputs_reinterprets_per_kind():
    params = [
        Param("x", "i32", range=(-10, 10)),
        Param("s", "cstring", range=(-10, 10)),
        Param("buf", "buffer_i32", length_param="n", range=(-10, 10)),
        Param("n", "i32", range=(1, 10)),
    ]
    cases = scout_inputs(params, [HI_MAGIC])
    # EVERY i32 param takes the needle; >=2^31 arrives under BOTH its uint32
    # and its signed int32 reading (one case each).
    needles = {c["x"] for c in cases}
    assert needles == {HI_MAGIC, HI_MAGIC - 0x100000000}
    for c in cases:
        assert c["s"] == HI_MAGIC.to_bytes(4, "little") + b"\0"
        assert c["buf"] == [c["x"]]
        assert c["n"] == c["x"]


def test_scout_inputs_empty_immediates_yields_no_cases():
    assert scout_inputs([Param("x", "i32")], []) == []


def find_sym_addr(binary: str, name: str) -> tuple[int, int]:
    """Tiny in-test symbol lookup for the synthetic attack fixture."""
    from elftools.elf.elffile import ELFFile

    with open(binary, "rb") as f:
        symtab = ELFFile(f).get_section_by_name(".symtab")
        s = symtab.get_symbol_by_name(name)[0]
        return int(s["st_value"]), int(s["st_size"])


@pytest.fixture(scope="module")
def magic_binary(built_corpus, tmp_path_factory):
    # The synthetic original: its only behavioral branch is one sparse
    # magic-equality — the #109 hole, reproduced under test control.
    from reschema.validate.program import compile_model

    out = tmp_path_factory.mktemp("magic") / "magic"
    ok, err = compile_model(MAGIC_ORIG_C, out)
    assert ok, err
    return str(out)


def test_magic_branch_stub_exposed_only_by_scouts(magic_binary, tmp_path):
    addr, _size = find_sym_addr(magic_binary, "magic")
    # CONTROL: with the slice disabled, uniform draws (~2^-28 per draw over
    # int32) pass the stub — the actual hole #109 filed.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("reschema.validate.scout.SCOUT_SLICE_MAX", 0)
        v = validate_function(
            magic_binary,
            addr,
            "magic",
            MAGIC_PARAMS,
            MAGIC_STUB,
            tmp_path / "a",
            seed=1,
        )
        assert v.ok  # THE HOLE: blind uniform fuzz flatters the stub
    # DEFAULT slice active: scouts feed the magic input; the lie dies
    v = validate_function(
        magic_binary, addr, "magic", MAGIC_PARAMS, MAGIC_STUB, tmp_path / "b", seed=1
    )
    assert not v.ok
    assert v.divergence["field"] == "ret"
    # ...and the truthful magic model passes with scouts on (no false fire)
    v = validate_function(
        magic_binary, addr, "magic", MAGIC_PARAMS, MAGIC_GOOD, tmp_path / "c", seed=1
    )
    assert v.ok


def test_scout_slice_budget_is_harness_owned_and_bounded(magic_binary, tmp_path):
    addr, _size = find_sym_addr(magic_binary, "magic")
    v = validate_function(
        magic_binary, addr, "magic", MAGIC_PARAMS, MAGIC_GOOD, tmp_path / "d", seed=1
    )
    assert v.ok
    assert v.compared == 64  # envelope size unchanged by the slice
    # merged scout+uniform still feeds the diversity floor honestly:
    # scouts make the magic input survivable but distinct-count stays > 1
