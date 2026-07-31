import pytest

from reschema.corpus.generate import build
from reschema.driver.spec import Param
from reschema.validate.function import validate_function

MODEL = r"""
#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){
    return v<lo?lo:v>hi?hi:v;
}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){
    int32_t s=0; for(int32_t i=lo;i<=hi;i++) s=clamp_i32(s+i,-1000,1000); return s;
}"""

GOOD_ROT = r"""
#include <stdint.h>
__attribute__((sysv_abi)) int32_t rot13_char(int32_t c){
    if(c>=97&&c<=122) return 97+(c-97+13)%26;
    if(c>=65&&c<=90) return 65+(c-65+13)%26;
    return c;
}"""

# Naive model: constant return, ignores the sum entirely.
BAD_SUM = r"""
#include <stdint.h>
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){ return 42; }"""

# Multiplies but never clamps: same ret shape, wrong memory effects.
BAD_SCALE = r"""
#include <stdint.h>
__attribute__((sysv_abi)) int32_t scale_buf(int32_t *buf,int32_t n,int32_t factor){
    for(int32_t i=0;i<n;i++) buf[i]*=factor; return 0;
}"""

# Compiles fine but does not define the declared function.
MISSING_SYMBOL = r"""
#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){ return v; }"""

NOT_C = "this is not C"

SUM_PARAMS = [Param("lo", "i32", range=(-20, 10)), Param("hi", "i32", range=(10, 30))]
CLAMP_PARAMS = [Param("v", "i32"), Param("lo", "i32"), Param("hi", "i32")]
# 51*2 = 102 > 100: bad no-clamp scale diverges on the very first element.
SCALE_PARAMS = [
    Param("buf", "buffer_i32", direction="in_out", length_param="n", range=(51, 100)),
    Param("n", "i32", range=(3, 4)),
    Param("factor", "i32", range=(2, 5)),
]


def _slot(manifest, seed, func):
    t = next(
        x
        for x in manifest
        if x["seed"] == seed
        and x["opt"] == "-O2"
        and x["stripped"] is False
        and x["compiler"] == "gcc"
    )
    return t["binary"], t["functions"][func]["addr"]


@pytest.fixture(scope="module")
def manifest():
    return build()


def test_true_sum_range_accepted(manifest, tmp_path):
    binary, addr = _slot(manifest, "calc", "sum_range")
    v = validate_function(binary, addr, "sum_range", SUM_PARAMS, MODEL, tmp_path / "m.so", seed=1, n_fuzz=16)
    assert v.ok, v.divergence


def test_true_clamp_i32_accepted(manifest, tmp_path):
    binary, addr = _slot(manifest, "calc", "clamp_i32")
    v = validate_function(binary, addr, "clamp_i32", CLAMP_PARAMS, MODEL, tmp_path / "m.so", seed=2, n_fuzz=16)
    assert v.ok, v.divergence


def test_true_rot13_char_accepted(manifest, tmp_path):
    binary, addr = _slot(manifest, "rot13", "rot13_char")
    params = [Param("c", "i32", range=(32, 126))]
    v = validate_function(binary, addr, "rot13_char", params, GOOD_ROT, tmp_path / "m.so", seed=3, n_fuzz=16)
    assert v.ok, v.divergence


def test_constant_model_rejected(manifest, tmp_path):
    binary, addr = _slot(manifest, "calc", "sum_range")
    v = validate_function(binary, addr, "sum_range", SUM_PARAMS, BAD_SUM, tmp_path / "m.so", seed=1, n_fuzz=8)
    assert not v.ok
    assert v.divergence["field"] == "ret"
    assert "input" in v.divergence and "expected" in v.divergence and "actual" in v.divergence


def test_wrong_memory_effects_rejected(manifest, tmp_path):
    binary, addr = _slot(manifest, "calc", "scale_buf")
    v = validate_function(binary, addr, "scale_buf", SCALE_PARAMS, BAD_SCALE, tmp_path / "m.so", seed=1, n_fuzz=8)
    assert not v.ok
    assert v.divergence["field"] == "mem"


def test_compile_failure_rejected(manifest, tmp_path):
    binary, addr = _slot(manifest, "calc", "sum_range")
    v = validate_function(binary, addr, "sum_range", SUM_PARAMS, NOT_C, tmp_path / "m.so", seed=1, n_fuzz=8)
    assert not v.ok
    assert v.divergence["stage"] == "compile"


def test_missing_symbol_rejected(manifest, tmp_path):
    binary, addr = _slot(manifest, "calc", "sum_range")
    v = validate_function(binary, addr, "sum_range", SUM_PARAMS, MISSING_SYMBOL, tmp_path / "m.so", seed=1, n_fuzz=8)
    assert not v.ok
    assert v.divergence["stage"] == "symbol"


def test_all_original_faults_reject_skip_starvation(manifest, tmp_path):
    # Bogus function address: original faults on every fuzz case. Skipping all cases
    # must be a loud rejection, not a vacuous pass.
    binary, _ = _slot(manifest, "calc", "sum_range")
    v = validate_function(binary, 0x111, "sum_range", SUM_PARAMS, MODEL, tmp_path / "m.so", seed=1, n_fuzz=4)
    assert not v.ok
    assert v.divergence["stage"] == "skip-starvation"
