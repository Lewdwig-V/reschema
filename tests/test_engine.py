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
    # acceptance was a stub {accepted, replay_pct}; calibration: audit parity
    # with rejections means counts + the hidden seed in the response itself.
    from reschema.engine import HIDDEN_N, submit_program

    st = _prog_store(manifest)
    st.record_case("b", ["world"], b"")
    r = submit_program(st, GOOD_ROT13_PROG)
    assert r["accepted"]
    assert r["recorded_cases"] == 2
    assert r["hidden_cases"] == HIDDEN_N
    assert r["hidden_seed"].startswith("hidden:")


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
