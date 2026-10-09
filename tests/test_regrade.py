"""#112 re-grade job: a known accept reproduces under the current judge; a
planted accept the current judge rejects surfaces as a diff entry, never a
silent pass; anything unreplayable is reported, never dropped."""

import json

import pytest
from conftest import wipe_task

from reschema.engine import TaskStore, submit_function, submit_program
from reschema.memory import append_fact
from reschema.regrade import _src_hash, regrade
from reschema.validate.function import FnVerdict

SUM = "calc::gcc-O2-sym"
ROT = "rot13::gcc-O2-sym"
PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-20, 10]},
    {"name": "hi", "kind": "i32", "range": [10, 30]},
]
RIGHT = """#include <stdint.h>
static int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)s=clamp_i32(s+i,-1000,1000);return s;}"""
# The pre-#143 accept: all-i32 scale_buf + return-0 stub (accepted on all 12
# slots before the skip floor). The current judge must surface the flip.
STUB = "#include <stdint.h>\n__attribute__((sysv_abi)) int32_t scale_buf(int32_t buf,int32_t n,int32_t factor){return 0;}\n"
MISTYPED = [
    {"name": "buf", "kind": "i32"},
    {"name": "n", "kind": "i32"},
    {"name": "factor", "kind": "i32"},
]
GOOD_ROT13 = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""
ECHO = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
puts(argv[1]); return 0; }
"""


@pytest.fixture
def calc(built_corpus):
    st = TaskStore(SUM)
    wipe_task(st)
    return st


def _plant(st, led):
    st.save_ledger({"submissions": 1, "rejections": 0, **led})


def test_known_function_accept_reproduces(calc):
    r = submit_function(calc, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=8)
    assert r["accepted"], r
    (row,) = regrade(task_ids={SUM})
    assert row["unit"] == "sum_range" and row["new_verdict"] == "accept", row
    assert (row["compared"], row["skipped"], row["seed"]) == (r["compared"], 0, 1)


def test_pre_floor_accept_surfaces_as_flip(calc):
    _plant(
        calc,
        {
            "accepted": [{"scale_buf": STUB}],
            "audit": {"scale_buf": {"seed": 7, "n_fuzz": 64, "params": MISTYPED}},
        },
    )
    (row,) = regrade(task_ids={SUM})
    assert (row["old_verdict"], row["new_verdict"]) == ("accept", "reject"), row
    assert row["divergence"]["stage"] == "spec", row


def test_pre_143_audit_falls_back_to_memory_params(calc):
    # Audit entries before #143 hold only {seed, n_fuzz}: params come from the
    # verified_fact with the SAME source and audit seed.
    _plant(
        calc,
        {
            "accepted": [{"scale_buf": STUB}],
            "audit": {"scale_buf": {"seed": 4242, "n_fuzz": 64}},
        },
    )
    append_fact(
        "calc",
        {
            "tier": "verified_fact",
            "fn": "scale_buf",
            "task_id": SUM,
            "params": MISTYPED,
            "c_source": STUB,
            "n_fuzz": 64,
            "audit_seed": 4242,
        },
    )
    (row,) = regrade(task_ids={SUM})
    assert row["new_verdict"] == "reject" and row["seed"] == 4242, row


def test_unreplayable_accepts_are_reported(calc):
    _plant(
        calc,
        {
            # params nowhere (no audit params, no matching memory fact)
            "accepted": [
                {"gone_fn": STUB},
                {"clamp_i32": "/* x */"},
                {"sum_range": RIGHT},
            ],
            "audit": {
                "clamp_i32": {"seed": 99, "n_fuzz": 8},
                # removed/renamed since the accept: reported, batch continues
                "gone_fn": {"seed": 1, "n_fuzz": 8, "params": MISTYPED},
            },
        },
    )
    rows = regrade(task_ids={SUM})
    # newest first: sum_range (no audit seed), clamp_i32 (no params), gone_fn
    assert [(r["unit"], r["new_verdict"], r["reason"]) for r in rows] == [
        ("sum_range", "unreplayable", "no audit seed"),
        ("clamp_i32", "unreplayable", "no params"),
        ("gone_fn", "unreplayable", "unknown function"),
    ]
    # unreplayable rows still name the accepted revision they could not test
    assert [(r["source_hash"], r["seed"]) for r in rows] == [
        (_src_hash(RIGHT), None),
        (_src_hash("/* x */"), 99),
        (_src_hash(STUB), 1),
    ]
    assert len(regrade(k=1, task_ids={SUM})) == 1


def test_program_accept_reproduces_and_planted_flip_surfaces(built_corpus):
    st = TaskStore(ROT)
    wipe_task(st)
    for i, word in enumerate(["alpha", "Beta", "zzz"]):
        st.record_case(f"e{i:02d}", [word], b"")
    r = submit_program(st, GOOD_ROT13)
    assert r["accepted"], r
    (row,) = regrade(task_ids={ROT})
    assert row["new_verdict"] == "accept", row
    assert row["seed"] == r["hidden_seed"] and row["fresh"] is False, row

    # An experiment AFTER the accept is new evidence, not a judge change: even
    # a trace the model would fail must not be replayed (accept-time snapshot).
    t = st.record_case("late", ["late"], b"")
    late = st._path("trace_late.json")
    late.write_text(json.dumps({**t, "stdout": b"WRONG\n".hex()}))
    (row,) = regrade(task_ids={ROT})
    assert row["new_verdict"] == "accept", row
    late.unlink()

    led = st.ledger()
    st.save_ledger({**led, "program_source": ECHO})  # an accept the judge rejects
    (row,) = regrade(task_ids={ROT})  # recorded-stage reject draws no seed...
    assert row["divergence"]["stage"] == "recorded", row
    assert row["seed"] == r["hidden_seed"], row  # ...the audit seed is kept
    (row,) = regrade(task_ids={ROT}, fresh=True)
    assert row["new_verdict"] == "reject" and row["fresh"] is True, row
    assert row["divergence"]["stage"] == "recorded", row

    del led["audit"]["program"]["hidden_seed"]  # a fresh draw is not a replay
    st.save_ledger(led)
    (row,) = regrade(task_ids={ROT})
    assert (row["new_verdict"], row["reason"]) == (
        "unreplayable",
        "no audit hidden_seed",
    )
    (row,) = regrade(task_ids={ROT}, fresh=True)  # explicitly requested: runs
    assert row["new_verdict"] == "accept" and row["fresh"] is True, row

    # an interrupted (non-atomic) trace write: a row, not a batch abort
    e01 = st._path("trace_e01.json")
    good = e01.read_text()
    e01.write_text(good[: len(good) // 2])
    (row,) = regrade(task_ids={ROT}, fresh=True)
    assert row["new_verdict"] == "unreplayable", row
    assert row["reason"].startswith("bad stored data: JSONDecodeError"), row
    assert row["source_hash"] == _src_hash(GOOD_ROT13) and row["fresh"] is True, row
    # edited in place: same input identity, different expected output
    e01.write_text(json.dumps({**json.loads(good), "stdout": b"X\n".hex()}))
    (row,) = regrade(task_ids={ROT}, fresh=True)
    assert (row["new_verdict"], row["reason"]) == (
        "unreplayable",
        "recorded cases changed",
    )
    e01.write_text(good)

    st._path("trace_e00.json").unlink()  # a snapshot case vanished
    (row,) = regrade(task_ids={ROT}, fresh=True)
    assert (row["new_verdict"], row["reason"]) == (
        "unreplayable",
        "recorded cases changed",
    )
    del led["audit"]["program"]["recorded"]  # pre-#144 accepts: no snapshot
    st.save_ledger(led)
    (row,) = regrade(task_ids={ROT}, fresh=True)
    assert (row["new_verdict"], row["reason"]) == (
        "unreplayable",
        "no recorded snapshot",
    )

    del led["program_source"]  # pre-#118 accepts kept no body
    st.save_ledger(led)
    (row,) = regrade(task_ids={ROT})
    assert (row["new_verdict"], row["reason"]) == ("unreplayable", "no program_source")


CLAMP = """#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}"""
CLAMP_PARAMS = [
    {"name": "v", "kind": "i32", "range": [-100, 100]},
    {"name": "lo", "kind": "i32", "range": [-50, 0]},
    {"name": "hi", "kind": "i32", "range": [1, 50]},
]


