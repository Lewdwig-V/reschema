"""#112 re-grade job: a known accept reproduces under the current judge; a
planted accept the current judge rejects surfaces as a diff entry, never a
silent pass; anything unreplayable is reported, never dropped; environment
faults fail the job loudly."""

import json

import pytest
from conftest import wipe_task

import reschema.engine as eng
import reschema.regrade as rg
from reschema.driver import podrun
from reschema.engine import (
    TASKS,
    TaskStore,
    binary_digest,
    submit_function,
    submit_program,
)
from reschema.exec.canonical import CANONICALIZER_VERSION
from reschema.memory import append_fact
from reschema.regrade import _src_hash, regrade
from reschema.validate.function import FnVerdict

SUM = "calc::gcc-O2-sym"
ROT = "rot13::gcc-O2-sym"
GONE = "gone::gcc-O2-sym"
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


@pytest.fixture
def gone_dir(built_corpus):
    """A task dir for a slot the manifest does not have."""
    d = TASKS / GONE.replace("::", "__")
    d.mkdir(parents=True, exist_ok=True)
    yield d
    for p in d.iterdir():
        p.unlink()
    d.rmdir()


def _plant(st, led):
    st.save_ledger({"submissions": 1, "rejections": 0, **led})


def _program_audit(st, **over):
    """A program audit shaped exactly like submit_program writes it."""
    return {
        "hidden_seed": "hidden:x:y",
        "recorded": [],
        "binary": binary_digest(st.meta["binary"]),
        "canonicalizer": CANONICALIZER_VERSION,
        **over,
    }


def test_known_function_accept_reproduces(calc):
    r = submit_function(calc, "sum_range", PARAMS, RIGHT, seed=1, n_fuzz=8)
    assert r["accepted"], r
    audit = calc.ledger()["audit"]["sum_range"]
    assert audit["binary"] == binary_digest(calc.meta["binary"]), audit
    assert audit["toolchain"] == podrun.image_id(), audit
    (row,) = regrade(task_ids={SUM})
    assert row["unit"] == "sum_range" and row["new_verdict"] == "accept", row
    assert row["binary_verified"] is True, row
    assert row["toolchain_verified"] is True, row
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
    assert row["binary_verified"] is False, row  # legacy audit: no digest
    assert row["toolchain_verified"] is False, row  # ...and no image ID


