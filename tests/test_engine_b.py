import json
from types import SimpleNamespace

import pytest
from conftest import wipe_task

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
WRONG = "#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t a,int32_t b){return a+b;}"
RIGHT = """#include <stdint.h>
static int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}"""
# Re-accept variant: same semantics, different source text (helper order flipped).
RIGHT2 = """#include <stdint.h>
static int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v>hi?hi:v<lo?lo:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}"""
# Rule violation for the dup-symbol path: single-function helper left EXPORTED.
RIGHT_EXPORTED_HELPER = """#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}"""
# clamp_i32 as the accepted function itself (exported — matches -shared model compiles).
CLAMP = """#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}"""
CLAMP_PARAMS = [
    {"name": "v", "kind": "i32", "range": [-100, 100]},
    {"name": "lo", "kind": "i32", "range": [-50, 0]},
    {"name": "hi", "kind": "i32", "range": [0, 50]},
]


@pytest.fixture(scope="module")
def manifest(built_corpus):
    return built_corpus


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


def test_open_function_task_carries_signature_guess_and_callees(store):
    t = open_function_task(store, "sum_range")
    assert t["signature_guess"]["arity_guess"] == 2
    assert t["signature_guess"]["returns_hint"] is True
    assert "heuristic" in t["signature_guess"]["labeled"]
    assert [c["name"] for c in t["callees"]] == ["clamp_i32"]
    assert t["callees"][0]["address"].startswith("0x")

    t = open_function_task(store, "scale_buf")
    assert t["signature_guess"]["returns_hint"] is False  # void seed


def test_experiment_function_forwards_whole_trace(store):
    t = experiment_function(store, "sum_range", PARAMS, {"lo": -5, "hi": 30})
    assert t["exit_code"] == 0
    # -5..30 sums to 450 (inside the +-1000 clamp band, so no clamping)
    assert t["ret"] == 450
    assert "mem" in t and "events" in t


def test_submit_function_accept_payload_carries_fuzz_audit(store):
    r = submit_function(store, "sum_range", PARAMS, RIGHT, seed=7, n_fuzz=64)
    assert r["accepted"]
    assert r["seed"] == 7  # effective fuzz seed surfaced in the response
    assert r["compared"] > 0 and r["skipped"] >= 0
    assert (
        store.ledger()["audit"]["sum_range"]["seed"] == 7
    )  # response matches ledger audit


def test_submit_function_rejects_then_accepts(store):
    bad = submit_function(store, "sum_range", PARAMS, WRONG, seed=1)
    assert not bad["accepted"]
    assert bad["divergence"]["field"] == "ret"
    led = store.ledger()
    assert led["submissions"] == 1 and led["rejections"] == 1 and led["accepted"] == []
    good = submit_function(store, "sum_range", PARAMS, RIGHT, seed=1)
    assert good["accepted"] is True
    assert good["seed"] == 1 and good["compared"] > 0  # audit-rich acceptance
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
    assert submit_function(store, "clamp_i32", CLAMP_PARAMS, CLAMP, seed=1, n_fuzz=8)[
        "accepted"
    ]
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=8)[
        "accepted"
    ]
    ok, err = compose(store)
    assert ok, err
    assert (store.dir / "composed").exists()
    assert "int main" in (store.dir / "composed_main.compose.c").read_text()


def test_compose_duplicate_exported_symbol_structured_reject(store):
    # Same EXTERNALLY-visible helper name across two accepted sources → ld failure
    # mapped to a structured, actionable reject.
    assert submit_function(store, "clamp_i32", CLAMP_PARAMS, CLAMP, seed=1, n_fuzz=8)[
        "accepted"
    ]
    assert submit_function(
        store, "sum_range", PARAMS, RIGHT_EXPORTED_HELPER, seed=1, n_fuzz=8
    )["accepted"]
    ok, err = compose(store)
    assert not ok
    assert "duplicate symbol" in err and "clamp_i32" in err and "static" in err


def test_experiment_function_mem_hex_json_safe(manifest):
    # rot13's `rot13` is a void cstring transform: the driver's mem readback is raw
    # bytes, which must not leak into the engine result (json.dumps would TypeError).
    st = TaskStore("rot13::gcc-O2-sym")
    t = experiment_function(
        st,
        "rot13",
        [{"name": "in_out", "kind": "cstring", "ret": "void"}],
        {"in_out": b"hello"},
    )
    assert t["exit_code"] == 0
    assert t["mem"]["in_out"] == b"uryyb".hex()  # hex, mirrors stdin_hex/stdout_hex
    json.dumps(t)


