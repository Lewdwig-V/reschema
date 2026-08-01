"""Dogfood (spec §9): the harness driven through its own MCP interface end-to-end.

A scripted mini-agent runs the blind loop — corpus_build → task_open → experiment →
submit_model → status — over MCP tools only (compose is deliberately not a tool,
spec §5, so the stitch check drives the engine directly). Scenarios pin spec §11:
criterion 1 (level-B rejection-repair accepted on -O2), the hardcoding negative
(overfit model rejected, divergence actionable), criterion 2 (all functions of one
seed binary accepted via MCP compose + link green).
"""
import json

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from reschema.corpus.generate import build
from reschema.engine import TaskStore, compose
from reschema.mcp.server import server

PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-20, 10]},
    {"name": "hi", "kind": "i32", "range": [10, 30]},
]
CLAMP_PARAMS = [
    {"name": "v", "kind": "i32", "range": [-100, 100]},
    {"name": "lo", "kind": "i32", "range": [-50, 0]},
    {"name": "hi", "kind": "i32", "range": [0, 50]},
]
SCALE_PARAMS = [
    {"name": "buf", "kind": "buffer_i32", "direction": "in_out",
     "length_param": "n", "range": [51, 100], "ret": "void"},
    {"name": "n", "kind": "i32", "range": [3, 4]},
    {"name": "factor", "kind": "i32", "range": [2, 5]},
]

WRONG = '#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t a,int32_t b){return a+b;}'
RIGHT = '''#include <stdint.h>
static int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}'''
CLAMP = '''#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}'''
GOOD_SCALE = '''#include <stdint.h>
__attribute__((sysv_abi)) void scale_buf(int32_t *buf,int32_t n,int32_t factor){for(int32_t i=0;i<n;i++){int32_t v=buf[i]*factor;buf[i]=v<-100?-100:v>100?100:v;}}'''
# Memorizes the probed case (-5,30)→450, garbage elsewhere: the classic overfit.
OVERFIT = '#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){return (lo==-5&&hi==30)?450:0;}'

# calc slots per scenario: O2 shared with test_mcp (both wipe), O0/O1 dogfood-only
T_O2, T_O1, T_O0 = "calc::gcc-O2-sym", "calc::gcc-O1-sym", "calc::gcc-O0-sym"


def _wipe(task_id):
    # task dirs are shared runtime state across modules; start clean
    st = TaskStore(task_id)
    for p in st.dir.glob("trace_*.json"):
        p.unlink()
    st._path("ledger.json").unlink(missing_ok=True)


@pytest.fixture(scope="module", autouse=True)
def corpus():
    build()
    for t in (T_O2, T_O1, T_O0):
        _wipe(t)


async def _acall(tool, kw):
    async with InMemoryTransport(server) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool(tool, kw)
        assert not res.is_error, res.content
        sc = res.structured_content
        if sc is not None:
            return sc["result"] if set(sc) == {"result"} else sc
        return json.loads(res.content[0].text)


def call(tool, **kw):
    return anyio.run(_acall, tool, kw)


# ponytail: no N_FUZZ patching here — dogfood intentionally runs the production
# engine-default campaign (submit_model always omits n_fuzz). Cost is the price
# of being the suite's only production-shaped end-to-end coverage.



def test_dogfood_rejection_repair_cycle():
    """Criterion 1 (spec §11): level-B agent loop on -O2 — disasm open, driver-level
    probe, wrong model rejected with divergence, repaired model accepted, ledger
    counts exactly one rejection."""
    ids = call("corpus_build")
    assert T_O2 in ids
    opened = call("task_open", task_id=T_O2, function="sum_range")
    assert "disasm" in opened  # function mode: the agent starts blind from asm
    probe = call("experiment", task_id=T_O2, function="sum_range",
                 params=PARAMS, case={"lo": -5, "hi": 30})
    assert isinstance(probe["ret"], int)
    r1 = call("submit_model", task_id=T_O2, function="sum_range",
              params=PARAMS, c_source=WRONG, seed=1)
    assert not r1["accepted"] and r1["divergence"]["field"] == "ret"
    assert r1["divergence"]["input"] != {"lo": -5, "hi": 30}  # fresh draw, not the probe case
    r2 = call("submit_model", task_id=T_O2, function="sum_range",
              params=PARAMS, c_source=RIGHT, seed=1)
    assert r2["accepted"]
    st = call("status", task_id=T_O2)
    assert st["ledger"]["rejections"] == 1


def test_dogfood_overfit_rejection_is_actionable():
    """Spec §9 negative: a model hardcoding the probed outputs is rejected by the
    fuzz campaign on a FRESH draw, and the divergence carries input/field/expected/
    actual — enough for the agent to iterate to the general model."""
    probe = call("experiment", task_id=T_O0, function="sum_range",
                 params=PARAMS, case={"lo": -5, "hi": 30})
    assert probe["ret"] == 450
    r1 = call("submit_model", task_id=T_O0, function="sum_range",
              params=PARAMS, c_source=OVERFIT, seed=1)
    assert not r1["accepted"]
    div = r1["divergence"]
    assert {"input", "field", "expected", "actual"} <= set(div)
    assert div["field"] == "ret"
    assert div["input"] != {"lo": -5, "hi": 30}  # caught on an unseen draw, not the probe
    r2 = call("submit_model", task_id=T_O0, function="sum_range",
              params=PARAMS, c_source=RIGHT, seed=1)
    assert r2["accepted"]


def test_dogfood_all_functions_of_binary_compose():
    """Criterion 2 (spec §11): every function of the calc seed binary accepted via
    MCP submissions stitches into one program (compose is engine-only, spec §5)."""
    for func, params, model in [
        ("clamp_i32", CLAMP_PARAMS, CLAMP),
        ("sum_range", PARAMS, RIGHT),
        ("scale_buf", SCALE_PARAMS, GOOD_SCALE),
    ]:
        r = call("submit_model", task_id=T_O1, function=func,
                 params=params, c_source=model, seed=1)
        assert r["accepted"], (func, r)
    st = call("status", task_id=T_O1)
    accepted = {f for e in st["ledger"]["accepted"] for f in e if isinstance(e, dict)}
    assert accepted == {"clamp_i32", "sum_range", "scale_buf"}  # whole binary covered
    ok, err = compose(TaskStore(T_O1))  # harness-side stitch; not an MCP tool
    assert ok, err
