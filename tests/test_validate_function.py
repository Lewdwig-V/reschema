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

# Correct void scale_buf: scales and clamps to [-100,100], like the original.
GOOD_SCALE = r"""
#include <stdint.h>
__attribute__((sysv_abi)) void scale_buf(int32_t *buf,int32_t n,int32_t factor){
    for(int32_t i=0;i<n;i++){ int32_t v=buf[i]*factor; buf[i]=v<-100?-100:v>100?100:v; }
}"""

# Writes nothing: only passes if the buffer readback ignores the declared direction.
NOOP_SCALE = r"""
#include <stdint.h>
__attribute__((sysv_abi)) void scale_buf(int32_t *buf,int32_t n,int32_t factor){ (void)buf;(void)n;(void)factor; }"""

# Correct void rot13: transforms the cstring in place.
GOOD_ROT13_STR = r"""
#include <stdint.h>
__attribute__((sysv_abi)) void rot13(char *s){
    for(char *p=s;*p;p++){ char c=*p;
        if(c>=97&&c<=122) *p=(char)(97+(c-97+13)%26);
        else if(c>=65&&c<=90) *p=(char)(65+(c-65+13)%26);
    }
}"""

# Writes nothing: must still be caught even when the cstring is (mis-)declared "in".
NOOP_ROT13 = r"""
#include <stdint.h>
__attribute__((sysv_abi)) void rot13(char *s){ (void)s; }"""

# Compiles fine but does not define the declared function.
MISSING_SYMBOL = r"""
#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){ return v; }"""

NOT_C = "this is not C"

SUM_PARAMS = [Param("lo", "i32", range=(-20, 10)), Param("hi", "i32", range=(10, 30))]
CLAMP_PARAMS = [Param("v", "i32"), Param("lo", "i32"), Param("hi", "i32")]
# 51*2 = 102 > 100: bad no-clamp scale diverges on the very first element.
# scale_buf/rot13 are void in the corpus: ret="void" on the first param (function-level
# attribute, carried on params[0]) so the validator skips the eax register scrape.
SCALE_PARAMS = [
    Param("buf", "buffer_i32", direction="in_out", length_param="n", range=(51, 100), ret="void"),
    Param("n", "i32", range=(3, 4)),
    Param("factor", "i32", range=(2, 5)),
]
ROT13_PARAMS = [Param("in_out", "cstring", direction="in_out", ret="void")]


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
    # The divergence must carry the fuzz seed so the failing input is reproducible.
    assert v.divergence["seed"] == 1


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


def test_param_ret_json_roundtrip():
    assert Param.from_json({"name": "s", "kind": "cstring", "ret": "void"}).ret == "void"
    assert Param.from_json({"name": "v", "kind": "i32"}).ret == "i32"


def test_true_scale_buf_void_accepted(manifest, tmp_path):
    """Correct void scale_buf must pass on mem alone; comparing eax (register garbage)
    falsely rejects it — that is how the blocker shipped green."""
    binary, addr = _slot(manifest, "calc", "scale_buf")
    v = validate_function(binary, addr, "scale_buf", SCALE_PARAMS, GOOD_SCALE, tmp_path / "m.so", seed=5, n_fuzz=16)
    assert v.ok, v.divergence
    assert v.compared == 16 and v.skipped == 0


def test_true_rot13_cstring_void_accepted(manifest, tmp_path):
    """Correct void rot13 (in_out cstring) must pass on mem alone."""
    binary, addr = _slot(manifest, "rot13", "rot13")
    v = validate_function(binary, addr, "rot13", ROT13_PARAMS, GOOD_ROT13_STR, tmp_path / "m.so", seed=6, n_fuzz=16)
    assert v.ok, v.divergence
    assert v.compared == 16 and v.skipped == 0


def test_buffer_declared_in_noop_model_rejected(manifest, tmp_path):
    """Wrong-spec flattery pin (i): buffer_i32 readback is direction-agnostic, so a
    write-nothing model cannot hide behind a buffer mis-declared 'in'."""
    binary, addr = _slot(manifest, "calc", "scale_buf")
    params = [
        Param("buf", "buffer_i32", direction="in", length_param="n", range=(51, 100), ret="void"),
        Param("n", "i32", range=(3, 4)),
        Param("factor", "i32", range=(2, 5)),
    ]
    v = validate_function(binary, addr, "scale_buf", params, NOOP_SCALE, tmp_path / "m.so", seed=1, n_fuzz=8)
    assert not v.ok
    assert v.divergence["field"] == "mem"


def test_cstring_declared_in_noop_model_rejected(manifest, tmp_path):
    """Wrong-spec flattery pin (ii): cstring readback must be direction-agnostic as well;
    otherwise a mis-declared pure-'in' cstring compares nothing and the no-op passes."""
    binary, addr = _slot(manifest, "rot13", "rot13")
    params = [Param("in_out", "cstring", direction="in", ret="void")]
    v = validate_function(binary, addr, "rot13", params, NOOP_ROT13, tmp_path / "m.so", seed=1, n_fuzz=8)
    assert not v.ok
    assert v.divergence["field"] == "mem"


def test_resubmit_same_path_stale_dlopen_cache(manifest, tmp_path):
    """A second submission to the SAME so_path must not pass the symbol gate on the first
    submission's cached dlopen image; the symbol check runs against a temp copy."""
    binary, addr = _slot(manifest, "calc", "sum_range")
    so = tmp_path / "m.so"
    v1 = validate_function(binary, addr, "sum_range", SUM_PARAMS, MODEL, so, seed=1, n_fuzz=4)
    assert v1.ok, v1.divergence
    v2 = validate_function(binary, addr, "sum_range", SUM_PARAMS, MISSING_SYMBOL, so, seed=1, n_fuzz=4)
    assert not v2.ok
    assert v2.divergence["stage"] == "symbol"


def test_verdict_reports_compared_and_skipped(manifest, tmp_path):
    """Otherwise near-starvation (1/64 compared) looks identical to a full 64/64 pass."""
    binary, addr = _slot(manifest, "calc", "sum_range")
    v = validate_function(binary, addr, "sum_range", SUM_PARAMS, MODEL, tmp_path / "m.so", seed=9, n_fuzz=12)
    assert v.ok, v.divergence
    assert v.compared == 12 and v.skipped == 0


def test_over_six_params_structured_arity_reject(manifest, tmp_path):
    """_guard_arity's NotImplementedError must not escape as a raw traceback."""
    binary, addr = _slot(manifest, "calc", "sum_range")
    params = [Param(f"a{i}", "i32") for i in range(7)]
    v = validate_function(binary, addr, "sum_range", params, MODEL, tmp_path / "m.so", seed=1, n_fuzz=2)
    assert not v.ok
    assert v.divergence["stage"] == "arity"
