import pytest

from reschema.corpus.generate import OUT_ROOT, build
from reschema.exec.recorder import record


@pytest.fixture(scope="module")
def manifest():
    return build()


def _slot(manifest, seed, opt="-O2", compiler="gcc", stripped=False):
    return next(x["binary"] for x in manifest if x["seed"] == seed and x["opt"] == opt
                and x["compiler"] == compiler and x["stripped"] == stripped)


def test_rot13_golden(manifest):
    t = record(_slot(manifest, "rot13"), ["hello"], b"")
    assert t["exit_code"] == 0
    assert bytes.fromhex(t["stdout"]).decode() == "uryyb\n"


def test_check_rejects_wrong_password(manifest):
    t = record(_slot(manifest, "check"), [], b"hunter2\n")
    assert t["exit_code"] == 1
    assert t["stdout"] == b"NOPE\n".hex()
    scs = [e["sc"] for e in t["events"] if e["phase"] == "enter"]
    assert "write" in scs and "exit_group" in scs


def test_check_accepts_correct_password_stdin_roundtrip_and_determinism(manifest):
    # pre-image `txy"od` for djb2-5381 -> 0x1F33E35F, verified natively against
    # .reschema/corpus/check/gcc-O2-sym/prog: `printf 'txy"od\n' | ./prog` -> "OK", exit 0.
    pw = b'txy"od\n'
    t = record(_slot(manifest, "check"), [], pw)
    assert t["exit_code"] == 0
    assert t["stdout"] == b"OK\n".hex()
    assert bytes.fromhex(t["stdin_hex"]) == pw
    t2 = record(_slot(manifest, "check"), [], pw)
    for key in ("stdout", "stderr", "exit_code", "events"):
        assert t2[key] == t[key]


def test_manifest_has_expected_slot(manifest):
    assert len(manifest) == 36
    assert OUT_ROOT.joinpath("manifest.json").exists()
