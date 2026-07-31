import json
from types import SimpleNamespace

import pytest

from reschema.corpus.generate import build
from reschema.driver.spec import Param
from reschema.engine import (
    TaskStore,
    compose,
    experiment_function,
    open_function_task,
    submit_function,
)
from reschema.validate.function import N_FUZZ

PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-20, 10]},
    {"name": "hi", "kind": "i32", "range": [10, 30]},
]
WRONG = '#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t a,int32_t b){return a+b;}'
RIGHT = '''#include <stdint.h>
static int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}'''
# Re-accept variant: same semantics, different source text (helper order flipped).
RIGHT2 = '''#include <stdint.h>
static int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v>hi?hi:v<lo?lo:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}'''
# Rule violation for the dup-symbol path: single-function helper left EXPORTED.
RIGHT_EXPORTED_HELPER = '''#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}'''
# clamp_i32 as the accepted function itself (exported — matches -shared model compiles).
CLAMP = '''#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}'''
CLAMP_PARAMS = [
    {"name": "v", "kind": "i32", "range": [-100, 100]},
    {"name": "lo", "kind": "i32", "range": [-50, 0]},
    {"name": "hi", "kind": "i32", "range": [0, 50]},
]


@pytest.fixture(scope="module")
def manifest():
    return build()


@pytest.fixture
def store(manifest):
    st = TaskStore("calc::gcc-O2-sym")
    # task dir is shared runtime state; reset so reruns see the default ledger
    st._path("ledger.json").unlink(missing_ok=True)
    return st


def test_open_function_task_returns_disasm_slice(store):
    t = open_function_task(store, "sum_range")
    assert t["task_id"] == "calc::gcc-O2-sym" and t["function"] == "sum_range"
    assert t["address"] == hex(store.meta["functions"]["sum_range"]["addr"])
    assert t["disasm"].startswith("0x") and "ret" in t["disasm"]


def test_experiment_function_forwards_whole_trace(store):
    t = experiment_function(store, "sum_range", PARAMS, {"lo": -5, "hi": 30})
    assert t["exit_code"] == 0
    # -5..30 sums to 450 (inside the +-1000 clamp band, so no clamping)
    assert t["ret"] == 450
    assert "mem" in t and "events" in t


def test_submit_function_rejects_then_accepts(store):
    bad = submit_function(store, "sum_range", PARAMS, WRONG, seed=1)
    assert not bad["accepted"]
    assert bad["divergence"]["field"] == "ret"
    led = store.ledger()
    assert led["submissions"] == 1 and led["rejections"] == 1 and led["accepted"] == []
    good = submit_function(store, "sum_range", PARAMS, RIGHT, seed=1)
    assert good == {"accepted": True}
    led = store.ledger()
    assert led["submissions"] == 2 and led["rejections"] == 1
    assert led["accepted"] == [{"sum_range": RIGHT}]


def test_submit_function_no_duplicate_ledger_entries(store):
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=1)["accepted"]
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=2)["accepted"]
    assert len(store.ledger()["accepted"]) == 1


def test_compose_compiles_accepted_sources(store):
    assert compose(store)[0] is False  # nothing accepted yet
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=1)["accepted"]
    ok, err = compose(store)
    assert ok, err


def test_compose_links_per_tu_with_static_helpers(store):
    # The calc canonical scenario: clamp_i32 accepted standalone (exported — model
    # compiles are -shared, so non-static functions export), sum_range's accepted
    # model embeds clamp_i32 as a STATIC helper. Per-TU compile + link must succeed.
    assert submit_function(store, "clamp_i32", CLAMP_PARAMS, CLAMP, seed=1, n_fuzz=8)["accepted"]
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=8)["accepted"]
    ok, err = compose(store)
    assert ok, err
    assert (store.dir / "composed").exists()
    assert "int main" in (store.dir / "composed_main.c").read_text()


def test_compose_duplicate_exported_symbol_structured_reject(store):
    # Same EXTERNALLY-visible helper name across two accepted sources → ld failure
    # mapped to a structured, actionable reject.
    assert submit_function(store, "clamp_i32", CLAMP_PARAMS, CLAMP, seed=1, n_fuzz=8)["accepted"]
    assert submit_function(store, "sum_range", PARAMS, RIGHT_EXPORTED_HELPER, seed=1, n_fuzz=8)[
        "accepted"
    ]
    ok, err = compose(store)
    assert not ok
    assert "duplicate symbol" in err and "clamp_i32" in err and "static" in err


def test_experiment_function_mem_hex_json_safe(manifest):
    # rot13's `rot13` is a void cstring transform: the driver's mem readback is raw
    # bytes, which must not leak into the engine result (json.dumps would TypeError).
    st = TaskStore("rot13::gcc-O2-sym")
    t = experiment_function(
        st, "rot13", [{"name": "in_out", "kind": "cstring", "ret": "void"}], {"in_out": b"hello"}
    )
    assert t["exit_code"] == 0
    assert t["mem"]["in_out"] == b"uryyb".hex()  # hex, mirrors stdin_hex/stdout_hex
    json.dumps(t)


def test_submit_function_reaccept_newest_source_wins(store):
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=1)["accepted"]
    assert submit_function(store, "sum_range", PARAMS, RIGHT2, seed=1)["accepted"]
    assert store.ledger()["accepted"] == [{"sum_range": RIGHT2}]


def test_open_function_task_unknown_function_lists_available(store):
    with pytest.raises(KeyError) as exc_info:
        open_function_task(store, "no_such_fn")
    msg = str(exc_info.value)
    assert "calc::gcc-O2-sym" in msg and "clamp_i32" in msg and "available" in msg


def test_taskstore_unknown_task_lists_available(manifest):
    with pytest.raises(KeyError) as exc_info:
        TaskStore("no_such_task")
    msg = str(exc_info.value)
    assert "no_such_task" in msg and "calc::" in msg and "available" in msg


def test_param_from_json_names_missing_key():
    with pytest.raises(KeyError) as exc_info:
        Param.from_json({"kind": "i32"})
    msg = str(exc_info.value)
    assert "'name'" in msg and "'kind': 'i32'" in msg  # the missing key AND the spec


def test_submit_function_malformed_spec_counts_rejection(store):
    # Phantom kind: structured reject BEFORE the fuzz loop, ledger still accounted.
    r = submit_function(store, "sum_range", [{"name": "lo", "kind": "buffer_u8"}], RIGHT, seed=1)
    assert r["accepted"] is False and r["reason"] == "spec"
    assert "buffer_u8" in r["detail"]
    r2 = submit_function(store, "sum_range", [{"kind": "i32"}], RIGHT, seed=1)  # missing 'name'
    assert r2["accepted"] is False and r2["reason"] == "spec"
    led = store.ledger()
    assert led["submissions"] == 2 and led["rejections"] == 2


def test_submit_function_clamps_n_fuzz(store, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "reschema.engine.validate_function",
        lambda *a, **kw: (
            seen.update(kw),
            SimpleNamespace(ok=False, divergence={"stage": "probe"}),
        )[1],
    )
    r = submit_function(store, "sum_range", PARAMS, RIGHT, n_fuzz=10**9)
    assert seen["n_fuzz"] == 4 * N_FUZZ and r["accepted"] is False
    assert store.ledger()["rejections"] == 1