def test_reaccept_is_newest_for_last_k(calc):
    # f1, f2, then a revised f1: --k 1 must re-grade the revised f1 source.
    revised = RIGHT + "\n/* revised */\n"
    for func, params, src in [
        ("sum_range", PARAMS, RIGHT),
        ("clamp_i32", CLAMP_PARAMS, CLAMP),
        ("sum_range", PARAMS, revised),
    ]:
        assert submit_function(calc, func, params, src, seed=1, n_fuzz=8)["accepted"]
    (row,) = regrade(k=1, task_ids={SUM})
    assert row["unit"] == "sum_range" and row["new_verdict"] == "accept", row
    assert row["source_hash"] == _src_hash(revised), row


def test_infra_failures_are_not_flips(calc, monkeypatch):
    # An environment outage (missing image, worker death) must never read as
    # a judge regression: unreplayable, not accept->reject.
    import reschema.regrade as rg

    _plant(
        calc,
        {
            "accepted": [{"sum_range": RIGHT}, "program"],
            "program_source": "int main(void){return 0;}",
            "audit": {
                "sum_range": {"seed": 1, "n_fuzz": 8, "params": PARAMS},
                "program": {"hidden_seed": "hidden:x:y"},
            },
        },
    )
    monkeypatch.setattr(
        rg,
        "validate_function",
        lambda *a, **k: FnVerdict(False, {"stage": "infra", "detail": "no image"}),
    )
    for fail in (
        {"reason": "compile", "stage": "infra", "detail": "compile infra: x"},
        {"reason": "hidden-starvation", "detail": "3/8"},
    ):
        monkeypatch.setattr(rg, "program_gate", lambda *a, f=fail, **k: (f, "s"))
        rows = regrade(task_ids={SUM})
        assert {(r["unit"], r["new_verdict"]) for r in rows} == {
            ("program", "unreplayable"),
            ("sum_range", "unreplayable"),
        }, rows