def test_pre_143_audit_falls_back_to_memory_params(calc):
    # Audit entries before #143 hold only {seed, n_fuzz}: params come from the
    # verified_fact with the SAME task, source and audit seed.
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
    audit = st.ledger()["audit"]["program"]
    assert set(audit) == {
        "hidden_seed",
        "recorded",
        "binary",
        "canonicalizer",
        "toolchain",
    }
    assert audit["toolchain"] == podrun.image_id(), audit
    assert audit["recorded"] == sorted(audit["recorded"]), audit
    assert [len(e) for e in audit["recorded"]] == [3, 3, 3], audit

    # An experiment AFTER the accept is new evidence, not a judge change: even
    # a trace the model would fail must not be replayed (accept-time snapshot).
    t = st.record_case("late", ["late"], b"")
    late = st._path("trace_late.json")
    late.write_text(json.dumps({**t, "stdout": b"WRONG\n".hex()}))
    (row,) = regrade(task_ids={ROT})
    assert row["new_verdict"] == "accept", row
    assert row["seed"] == r["hidden_seed"] and row["fresh"] is False, row
    assert row["binary_verified"] is True, row
    assert row["toolchain_verified"] is True, row
    late.unlink()

    led = st.ledger()
    led["program_source"] = ECHO  # an accept the current judge rejects
    st.save_ledger(led)
    (row,) = regrade(task_ids={ROT})  # recorded-stage reject draws no seed...
    assert row["divergence"]["stage"] == "recorded", row
    assert row["seed"] == r["hidden_seed"], row  # ...the audit seed is kept

    del led["audit"]["program"]["hidden_seed"]  # a fresh draw is not a replay
    st.save_ledger(led)
    (row,) = regrade(task_ids={ROT})
    assert (row["new_verdict"], row["reason"]) == (
        "unreplayable",
        "no audit hidden_seed",
    )
    # explicitly requested: it runs (ECHO's recorded-stage reject proves the
    # replay happened without paying for a hidden suite)
    (row,) = regrade(task_ids={ROT}, fresh=True)
    assert row["new_verdict"] == "reject" and row["fresh"] is True, row
    assert row["divergence"]["stage"] == "recorded", row

    # an interrupted (non-atomic) trace write: a row, not a batch abort
    e01 = st._path("trace_e01.json")
    good = e01.read_text()
    e01.write_text(good[: len(good) // 2])
    (row,) = regrade(task_ids={ROT}, fresh=True)
    assert row["new_verdict"] == "unreplayable", row
    assert row["reason"].startswith("bad stored data: JSONDecodeError"), row
    assert row["source_hash"] == _src_hash(ECHO) and row["fresh"] is True, row
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


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"binary": "0" * 16}, "binary changed"),
        ({"toolchain": "0" * 64}, "toolchain changed"),
        ({"canonicalizer": "0.0"}, "canonicalizer changed"),
    ],
)
def test_program_pins_changed_are_unreplayable(calc, over, reason):
    # A corpus rebuild or a canonicalizer bump changes the evidence, not the
    # judge: never an accept->reject flip.
    _plant(
        calc,
        {
            "accepted": ["program"],
            "program_source": GOOD_ROT13,
            "audit": {"program": _program_audit(calc, **over)},
        },
    )
    (row,) = regrade(task_ids={SUM})
    assert (row["new_verdict"], row["reason"]) == ("unreplayable", reason)
    assert row["source_hash"] == _src_hash(GOOD_ROT13), row


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"binary": "0" * 16}, "binary changed"),
        # #146: the image tag is mutable; a rebuilt gcc/clang may compile the
        # accepted source differently (even to a compile-stage reject)
        ({"toolchain": "0" * 64}, "toolchain changed"),
    ],
)
def test_function_pins_changed_are_unreplayable(calc, over, reason):
    # Same seed/params/source against a CHANGED original or toolchain is not
    # a judge comparison: a rebuild must not read as an accept->reject flip.
    _plant(
        calc,
        {
            "accepted": [{"sum_range": RIGHT}],
            "audit": {"sum_range": {"seed": 1, "n_fuzz": 8, "params": PARAMS, **over}},
        },
    )
    (row,) = regrade(task_ids={SUM})
    assert (row["new_verdict"], row["reason"]) == ("unreplayable", reason)
    assert row["source_hash"] == _src_hash(RIGHT), row


CLAMP = """#include <stdint.h>
__attribute__((sysv_abi)) int32_t clamp_i32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:v>hi?hi:v;}"""


def test_reaccept_is_newest_for_last_k(calc, monkeypatch):
    # f1, f2, then a revised f1: --k 1 must re-grade the revised f1 source.
    # Ledger ordering is the subject, so the judge is stubbed (no compiles);
    # a real accept/replay is pinned by test_known_function_accept_reproduces.
    ok = lambda *a, **k: FnVerdict(True, compared=8, seed=1)
    monkeypatch.setattr(eng, "validate_function", ok)
    monkeypatch.setattr(rg, "validate_function", ok)
    revised = RIGHT + "\n/* revised */\n"
    for func, src in [
        ("sum_range", RIGHT),
        ("clamp_i32", CLAMP),
        ("sum_range", revised),
    ]:
        assert submit_function(calc, func, PARAMS, src, seed=1, n_fuzz=8)["accepted"]
    (row,) = regrade(k=1, task_ids={SUM})
    assert row["unit"] == "sum_range" and row["new_verdict"] == "accept", row
    assert row["source_hash"] == _src_hash(revised), row


