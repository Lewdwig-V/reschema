import pytest

from reschema.corpus.generate import build
from reschema.engine import TaskStore


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
    assert sidecar.read_text() == "2.0"  # build() stamps the current rules
    sidecar.write_text("1.0")
    try:
        with pytest.raises(
            RuntimeError, match="canonicalizer 1.0.*current 2.0.*re-record"
        ):
            load_manifest()
    finally:
        sidecar.write_text("2.0")
