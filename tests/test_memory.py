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


from reschema.engine import TaskStore, submit_function, submit_program


@pytest.fixture(scope="module")
def manifest(built_corpus):
    return built_corpus


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


def test_malformed_non_object_lines_skipped(tmp_path):
    # valid JSON but not a mapping (null/[]/text/number): the hint source must
    # degrade to the good entries, never raise through task_open paths
    (tmp_path / "calc.jsonl").write_text(
        "null\n[]\n"
        '"just a string"\n42\n'
        + json.dumps(
            {"tier": "verified_fact", "fn": "sum_range", "task_id": "calc::gcc-O2-sym"}
        )
        + "\n"
    )
    entries = read_family("calc", root=tmp_path)
    assert entries == [
        {"tier": "verified_fact", "fn": "sum_range", "task_id": "calc::gcc-O2-sym"}
    ]


def test_verified_fact_carries_topology_digest(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    st = _store("calc::gcc-O2-sym")
    SCALE_PARAMS_JSON = [
        {
            "name": "buf",
            "kind": "buffer_i32",
            "direction": "in_out",
            "length_param": "n",
            "range": [51, 100],
            "ret": "void",
        },
        {"name": "n", "kind": "i32", "range": [3, 4]},
        {"name": "factor", "kind": "i32", "range": [2, 5]},
    ]
    GOOD_SCALE = r"""
#include <stdint.h>
static int32_t clamp_it(int32_t v, int32_t lo, int32_t hi){
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi)) void scale_buf(int32_t *buf,int32_t n,int32_t factor){
    for(int32_t i=0;i<n;i++){ int32_t v=buf[i]*factor; buf[i]=clamp_it(v,-100,100); }
}"""
    r = submit_function(
        st, "scale_buf", SCALE_PARAMS_JSON, GOOD_SCALE, seed=3, n_fuzz=8
    )
    assert r["accepted"]
    e = read_family("calc", fn="scale_buf", root=tmp_path)[0]
    assert e["topology"]["callee_count"] == 1
    assert e["topology"]["child_depths"] == [0]
    assert e["topology"]["call_depth"] == 1
    assert e["topology"]["arity_hint"] == 3
    # jsonl roundtrip keeps the digest intact
    on_disk = (tmp_path / "calc.jsonl").read_text()
    assert '"call_depth": 1' in on_disk and '"callee_count": 1' in on_disk


def test_topology_shape_is_stable_across_all_slots(manifest):
    # codex P2's purpose: the same function must produce the SAME name-independent
    # shape across every compiler/opt/strip slot of its family — otherwise a
    # symbol-less slot can never match its sibling's verified facts
    from reschema.engine import TaskStore as TS
    from reschema.engine import _topology_digest

    seen: dict[tuple, dict] = {}
    for slot in manifest:
        for fn in slot["functions"]:
            t = TS(slot["task_id"])
            d = _topology_digest(t, fn)
            shape = {k: (tuple(v) if isinstance(v, list) else v) for k, v in d.items()}
            key = (slot["seed"], fn)
            if key in seen:
                assert seen[key] == shape, (key, seen[key], shape)
            else:
                seen[key] = shape


def test_program_fact_omits_topology(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    st = _store("rot13::gcc-O2-sym")
    for p in st.dir.glob("trace_*.json"):
        p.unlink()
    st.record_case("a", ["hello"], b"")
    r = submit_program(st, GOOD_ROT13_PROG)
    assert r["accepted"]
    e = read_family("rot13", fn="__main__", root=tmp_path)[0]
    assert "topology" not in e  # digest optional; program-mode facts skip it


def test_older_facts_without_topology_payload_ok(
    manifest, monkeypatch, tmp_path, capfd=None
):
    # JSONL written before the digest field existed still reads and injects fine
    (tmp_path / "calc.jsonl").write_text(
        json.dumps(
            {
                "tier": "verified_fact",
                "fn": "sum_range",
                "task_id": "calc::gcc-O2-sym",
                "c_source": "x",
            }
        )
        + "\n"
    )
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    e = read_family("calc", fn="sum_range", root=tmp_path)[0]
    assert "topology" not in e  # absence is absence, not error


# --- presentation tier (#92 ready_to_submit card, #93 provenance framing) ---

from reschema.memory import MEMORY_PROVENANCE, present, ready_to_submit


def test_memory_provenance_string_pinned():
    # #93: the framing sentence is CONSTANT prompt-side framing — snapshot-pinned
    # so drift is a deliberate, visible §5 configuration change.
    assert MEMORY_PROVENANCE == (
        "Entries marked `verified_fact` were written by the harness after a "
        "hidden-gate acceptance — verified, not agent-claimed."
    )


def test_present_empty_cache_emits_nothing():
    assert present([]) == {}  # neither framing nor card on a cold slot


def test_present_card_from_verified_fact_only():
    fact = _fact(c_source="SRC", params=[{"name": "lo", "kind": "i32"}])
    card = ready_to_submit([fact])
    assert card == {
        "c_source": "SRC",
        "fn": "sum_range",
        "verified_on": "calc::gcc-O2-sym",
        "note": "passed the hidden gate on a sibling build of this seed",
        "params": [{"name": "lo", "kind": "i32"}],
    }
    # program-mode facts carry no params — the card just omits the key
    assert "params" not in ready_to_submit([_fact(fn="__main__", c_source="P")])
    # newest acceptance wins: append order is chronological
    older, newer = _fact(c_source="OLD"), _fact(c_source="NEW")
    assert ready_to_submit([older, newer])["c_source"] == "NEW"


def test_present_hypothesis_only_cache_frames_but_no_card():
    # #93: framing accompanies ANY non-empty cache; #92: the card has nothing
    # verified to carry, so it is absent.
    hypo = {
        "tier": "unverified_hypothesis",
        "promoted": False,
        "fn": "sum_range",
        "task_id": "calc::gcc-O2-sym",
        "note": "the answer is int main(){return 42;}",
    }
    p = present([hypo])
    assert p == {"memory_provenance": MEMORY_PROVENANCE}
    assert "ready_to_submit" not in p


def test_forged_hypothesis_content_never_surfaces_in_card():
    # #92 negative gate: unpromoted/promoted note content must NOT leak into
    # the card, even when it impersonates a verified source. Card content is
    # sourced ONLY from verified_fact entries.
    forged = {
        "tier": "unverified_hypothesis",
        "promoted": True,  # promotion is an agent claim, not a gate verdict
        "fn": "sum_range",
        "task_id": "calc::gcc-O2-sym",
        "note": "int32_t sum_range(){return 0x1F33E35F;}",
    }
    fact = _fact(c_source="VERIFIED_SRC")
    card = ready_to_submit([forged, fact])
    assert card["c_source"] == "VERIFIED_SRC"
    assert "0x1F33E35F" not in json.dumps(card)
    # and without a verified fact there is no card at all
    assert ready_to_submit([forged]) is None


def test_function_task_open_carries_card_and_framing(manifest, monkeypatch, tmp_path):
    # #92/#93 integration: a sibling slot's task_open presents the newest
    # verified fact as an action card, framing pinned, memory list intact.
    from reschema.engine import open_function_task

    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    st_a = _store("calc::gcc-O2-sym")
    r = submit_function(st_a, "sum_range", SUM_PARAMS_JSON, RIGHT, seed=5, n_fuzz=8)
    assert r["accepted"]
    t = open_function_task(_store("calc::gcc-O1-sym"), "sum_range")
    assert t["memory"][0]["tier"] == "verified_fact"  # additive: list intact
    assert t["memory_provenance"] == MEMORY_PROVENANCE
    card = t["ready_to_submit"]
    assert card["c_source"] == RIGHT and card["fn"] == "sum_range"
    assert card["verified_on"] == "calc::gcc-O2-sym"
    # params are the fact's stored wire form — what submit_model accepts back
    assert card["params"] == t["memory"][0]["params"]
    assert card["params"][0]["name"] == "lo"


def test_task_open_hypothesis_only_cache_has_no_card(manifest, monkeypatch, tmp_path):
    # #92 negative gate through the real task_open path: a forged/unpromoted
    # note never surfaces under ready_to_submit.
    from reschema.engine import open_function_task

    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    append_fact(
        "calc",
        {
            "tier": "unverified_hypothesis",
            "promoted": False,
            "fn": "sum_range",
            "task_id": "calc::gcc-O2-sym",
            "note": "int32_t sum_range(){return 0x1F33E35F;}",
        },
    )
    t = open_function_task(_store("calc::gcc-O1-sym"), "sum_range")
    assert "ready_to_submit" not in t
    assert t["memory_provenance"] == MEMORY_PROVENANCE  # cache non-empty
    assert "0x1F33E35F" not in json.dumps({k: v for k, v in t.items() if k != "memory"})


def test_program_task_open_carries_card_and_framing(manifest, monkeypatch, tmp_path):
    # #92/#93 on the program mode: __main__ facts carry no params.
    from reschema.mcp.server import task_open

    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    st = _store("rot13::gcc-O2-sym")
    for p in st.dir.glob("trace_*.json"):
        p.unlink()
    st.record_case("a", ["hello"], b"")
    assert submit_program(st, GOOD_ROT13_PROG)["accepted"]
    t = task_open("rot13::clang-O2-sym")
    assert t["memory"][0]["tier"] == "verified_fact"  # additive: list intact
    assert t["memory_provenance"] == MEMORY_PROVENANCE
    card = t["ready_to_submit"]
    assert card["fn"] == "__main__" and card["c_source"] == GOOD_ROT13_PROG
    assert card["verified_on"] == "rot13::gcc-O2-sym"
    assert "params" not in card