def test_experiment_function_cstring_hex_input_decoded(manifest):
    # JSON boundary rule: a cstring case value crossing MCP is a hex string (mirrors
    # stdin_hex/stdout_hex); the engine decodes engine-side before the driver call.
    st = TaskStore("rot13::gcc-O2-sym")
    t = experiment_function(
        st,
        "rot13",
        [{"name": "in_out", "kind": "cstring", "ret": "void"}],
        {"in_out": "68656c6c6f"},
    )
    assert t["exit_code"] == 0
    assert t["mem"]["in_out"] == b"uryyb".hex()  # return side stays hex-encoded
    json.dumps(t)


def test_experiment_function_cstring_bad_input_is_spec_error(manifest):
    st = TaskStore("rot13::gcc-O2-sym")
    params = [{"name": "in_out", "kind": "cstring", "ret": "void"}]
    with pytest.raises(ValueError, match="hex"):
        experiment_function(st, "rot13", params, {"in_out": "zz"})
    with pytest.raises(ValueError, match="hex|bytes"):
        experiment_function(st, "rot13", params, {"in_out": 42})


def test_submit_function_reaccept_newest_source_wins(store):
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=1)["accepted"]
    assert submit_function(store, "sum_range", PARAMS, RIGHT2, seed=1)["accepted"]
    assert store.ledger()["accepted"] == [{"sum_range": RIGHT2}]


def test_submit_function_audit_records_seed_and_budget(store):
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=8)[
        "accepted"
    ]
    assert store.ledger()["audit"]["sum_range"] == {"seed": 1, "n_fuzz": 8}


def test_function_accept_reports_task_incomplete_with_note(store):
    # ISSUE-103: ~46% of floor agent-exits were FALSE COMPLETIONS — a function
    # accept read as task completion, ledger holding {func: src}, no program
    # marker. Every accept payload must now say whether the TASK is done, and
    # incomplete ones must say what completes it. Truth-only, constant.
    from reschema.engine import TASK_INCOMPLETE_NOTE

    r = submit_function(store, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=8)
    assert r["accepted"]
    assert r["task_complete"] is False
    assert r["note"] == TASK_INCOMPLETE_NOTE
    assert "program" in r["note"]  # names the completion criterion


def test_function_accept_after_program_accept_reports_task_complete(manifest):
    # Dynamic read, not a static label: program acceptance already landed for
    # rot13, so a later FUNCTION accept must report the task as complete.
    st = TaskStore("rot13::gcc-O2-sym")
    wipe_task(st)
    GOOD_ROT13_PROG = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""
    st.record_case("a", ["hello"], b"")
    from reschema.engine import submit_program

    assert submit_program(st, GOOD_ROT13_PROG)["accepted"] is True
    good_char = r"""#include <stdint.h>
__attribute__((sysv_abi)) int32_t rot13_char(int32_t c){
    if(c>=97&&c<=122) return 97+(c-97+13)%26;
    if(c>=65&&c<=90) return 65+(c-65+13)%26;
    return c;
}"""
    r = submit_function(
        st,
        "rot13_char",
        [{"name": "c", "kind": "i32", "range": [0, 255]}],
        good_char,
        seed=1,
        n_fuzz=8,
    )
    assert r["accepted"]
    assert r["task_complete"] is True
    assert "note" not in r  # nothing left to complete, nothing to explain


def test_submit_function_audit_newest_wins_and_entropy_seed(store, monkeypatch):
    assert submit_function(store, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=8)[
        "accepted"
    ]
    monkeypatch.setattr(
        "reschema.validate.function.secrets.token_hex", lambda n: "ent" * 8
    )
    assert submit_function(store, "sum_range", PARAMS, RIGHT2, seed=None, n_fuzz=8)[
        "accepted"
    ]
    # re-accept replaces the seed too; fresh entropy is recorded, never None
    assert store.ledger()["audit"]["sum_range"] == {"seed": "ent" * 8, "n_fuzz": 8}
    ok, err = compose(store)  # extra ledger keys don't disturb stitching
    assert ok, err


def test_submit_function_void_scalar_only_counts_rejection(store):
    # ret:"void" with zero memory-channel params is a no-op pass vector: spec-stage
    # reject from the validator must still hit the ledger like any other rejection.
    params = [dict(PARAMS[0], ret="void"), PARAMS[1]]
    r = submit_function(store, "sum_range", params, RIGHT, seed=1, n_fuzz=8)
    assert r["accepted"] is False
    assert r["divergence"]["stage"] == "spec"
    led = store.ledger()
    assert led["submissions"] == 1 and led["rejections"] == 1 and led["accepted"] == []