def test_corrupt_memory_is_named_not_no_params(calc, monkeypatch, tmp_path):
    # #147: a pre-#143 accept whose matching verified_fact line is corrupt
    # must name the unreadable store, not claim the params never existed.
    _plant(
        calc,
        {
            "accepted": [{"scale_buf": STUB}],
            "audit": {"scale_buf": {"seed": 4242, "n_fuzz": 64}},
        },
    )
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    append_fact("calc", {"tier": "verified_fact", "fn": "other", "task_id": SUM})
    fam = tmp_path / "calc.jsonl"
    fam.write_text(fam.read_text() + '{"tier": "verified_fact", "fn": "scale_bu\n')
    (row,) = regrade(task_ids={SUM})
    assert (row["new_verdict"], row["reason"]) == (
        "unreplayable",
        "bad stored data: corrupt memory (1 lines)",
    ), row
    assert row["source_hash"] == _src_hash(STUB), row


def test_infra_failures_are_not_flips(calc, monkeypatch):
    # An environment outage (missing image, worker death) or an unjudged draw
    # must never read as a judge regression: unreplayable, not accept->reject.
    _plant(
        calc,
        {
            "accepted": [{"sum_range": RIGHT}, "program"],
            "program_source": "int main(void){return 0;}",
            "audit": {
                "sum_range": {"seed": 1, "n_fuzz": 8, "params": PARAMS},
                "program": _program_audit(calc),  # passes prepare: reaches the gate
            },
        },
    )
    monkeypatch.setattr(
        rg,
        "validate_function",
        lambda *a, **k: FnVerdict(False, {"stage": "infra", "detail": "no image"}),
    )
    for fail, stage in (
        (
            {"reason": "compile", "stage": "infra", "detail": "compile infra: x"},
            "infra",
        ),
        ({"reason": "hidden-starvation", "detail": "3/8"}, "hidden-starvation"),
    ):
        monkeypatch.setattr(rg, "program_gate", lambda *a, f=fail, **k: (f, "s"))
        rows = {r["unit"]: r for r in regrade(task_ids={SUM})}
        assert (rows["program"]["new_verdict"], rows["program"]["reason"]) == (
            "unreplayable",
            stage,
        ), rows
        assert (rows["sum_range"]["new_verdict"], rows["sum_range"]["reason"]) == (
            "unreplayable",
            "infra",
        ), rows


def test_bad_params_and_corrupt_ledger_are_unreplayable(calc):
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


@pytest.mark.parametrize(
    "audit, err",
    [
        ({"seed": [1], "n_fuzz": 8}, "TypeError"),  # would raise inside the judge
        ({"seed": 1, "n_fuzz": "8"}, "TypeError"),
        ({"seed": 1, "n_fuzz": True}, "TypeError"),
        ({"seed": 1}, "KeyError"),  # budget unknown: the draw is unreproducible
    ],
)
def test_malformed_judge_inputs_are_rows_not_aborts(calc, audit, err):
    # Values the judge consumes are type-checked in PREPARE: the judge runs
    # outside the net, so a malformed ledger must not abort the batch there.
    _plant(
        calc,
        {
            "accepted": [{"sum_range": RIGHT}],
            "audit": {"sum_range": {**audit, "params": PARAMS}},
        },
    )
    (row,) = regrade(task_ids={SUM})
    assert row["new_verdict"] == "unreplayable", row
    assert row["reason"].startswith(f"bad stored data: {err}"), row
    assert row["source_hash"] == _src_hash(RIGHT), row


def test_judge_errors_still_raise(calc, monkeypatch):
    # Only stored-data loading degrades to a row: an exception inside the
    # judge is an engine bug and must surface, never read as "unreplayable".
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


def test_removed_task_row_keeps_identity(gone_dir):
    # A readable ledger for a slot the manifest no longer has: unreplayable,
    # and the row still names the accepted revision.
    (gone_dir / "ledger.json").write_text(
        json.dumps(
            {
                "accepted": [{"f": RIGHT}],
                "audit": {"f": {"seed": 5, "n_fuzz": 8, "params": PARAMS}},
            }
        )
    )
    (row,) = regrade(task_ids={GONE})
    assert (row["new_verdict"], row["reason"]) == ("unreplayable", "unknown task")
    assert (row["source_hash"], row["seed"]) == (_src_hash(RIGHT), 5), row


