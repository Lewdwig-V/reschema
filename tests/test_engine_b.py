import pytest

from reschema.corpus.generate import build
from reschema.engine import (
    TaskStore,
    compose,
    experiment_function,
    open_function_task,
    submit_function,
)

PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-20, 10]},
    {"name": "hi", "kind": "i32", "range": [10, 30]},
]
WRONG = '#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t a,int32_t b){return a+b;}'
RIGHT = '''#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}'''


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
