import re

import pytest
from elftools.elf.elffile import ELFFile

from reschema.corpus.generate import build


@pytest.fixture(scope="module")
def manifest():
    return build()


def _gcc_slot(m, seed, opt, stripped):
    for x in m:
        if (
            x["seed"] == seed
            and x["compiler"] == "gcc"
            and x["opt"] == opt
            and x["stripped"] == stripped
        ):
            return x
    pytest.skip(f"no gcc slot for {seed} {opt} stripped={stripped}")


def test_corpus_builds_with_addresses(manifest):
    m = manifest
    assert len(m) >= 6 * 3, "expect >= 6 compiler-opt combos x seeds (minus missing compilers)"
    for x in m:
        assert re.fullmatch(
            r"[a-z0-9]+::(gcc|clang)-O[0-2]-(sym|stripped)", x["task_id"]
        ), x["task_id"]
    rot = _gcc_slot(m, "rot13", "-O2", False)
    assert rot["functions"]["rot13"] != 0
    stripped = _gcc_slot(m, "rot13", "-O2", True)
    assert stripped["functions"]["rot13"] != 0  # address captured pre-strip


def test_stripped_has_no_symtab(manifest):
    s = next(x for x in manifest if x["stripped"])
    with open(s["binary"], "rb") as f:
        assert ELFFile(f).get_section_by_name(".symtab") is None