def test_bad_params_and_corrupt_ledger_are_unreplayable(calc, built_corpus):
    # Cross-version data the current schema cannot read is reported, and the
    # batch keeps going (no traceback aborts later accepts or the totals).
    _plant(
        calc,
        {
            "accepted": [{"sum_range": RIGHT}],
            "audit": {
                "sum_range": {
                    "seed": 1,
                    "n_fuzz": 8,
                    # a short range (IndexError) decoded before a renamed kind
                    "params": [
                        {"name": "lo", "kind": "i32", "range": [0]},
                        {"name": "hi", "kind": "renamed_kind"},
                    ],
                }
            },
        },
    )
    other = TaskStore("calc::gcc-O1-sym")
    wipe_task(other)
    other._path("ledger.json").write_text("{not json")
    rows = {r["task_id"]: r for r in regrade(task_ids={SUM, "calc::gcc-O1-sym"})}
    assert rows[SUM]["new_verdict"] == "unreplayable", rows
    assert rows[SUM]["reason"].startswith("bad stored data: IndexError"), rows
    bad = rows["calc::gcc-O1-sym"]
    assert (bad["old_verdict"], bad["new_verdict"], bad["reason"]) == (
        "unknown",
        "unreplayable",
        "bad ledger",
    )
    wipe_task(other)


def test_judge_errors_still_raise(calc, monkeypatch):
    # Only stored-data loading degrades to a row: an exception inside the
    # judge is an engine bug and must surface, never read as "unreplayable".
    import reschema.regrade as rg

    _plant(
        calc,
        {
            "accepted": [{"sum_range": RIGHT}],
            "audit": {"sum_range": {"seed": 1, "n_fuzz": 8, "params": PARAMS}},
        },
    )

    def boom(*a, **k):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(rg, "validate_function", boom)
    with pytest.raises(RuntimeError, match="engine bug"):
        regrade(task_ids={SUM})


def test_removed_task_row_keeps_identity(built_corpus):
    # A readable ledger for a slot the manifest no longer has: unreplayable,
    # and the row still names the accepted revision.
    from reschema.engine import TASKS

    d = TASKS / "gone__gcc-O2-sym"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ledger.json").write_text(
        json.dumps(
            {
                "accepted": [{"f": RIGHT}],
                "audit": {"f": {"seed": 5, "n_fuzz": 8, "params": PARAMS}},
            }
        )
    )
    try:
        (row,) = regrade(task_ids={"gone::gcc-O2-sym"})
    finally:
        (d / "ledger.json").unlink()
        d.rmdir()
    assert (row["new_verdict"], row["reason"]) == ("unreplayable", "unknown task")
    assert (row["source_hash"], row["seed"]) == (_src_hash(RIGHT), 5), row
