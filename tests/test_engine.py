import pytest
from conftest import wipe_task

from reschema.engine import TaskStore
from reschema.exec.canonical import CANONICALIZER_VERSION


@pytest.fixture(scope="module")
def manifest(built_corpus):
    return built_corpus


def test_record_case_saves_canonical_trace(manifest):
    st = TaskStore("rot13::gcc-O2-sym")
    t = st.record_case("a", ["abc"], b"")
    assert st.recorded()[0]["stdout"] == t["stdout"]
    # canonicalized: argv[0] is basename only
    assert st.recorded()[0]["argv"][0] == "prog"


def test_flakiness_detected(manifest, monkeypatch):
    st = TaskStore("rot13::gcc-O2-sym")
    runs = iter(
        [
            {"stdout": "aa", "argv": ["prog", "z"], "events": []},
            {"stdout": "bb", "argv": ["prog", "z"], "events": []},
        ]
    )
    monkeypatch.setattr("reschema.engine.record", lambda *a, **k: next(runs))
    with pytest.raises(RuntimeError, match="flaky"):
        st.record_case("x", ["z"], b"")


def test_ledger_roundtrip(manifest):
    st = TaskStore("rot13::gcc-O2-sym")
    # task dir is shared runtime state; reset so reruns see the default ledger
    st._path("ledger.json").unlink(missing_ok=True)
    led = st.ledger()
    assert led == {"accepted": [], "submissions": 0, "rejections": 0}
    led["submissions"] = 3
    st.save_ledger(led)
    assert TaskStore("rot13::gcc-O2-sym").ledger()["submissions"] == 3


def test_unknown_task_id_raises_clean_keyerror(manifest):
    with pytest.raises(KeyError, match="unknown task_id: nope::x"):
        TaskStore("nope::x")


def test_load_manifest_rejects_stale_canonicalizer_version(manifest):
    from reschema.engine import ROOT, load_manifest

    sidecar = ROOT / ".reschema" / "corpus" / "canonicalizer_version"
    assert (
        sidecar.read_text() == CANONICALIZER_VERSION
    )  # build() stamps the current rules
    sidecar.write_text("1.0")
    try:
        with pytest.raises(
            RuntimeError,
            match=f"canonicalizer 1.0.*current {CANONICALIZER_VERSION}.*re-record",
        ):
            load_manifest()
    finally:
        sidecar.write_text(CANONICALIZER_VERSION)


GOOD_ROT13_PROG = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""
BAD_ROT13_PROG = '#include <stdio.h>\nint main(void){puts("uryyb");return 0;}'


def _prog_store(manifest):
    from reschema.engine import TaskStore as TS

    st = TS("rot13::gcc-O1-sym")  # separate slot: other modules own the O2 dir
    wipe_task(st)
    st.record_case("a", ["hello"], b"")
    return st


def test_program_path_counts_submissions_and_rejections(manifest):
    from reschema.engine import submit_program

    st = _prog_store(manifest)
    r = submit_program(st, "int main( {")  # compile reject
    assert not r["accepted"]
    led = st.ledger()
    assert led["submissions"] == 1 and led["rejections"] == 1
    # program-path rejects were previously INVISIBLE to the ledger

    r = submit_program(st, GOOD_ROT13_PROG)
    assert r["accepted"]
    led = st.ledger()
    assert led["submissions"] == 2 and led["rejections"] == 1

    r = submit_program(st, BAD_ROT13_PROG)  # hidden reject
    assert not r["accepted"]
    led = st.ledger()
    assert led["submissions"] == 3 and led["rejections"] == 2


def test_program_rejects_persist_raw_sources_for_self_play_mining(manifest):
    # #111 prerequisite found by review: ledgers kept only normalized
    # fingerprints + journal stages — the self-play fodder experiment has no
    # rejected BODIES to compile. Code-verdict program rejects must persist
    # the raw source with its stage (the miner's class tag).
    from reschema.engine import submit_program

    st = _prog_store(manifest)
    compile_rej = "int main( {"
    submit_program(st, compile_rej)
    submit_program(st, BAD_ROT13_PROG)  # hidden-stage reject
    rs = st.ledger()["rejected_sources"]
    assert rs == [
        {"mode": "program", "stage": "compile", "c_source": compile_rej},
        {"mode": "program", "stage": "hidden", "c_source": BAD_ROT13_PROG},
    ]
    assert " " in rs[0]["c_source"]  # RAW source, not the normalized fingerprint