def test_function_rejects_persist_raw_sources_but_decl_failures_never(store):
    # #111 prerequisite: code-verdict function rejects persist the raw source;
    # DECLARATION failures (spec stage — the source was never judged) must not
    # contaminate the self-play supply with unjudged entries.
    r = submit_function(store, "sum_range", PARAMS, WRONG, seed=1, n_fuzz=8)
    assert r["accepted"] is False
    rs = store.ledger()["rejected_sources"]
    assert rs == [
        {
            "mode": "function",
            "function": "sum_range",
            "stage": "divergence",
            "c_source": WRONG,
            "params": [
                {
                    "name": "lo",
                    "kind": "i32",
                    "direction": "in",
                    "length_param": None,
                    "range": [-20, 10],
                    "ret": "i32",
                },
                {
                    "name": "hi",
                    "kind": "i32",
                    "direction": "in",
                    "length_param": None,
                    "range": [10, 30],
                    "ret": "i32",
                },
            ],
        }
    ]
    # spec-stage rejects (no code verdict) do NOT persist a body
    r2 = submit_function(store, "sum_range", [], WRONG, seed=1, n_fuzz=8)
    assert r2["accepted"] is False and r2["divergence"]["stage"] == "spec"
    assert len(store.ledger()["rejected_sources"]) == 1


def test_function_rejected_sources_params_persist_on_duplicate_stage(store):
    # params must ride the entry whatever the stage — the fodder pipeline
    # re-verifies from {task_id, function, params, c_source} alone.
    submit_function(store, "sum_range", PARAMS, WRONG, seed=1, n_fuzz=8)
    submit_function(store, "sum_range", PARAMS, WRONG, seed=1, n_fuzz=8)
    r = submit_function(store, "sum_range", PARAMS, WRONG, seed=1, n_fuzz=8)
    assert r["accepted"] is False and r.get("reason") == "duplicate"
    rs = store.ledger()["rejected_sources"]
    assert rs[-1]["stage"] == "duplicate" and rs[-1]["params"][0]["name"] == "lo"


def test_submit_function_empty_params_never_accepted_never_poisoned(
    store, monkeypatch, tmp_path
):
    # ISSUE-100, the live smoke attack replayed: a live agent passed a BROKEN
    # source with params:[] (64 identical zero-arg calls — a one-behavior-point
    # coin flip) and the gate accepted + wrote a verified_fact. The gate must
    # refuse at the spec floor, and the family cache must stay clean.
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    smoke_garbage = (
        "#include <stdint.h>\n"
        "__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){"
        "if(lo>hi) return lo%21; // Wait, I'll just use the right modulus.\n"
        "// ...\n"
        "return 0;}"
    )
    r = submit_function(store, "sum_range", [], smoke_garbage, seed=1, n_fuzz=8)
    assert r["accepted"] is False
    assert r["divergence"]["stage"] == "spec"
    led = store.ledger()
    assert led["submissions"] == 1 and led["rejections"] == 1 and led["accepted"] == []
    from reschema.memory import read_family

    assert read_family("calc", fn="sum_range", root=tmp_path) == []  # no poisoning


def test_spec_stage_rejects_never_fingerprint_the_source(store):
    # Codex P2 on #101: a spec-stage verdict (no input variation, bad decls)
    # judges the DECLARATION, not the C source. Two such rejects must not
    # accumulate flail fingerprints — the params-fixed resubmit of the same
    # source reaches the gate and wins on merit.
    for _ in range(2):
        r = submit_function(store, "sum_range", [], RIGHT, seed=1, n_fuzz=2)
        assert r["accepted"] is False and r["divergence"]["stage"] == "spec"
    assert not store.ledger().get("rejected_norm")  # nothing judged, nothing stored
    r = submit_function(store, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=2)
    assert r["accepted"] is True


def test_submit_function_link_failure_counts_rejection(store):
    # -shared link tolerates the undefined extern; the engine must account the
    # structured link-stage reject like any other failed validation.
    src = (
        "#include <stdint.h>\nextern int32_t missing_dep(int32_t);\n"
        "__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){return missing_dep(lo)+hi;}"
    )
    r = submit_function(store, "sum_range", PARAMS, src, seed=1, n_fuzz=8)
    assert r["accepted"] is False
    assert r["divergence"]["stage"] == "link"
    led = store.ledger()
    assert led["submissions"] == 1 and led["rejections"] == 1 and led["accepted"] == []


def test_compose_tolerates_ledger_without_audit_key(store):
    # Backward compat: a pre-audit ledger (accepted + counters only) still composes.
    led = store.ledger()
    led["accepted"].append({"sum_range": RIGHT})
    store.save_ledger(led)
    ok, err = compose(store)
    assert ok, err


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
    r = submit_function(
        store, "sum_range", [{"name": "lo", "kind": "buffer_u8"}], RIGHT, seed=1
    )
    assert r["accepted"] is False and r["reason"] == "spec"
    assert "buffer_u8" in r["detail"]
    r2 = submit_function(
        store, "sum_range", [{"kind": "i32"}], RIGHT, seed=1
    )  # missing 'name'
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


