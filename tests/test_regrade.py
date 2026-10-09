"""#112 re-grade job: a known accept reproduces under the current judge; a
planted accept the current judge rejects surfaces as a diff entry, never a
silent pass; anything unreplayable is reported, never dropped."""

import pytest
from conftest import wipe_task

from reschema.engine import TaskStore, submit_function, submit_program
from reschema.memory import append_fact
from reschema.regrade import regrade

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

    led = st.ledger()
    st.save_ledger({**led, "program_source": ECHO})  # an accept the judge rejects
    (row,) = regrade(task_ids={ROT}, fresh=True)
    assert row["new_verdict"] == "reject" and row["fresh"] is True, row
    assert row["divergence"]["stage"] == "recorded", row

    del led["audit"]["program"]  # no seed: a fresh draw is not a replay
    st.save_ledger(led)
    (row,) = regrade(task_ids={ROT})
    assert (row["new_verdict"], row["reason"]) == (
        "unreplayable",
        "no audit hidden_seed",
    )
    (row,) = regrade(task_ids={ROT}, fresh=True)  # explicitly requested: runs
    assert row["new_verdict"] == "accept" and row["fresh"] is True, row

    del led["program_source"]  # pre-#118 accepts kept no body
    st.save_ledger(led)
    (row,) = regrade(task_ids={ROT})
    assert (row["new_verdict"], row["reason"]) == ("unreplayable", "no program_source")
