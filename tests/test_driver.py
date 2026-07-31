import random
import subprocess

import pytest

from reschema.corpus.generate import build
from reschema.driver.calling import call_model_native, call_original, gen_inputs
from reschema.driver.spec import Param

MODEL = r"""
#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi){
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo, int32_t hi){
    int32_t s=0; for(int32_t i=lo;i<=hi;i++) s=clamp_i32(s+i,-1000,1000); return s;
}"""


@pytest.fixture(scope="module")
def manifest():
    return build()


def _slot(manifest, seed, func):
    t = next(
        x
        for x in manifest
        if x["seed"] == seed
        and x["opt"] == "-O2"
        and not x["stripped"]
        and x["compiler"] == "gcc"
    )
    return t["binary"], t["functions"][func]["addr"]


def _pw_hash_ref(s: bytes) -> int:
    h = 5381
    for c in s.rstrip(b"\0"):
        h = (h * 33 + c) & 0xFFFFFFFF
    return h


def _as_int32(v: int) -> int:
    return v - 0x100000000 if v & 0x80000000 else v


def test_rot13_char_ground_truth(manifest):
    binary, addr = _slot(manifest, "rot13", "rot13_char")
    params = [Param("c", "i32")]
    for cin, cout in (("a", "n"), ("n", "a"), ("z", "m"), ("A", "N"), ("!", "!")):
        out = call_original(binary, addr, params, {"c": ord(cin)})
        assert out["ret"] & 0xFF == ord(cout)


def test_rot13_in_out_memory(manifest):
    binary, addr = _slot(manifest, "rot13", "rot13")
    params = [Param("in_out", "cstring", direction="in_out")]
    case = {"in_out": b"hello\x00"}
    out = call_original(binary, addr, params, case)
    assert out["mem"]["in_out"] == b"uryyb\x00"


def test_pw_hash_matches_reference(manifest):
    binary, addr = _slot(manifest, "check", "pw_hash")
    params = [Param("s", "cstring")]
    for s in (b"abc\0", b"hello world\0", b"\0"):
        out = call_original(binary, addr, params, {"s": s})
        assert out["ret"] == _as_int32(_pw_hash_ref(s))


def test_check_pw_rejects_wrong_password(manifest):
    binary, addr = _slot(manifest, "check", "check_pw")
    params = [Param("s", "cstring")]
    out = call_original(binary, addr, params, {"s": b"abc\0"})
    assert out["ret"] == 0


def test_scale_buf_in_out_buffer(manifest):
    binary, addr = _slot(manifest, "calc", "scale_buf")
    params = [
        Param("buf", "buffer_i32", direction="in_out", length_param="n"),
        Param("n", "i32"),
        Param("factor", "i32"),
    ]
    case = {"buf": [1, -50, 10, 5], "n": 4, "factor": 3}
    out = call_original(binary, addr, params, case)
    assert out["mem"]["buf"] == [3, -100, 30, 15]


def test_sum_range_matches_native_model(manifest, tmp_path):
    binary, addr = _slot(manifest, "calc", "sum_range")
    so = tmp_path / "m.so"
    subprocess.run(
        ["gcc", "-O1", "-shared", "-fPIC", "-x", "c", "-", "-o", str(so)],
        input=MODEL.encode(),
        check=True,
    )
    params = [Param("lo", "i32", range=(-20, 10)), Param("hi", "i32", range=(10, 30))]
    for case in gen_inputs(params, random.Random(1), 4):
        assert case["lo"] <= case["hi"]
        a = call_original(binary, addr, params, case)
        b = call_model_native(str(so), "sum_range", params, case)
        assert a["ret"] == b["ret"]


def test_clamp_i32_ground_truth(manifest):
    binary, addr = _slot(manifest, "calc", "clamp_i32")
    params = [Param("v", "i32"), Param("lo", "i32"), Param("hi", "i32")]
    for v, lo, hi, want in ((5, 0, 10, 5), (-5, 0, 10, 0), (50, 0, 10, 10)):
        out = call_original(binary, addr, params, {"v": v, "lo": lo, "hi": hi})
        assert out["ret"] == want