@pytest.fixture(scope="module", autouse=True)
def _corpus_once(built_corpus):
    pass


def _status_store():
    st = TaskStore("calc::gcc-O1-sym")  # own slot: engine_b's shared store is O2
    st._path("ledger.json").unlink(missing_ok=True)
    return st


def test_status_snapshot_readiness_coverage_and_journal():
    from reschema.engine import HIDDEN_N, status_snapshot

    st = _status_store()
    st.record_case("a", [], b"")
    s = status_snapshot(st)
    assert s["recorded_cases"] == 1
    assert s["readiness"] == {"minimum": HIDDEN_N, "ready": False}
    cov = s["coverage"]
    assert cov["total_functions"] == 3
    assert cov["accepted_functions"] == []
    assert cov["program_accepted"] is False
    assert s["recent"] == []

    assert submit_function(st, "sum_range", PARAMS, RIGHT, seed=3, n_fuzz=8)["accepted"]
    s = status_snapshot(st)
    assert s["coverage"]["accepted_functions"] == ["sum_range"]
    assert s["recent"] == [
        {"mode": "function", "outcome": "accept", "function": "sum_range"}
    ]

    r = submit_function(st, "clamp_i32", CLAMP_PARAMS, "int main( {", seed=4, n_fuzz=8)
    assert not r["accepted"]
    s = status_snapshot(st)
    assert s["recent"][-1] == {
        "mode": "function",
        "outcome": "reject",
        "function": "clamp_i32",
        "stage": "compile",
    }
    assert len(s["recent"]) == 2


def test_status_snapshot_program_path_events(manifest):
    from reschema.engine import status_snapshot, submit_program

    st = _status_store()
    st.record_case("a", [], b"")
    submit_program(st, "int main( {")
    s = status_snapshot(st)
    assert s["recent"][-1] == {
        "mode": "program",
        "outcome": "reject",
        "stage": "compile",
    }


def test_status_snapshot_efficiency_metric():
    import math

    from reschema.engine import status_snapshot

    st = _status_store()
    wipe_task(st)
    st.record_case("a", [], b"")
    eff = status_snapshot(st)["efficiency"]
    assert eff["n_exp"] == 1 and eff["n_sub"] == 0
    assert eff["E"] == 0.0  # indicator false until an acceptance exists

    assert submit_function(st, "sum_range", PARAMS, RIGHT, seed=3, n_fuzz=8)["accepted"]
    eff = status_snapshot(st)["efficiency"]
    assert eff["E"] == 1.0  # accepted on the first submission with one probe

    led = st.ledger()
    led["probes"], led["submissions"] = 7, 3
    st.save_ledger(led)
    eff = status_snapshot(st)["efficiency"]
    assert eff["E"] == pytest.approx(math.exp(-(0.15 * 6 + 0.4 * 2)))

    # legacy accepted ledger with submissions == 0: E must cap at the
    # baseline-one-submission state, never exceed 1
    led["probes"], led["submissions"] = 1, 0
    st.save_ledger(led)
    assert status_snapshot(st)["efficiency"]["E"] == 1.0
    led["probes"] = 11
    st.save_ledger(led)
    assert status_snapshot(st)["efficiency"]["E"] == pytest.approx(
        math.exp(-(0.15 * 10))
    )
    assert eff == {
        "E": eff["E"],
        "n_exp": 7,
        "n_sub": 3,
        "alpha": 0.15,
        "beta": 0.4,
    }


def test_task_open_repair_directive_only_after_rejection():
    st = _status_store()
    st._path("ledger.json").unlink(missing_ok=True)
    t = open_function_task(st, "sum_range")
    assert "repair_directive" not in t  # nothing to repair yet

    submit_function(st, "sum_range", PARAMS, WRONG, seed=1, n_fuzz=8)
    t = open_function_task(st, "sum_range")  # re-open: directive appears
    d = t["repair_directive"]
    assert "divergence" in d["trigger"]
    assert d["order"][0].startswith("1)") and "bit-logic" in d["order"][0]
    assert d["order"][1].startswith("2)") and "idiomatic" in d["order"][1].lower()
    assert "guidance" in d["provenance"]

    # directive is additive coaching: every existing payload key still present
    assert (
        t["signature_guess"]
        and t["callees"] is not None
        and t["abi_template"]
        and "memory" in t
    )

    # after acceptance the history (and its coaching) persist — no reversion
    submit_function(st, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=8)
    assert (
        open_function_task(st, "sum_range")["repair_directive"]["order"] == d["order"]
    )
