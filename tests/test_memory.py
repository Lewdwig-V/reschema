import json

import pytest

from reschema.memory import append_fact, read_family


def _fact(**kw):
    return {
        "tier": "verified_fact",
        "fn": "sum_range",
        "task_id": "calc::gcc-O2-sym",
        **kw,
    }


def test_memory_jsonl_roundtrip(tmp_path):
    append_fact("calc", _fact(), root=tmp_path)
    append_fact("calc", _fact(task_id="calc::gcc-O1-sym"), root=tmp_path)
    append_fact("rot13", _fact(fn="rot13_char"), root=tmp_path)
    entries = read_family("calc", root=tmp_path)
    assert len(entries) == 2
    assert read_family("calc", fn="rot13_char", root=tmp_path) == []
    assert len(read_family("rot13", fn="rot13_char", root=tmp_path)) == 1
    # JSONL file on disk, one entry per line, atomic-temp discipline
    lines = (tmp_path / "calc.jsonl").read_text().strip().splitlines()
    assert [json.loads(x)["task_id"] for x in lines] == [
        "calc::gcc-O2-sym",
        "calc::gcc-O1-sym",
    ]


from reschema.corpus.generate import build
from reschema.engine import TaskStore, submit_function, submit_program


@pytest.fixture(scope="module")
def manifest():
    return build()


RIGHT = r"""
#include <stdint.h>
static int32_t clamp_it(int32_t v, int32_t lo, int32_t hi){
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo, int32_t hi){
    int32_t s=0; for(int32_t i=lo;i<=hi;i++) s=clamp_it(s+i,-1000,1000); return s;
}"""

GOOD_ROT13_PROG = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""
SUM_PARAMS_JSON = [
    {"name": "lo", "kind": "i32", "range": [-20, 10]},
    {"name": "hi", "kind": "i32", "range": [10, 30]},
]


def _store(task_id):
    st = TaskStore(task_id)
    st._path("ledger.json").unlink(missing_ok=True)
    return st


def test_function_accept_writes_verified_fact(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    st = _store("calc::gcc-O2-sym")
    r = submit_function(st, "sum_range", SUM_PARAMS_JSON, RIGHT, seed=5, n_fuzz=8)
    assert r["accepted"]
    entries = read_family("calc", fn="sum_range", root=tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["tier"] == "verified_fact" and e["promoted"] is True
    assert e["fn"] == "sum_range"
    # params stored in wire form (from_json roundtrip):
    assert e["params"][0]["name"] == "lo" and e["params"][0]["range"] == [-20, 10]
    assert e["params"][1]["name"] == "hi" and e["params"][1]["range"] == [10, 30]
    assert e["c_source"] == RIGHT
    assert e["n_fuzz"] == 8 and e["audit_seed"] == 5


def test_program_accept_writes_main_fact(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    st = _store("rot13::gcc-O2-sym")
    for p in st.dir.glob("trace_*.json"):
        p.unlink()
    st.record_case("a", ["hello"], b"")
    r = submit_program(st, GOOD_ROT13_PROG)
    assert r["accepted"]
    entries = read_family("rot13", fn="__main__", root=tmp_path)
    assert len(entries) == 1
    assert entries[0]["tier"] == "verified_fact"
    assert entries[0]["promoted"] is True


def test_notes_become_unverified_and_promote_on_acceptance(
    manifest, monkeypatch, tmp_path
):
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    st = _store("calc::gcc-O2-sym")
    wrong = RIGHT.replace("clamp_it(s+i,-1000,1000)", "clamp_it(s+i,-10,10)")
    r = submit_function(
        st,
        "sum_range",
        SUM_PARAMS_JSON,
        wrong,
        notes=["the clamp band is +-10"],
        seed=1,
        n_fuzz=8,
    )
    assert not r["accepted"]
    notes = [
        e
        for e in read_family("calc", fn="sum_range", root=tmp_path)
        if e["tier"] == "unverified_hypothesis"
    ]
    assert len(notes) == 1 and notes[0]["note"] == "the clamp band is +-10"
    assert notes[0]["promoted"] is False  # rejected: the claim stands unverified

    r = submit_function(
        st,
        "sum_range",
        SUM_PARAMS_JSON,
        RIGHT,
        notes=["the clamp band is +-1000"],
        seed=2,
        n_fuzz=8,
    )
    assert r["accepted"]
    notes = [
        e
        for e in read_family("calc", fn="sum_range", root=tmp_path)
        if e["tier"] == "unverified_hypothesis"
    ]
    assert any(
        e["promoted"] is True and e["note"] == "the clamp band is +-1000" for e in notes
    )
    # the older rejected claim must NOT be retro-promoted
    assert any(
        e["promoted"] is False and e["note"] == "the clamp band is +-10" for e in notes
    )


def test_task_open_injects_family_memory(manifest, monkeypatch, tmp_path):
    from reschema.engine import open_function_task

    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    src_a = _store("calc::gcc-O2-sym")
    submit_function(
        src_a,
        "sum_range",
        SUM_PARAMS_JSON,
        RIGHT,
        notes=["clamped running sum"],
        seed=9,
        n_fuzz=8,
    )
    # a DIFFERENT slot of the same seed: same function family
    st_b = _store("calc::gcc-O1-sym")
    t = open_function_task(st_b, "sum_range")
    mem = t["memory"]
    facts = [e for e in mem if e["tier"] == "verified_fact"]
    notes = [e for e in mem if e["tier"] == "unverified_hypothesis"]
    assert len(facts) == 1 and facts[0]["c_source"] == RIGHT
    assert notes == [
        {
            "tier": "unverified_hypothesis",
            "fn": "sum_range",
            "task_id": "calc::gcc-O2-sym",
            "note": "clamped running sum",
            "promoted": True,
        }
    ]


def test_program_task_open_injects_main_memory(manifest, monkeypatch, tmp_path):
    from reschema.mcp.server import task_open

    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    st = _store("rot13::gcc-O2-sym")
    for p in st.dir.glob("trace_*.json"):
        p.unlink()
    st.record_case("a", ["hello"], b"")
    submit_program(st, GOOD_ROT13_PROG)
    # a DIFFERENT slot of the same seed: program-mode task_open carries the memory
    t = task_open("rot13::clang-O2-sym")
    facts = [e for e in t["memory"] if e["tier"] == "verified_fact"]
    assert len(facts) == 1
    assert facts[0]["fn"] == "__main__" and facts[0]["promoted"] is True
    assert facts[0]["c_source"] == GOOD_ROT13_PROG
