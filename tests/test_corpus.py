import re

import pytest
from elftools.elf.elffile import ELFFile


@pytest.fixture(scope="module")
def manifest(built_corpus):
    return built_corpus


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
    assert len(m) >= 6 * 3, (
        "expect >= 6 compiler-opt combos x seeds (minus missing compilers)"
    )
    for x in m:
        assert re.fullmatch(
            r"[a-z0-9]+::(gcc|clang)-O[0-2]-(sym|stripped)", x["task_id"]
        ), x["task_id"]
    rot = _gcc_slot(m, "rot13", "-O2", False)
    assert rot["functions"]["rot13"]["addr"] != 0
    stripped = _gcc_slot(m, "rot13", "-O2", True)
    assert stripped["functions"]["rot13"]["addr"] != 0  # address captured pre-strip


def test_pkfmt_function_address_and_size(manifest):
    # Historical edge counts need rerunning after the hook-lifecycle repair;
    # they are not a test oracle.
    # function-mode surface is deliberately ONLY the representable i32
    # function (pointer-buffer helpers await pointer-kind sketch inference).
    p = _gcc_slot(manifest, "pkfmt", "-O2", False)
    assert set(p["functions"]) == {"pk_version_ok"}
    for f in p["functions"].values():
        assert f["addr"] and f["size"] and f["size"] > 8


def test_stripped_has_no_symtab(manifest):
    s = next(x for x in manifest if x["stripped"])
    with open(s["binary"], "rb") as f:
        assert ELFFile(f).get_section_by_name(".symtab") is None


def test_load_manifest_refuses_a_stale_canonicalizer(tmp_path):
    # A rules change means a corpus rebuild: ground truth recorded under other
    # canonicalisation rules must never be judged against current ones.
    from reschema.corpus.generate import load_manifest
    from reschema.exec.canonical import CANONICALIZER_VERSION

    (tmp_path / "manifest.json").write_text("[]")
    with pytest.raises(RuntimeError, match="canonicalizer \\(missing\\)"):
        load_manifest(tmp_path)
    (tmp_path / "canonicalizer_version").write_text("1.0")
    with pytest.raises(RuntimeError, match="canonicalizer 1.0"):
        load_manifest(tmp_path)
    (tmp_path / "canonicalizer_version").write_text(CANONICALIZER_VERSION)
    assert load_manifest(tmp_path) == []


def test_unknown_slot_lists_the_available_ones(manifest):
    from reschema.corpus.generate import slot

    with pytest.raises(KeyError, match="unknown task_id: nope::x.*calc::"):
        slot("nope::x")
