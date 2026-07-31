"""MCP server tests: in-process via mcp 2.0 InMemoryTransport (no network).

mcp 2.0 has no mcp.shared.memory/create_connected_server_and_client_session from the
plan (that was the 1.x API) — InMemoryTransport + ClientSession is the in-process path.
"""
import json

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from reschema.corpus.generate import build
from reschema.engine import TaskStore
from reschema.mcp.server import server

PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-50, 0]},
    {"name": "hi", "kind": "i32", "range": [0, 50]},
]
WRONG = '#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t a,int32_t b){return a+b;}'
RIGHT = '''#include <stdint.h>
static int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}'''
GOOD_ROT13 = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""


def _wipe(task_id):
    # task dirs are shared runtime state across modules; start clean
    st = TaskStore(task_id)
    for p in st.dir.glob("trace_*.json"):
        p.unlink()
    st._path("ledger.json").unlink(missing_ok=True)


@pytest.fixture(scope="module", autouse=True)
def corpus():
    build()
    _wipe("rot13::gcc-O2-sym")
    _wipe("calc::gcc-O2-sym")


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


def test_tool_listing():
    async def go():
        async with InMemoryTransport(server) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            return {t.name for t in (await s.list_tools()).tools}

    names = anyio.run(go)
    assert names == {"corpus_build", "task_open", "experiment", "submit_model", "status"}
    # compose is deliberately not exposed (spec §5)


def test_task_open_and_program_submit_flow():
    meta = call("task_open", task_id="rot13::gcc-O2-sym")
    assert meta["functions"]["rot13"]
    tr = call("experiment", task_id="rot13::gcc-O2-sym", argv=["abc"])
    assert bytes.fromhex(tr["stdout"]).decode() == "nop\n"
    r = call("submit_model", task_id="rot13::gcc-O2-sym", c_source=GOOD_ROT13)
    assert r["accepted"]
    st = call("status", task_id="rot13::gcc-O2-sym")
    assert st["recorded_cases"] >= 1 and st["ledger"]["accepted"]


def test_function_experiment_and_submit_flow():
    opened = call("task_open", task_id="calc::gcc-O2-sym", function="sum_range")
    assert "disasm" in opened
    probe = call("experiment", task_id="calc::gcc-O2-sym", function="sum_range",
                 params=PARAMS, case={"lo": -5, "hi": 30})
    assert isinstance(probe["ret"], int)
    # low fuzz counts keep the qiling bill small; seed pinned for determinism
    r1 = call("submit_model", task_id="calc::gcc-O2-sym", function="sum_range",
              params=PARAMS, c_source=WRONG, seed=1, n_fuzz=8)
    assert not r1["accepted"]
    r2 = call("submit_model", task_id="calc::gcc-O2-sym", function="sum_range",
              params=PARAMS, c_source=RIGHT, seed=1, n_fuzz=8)
    assert r2["accepted"]
    st = call("status", task_id="calc::gcc-O2-sym")
    assert st["ledger"]["rejections"] == 1


def test_unknown_task_returns_structured_error():
    r = call("task_open", task_id="nope::x")
    assert "unknown task" in r["detail"] and "error" in r
    r2 = call("task_open", task_id="calc::gcc-O2-sym", function="nope")
    assert "unknown function" in r2["detail"] and "error" in r2
