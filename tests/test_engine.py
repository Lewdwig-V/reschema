import pytest

from reschema.corpus.generate import build
from reschema.engine import TaskStore
from reschema.exec.canonical import CANONICALIZER_VERSION


@pytest.fixture(scope="module")
def manifest():
    return build()


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
    for p in st.dir.glob("trace_*.json"):
        p.unlink()
    st._path("ledger.json").unlink(missing_ok=True)
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