@pytest.mark.parametrize(
    "write",
    [
        # #148: no `accepted` key at all is corruption, not "0 accepts"
        lambda p: p.write_text("{}"),
        lambda p: p.write_text(json.dumps({"submissions": 3, "audit": {}})),
        # wrong-typed collection: reversed() accepts a dict, keys are ignored
        lambda p: p.write_text(json.dumps({"accepted": {"sum_range": "x"}})),
        # unknown entry shapes: a scalar, an empty dict (vacuous all()), a
        # multi-key dict, a non-str source
        lambda p: p.write_text(json.dumps({"accepted": [42]})),
        lambda p: p.write_text(json.dumps({"accepted": [{}]})),
        lambda p: p.write_text(json.dumps({"accepted": [{"a": "x", "b": "y"}]})),
        lambda p: p.write_text(json.dumps({"accepted": [{"a": 5}]})),
        # stat fails (broken symlink): must not abort the whole job
        lambda p: p.symlink_to(p.parent / "missing.json"),
    ],
)
def test_malformed_ledgers_are_bad_ledger_rows(calc, write):
    p = calc._path("ledger.json")
    p.unlink(missing_ok=True)
    write(p)
    try:
        (row,) = regrade(task_ids={SUM})
    finally:
        p.unlink()
    assert (row["new_verdict"], row["reason"]) == ("unreplayable", "bad ledger")


def _stale_manifest():
    raise RuntimeError("corpus recorded under canonicalizer 2.0")


def _no_image():
    raise RuntimeError("toolchain image missing")


@pytest.mark.parametrize(
    "fault, exc",
    [
        # stale/corrupt manifest: one loud failure, not N "bad stored data" rows
        (
            lambda mp, tmp: mp.setattr(rg, "load_manifest", _stale_manifest),
            RuntimeError,
        ),
        # wrong RESCHEMA_HOME: must not read as {"total": 0}
        (lambda mp, tmp: mp.setattr(rg, "TASKS", tmp / "nope"), FileNotFoundError),
        # toolchain image gone: one loud failure, not N "toolchain changed"
        (
            lambda mp, tmp: mp.setattr(rg.podrun, "image_id", _no_image),
            RuntimeError,
        ),
        # corpus binary gone: an environment fault, not stored data
        (
            lambda mp, tmp: mp.setattr(
                rg,
                "load_manifest",
                lambda: [
                    {**t, "binary": str(tmp / "missing")} if t["task_id"] == SUM else t
                    for t in eng.load_manifest()
                ],
            ),
            FileNotFoundError,
        ),
    ],
)
def test_environment_faults_fail_loudly(calc, monkeypatch, tmp_path, fault, exc):
    _plant(
        calc,
        {
            "accepted": [{"sum_range": RIGHT}],
            "audit": {"sum_range": {"seed": 1, "n_fuzz": 8, "params": PARAMS}},
        },
    )
    fault(monkeypatch, tmp_path)
    with pytest.raises(exc):
        regrade(task_ids={SUM})


def test_main_emits_rows_and_totals(calc, gone_dir, capsys):
    _plant(calc, {"accepted": [{"sum_range": RIGHT}], "audit": {}})
    (gone_dir / "ledger.json").write_text(json.dumps({"accepted": [{"f": RIGHT}]}))
    assert rg.main(["--task", SUM, "--task", GONE]) == 0  # repeatable --task
    out, err = capsys.readouterr()
    rows = [json.loads(line) for line in out.splitlines()]
    assert {(r["task_id"], r["reason"]) for r in rows} == {
        (SUM, "no audit seed"),
        (GONE, "no audit seed"),
    }, rows
    assert all(r["canonicalizer"] == CANONICALIZER_VERSION for r in rows), rows
    assert json.loads(err) == {"total": 2, "accept->unreplayable": 2}, err