def test_rejected_sources_capped_at_16_newest(manifest):
    from reschema.engine import submit_program

    st = _prog_store(manifest)
    st.record_case("b", ["world"], b"")
    for i in range(18):
        submit_program(st, f"int main() {{ return {i}; }}")  # hidden rejects
    rs = st.ledger()["rejected_sources"]
    assert len(rs) == 16
    assert rs[0]["c_source"] == "int main() { return 2; }"  # oldest evicted
    assert rs[-1]["c_source"] == "int main() { return 17; }"


def test_unjudged_outcomes_never_enter_the_failure_supply(manifest, monkeypatch):
    # codex P2 on #116: infra compile failures (worker unavailable — the source
    # was never compiled or judged) and hidden-starvation (input-side
    # exhaustion) must NOT pollute rejected_sources nor feed the flail guard.
    from reschema import engine as eng

    st = _prog_store(manifest)
    monkeypatch.setattr(
        eng, "compile_model", lambda src, out: (False, "compile infra: no podman")
    )
    r = eng.submit_program(st, GOOD_ROT13_PROG)
    assert r["accepted"] is False and r["reason"] == "compile"
    led = st.ledger()
    assert "rejected_sources" not in led  # unjudged: no body in the supply
    assert "rejected_norm" not in led  # unjudged: no fingerprint either
    assert led["recent"][-1]["stage"] == "infra"  # ...but the journal says why

    monkeypatch.undo()
    # genuine compile failure of the same GOOD source shape is a code verdict
    # again — and the unjudged infra attempts leave no flail residue behind
    st2 = _prog_store(manifest)
    monkeypatch.setattr(
        eng,
        "hidden_input_stream",
        lambda rng, modes: iter([]),  # input draw starves: harness-side failure
    )
    st2.record_case("b", ["world"], b"")
    r2 = eng.submit_program(st2, GOOD_ROT13_PROG)
    assert r2["reason"] == "hidden-starvation"
    led2 = st2.ledger()
    assert "rejected_sources" not in led2 and "rejected_norm" not in led2


def test_program_accept_is_idempotent_and_audited(manifest):
    from reschema.engine import submit_program

    st = _prog_store(manifest)
    assert submit_program(st, GOOD_ROT13_PROG)["accepted"]
    assert submit_program(st, GOOD_ROT13_PROG)["accepted"]  # re-accept
    led = st.ledger()
    assert led["accepted"].count("program") == 1  # marker, not a stack
    hidden_seed = led["audit"]["program"]["hidden_seed"]
    assert hidden_seed.startswith("hidden:rot13::gcc-O1-sym:")
    assert len(hidden_seed.rsplit(":", 1)[1]) == 32  # token_hex(16)


def test_program_accept_payload_is_rich(manifest):
    # accept payload parity with rejects: counts + the hidden seed, no placeholders.
    from reschema.engine import HIDDEN_N, submit_program

    st = _prog_store(manifest)
    st.record_case("b", ["world"], b"")
    r = submit_program(st, GOOD_ROT13_PROG)
    assert r["accepted"]
    assert r["recorded_cases"] == 2
    assert r["hidden_cases"] == HIDDEN_N
    assert r["hidden_seed"].startswith("hidden:")
    assert r["task_complete"] is True  # #103: the one true completion signal
    assert "replay_pct" not in r


def test_probes_counted_on_program_experiment(manifest):
    st = TaskStore("rot13::gcc-O2-sym")
    wipe_task(st)
    st.record_case("a", ["abcdefghijklmnopqrstuvwxyz"], b"")
    st.record_case("b", ["zz"], b"")
    led = st.ledger()
    assert led.get("probes") == 2  # experiment call = one probe, one tally


def test_probes_counted_on_function_experiment(manifest):
    from reschema.engine import experiment_function

    st = TaskStore("calc::gcc-O2-sym")
    st._path("ledger.json").unlink(missing_ok=True)
    ps = [
        {"name": "v", "kind": "i32"},
        {"name": "lo", "kind": "i32"},
        {"name": "hi", "kind": "i32"},
    ]
    experiment_function(st, "clamp_i32", ps, {"v": 5, "lo": 0, "hi": 10})
    experiment_function(st, "clamp_i32", ps, {"v": -5, "lo": 0, "hi": 10})
    assert st.ledger().get("probes") == 2
