"""ISSUE-95: the near-duplicate resubmission flail-guard.

Smoke evidence (gemma4:26b): small agents resubmit near-identical broken
sources repeatedly — comment churn, one edited line, syntactically incomplete
— burning the β=0.40 E-decay plus a compile+replay round per loop. The guard
fingerprints GATE-rejected sources (comment/whitespace-stripped, so
reformatting is no evasion) and refuses a shape only after it has looped.

Two bands (codex P2 on #99): text alone cannot tell verbatim churn from the
legitimate minimal repair, so EXACT normalized duplicates are refused from
the 3rd submission of the shape, while small EDITED variants get one extra
repair attempt's runway — refused from the 4th. A one-char fix discovered
after two flailed attempts always reaches the gate.
"""

import pytest
from conftest import wipe_task

from reschema.engine import (
    TaskStore,
    _char_diff,
    _norm_source,
    status_snapshot,
    submit_function,
    submit_program,
)


@pytest.fixture(scope="module", autouse=True)
def _corpus_once(built_corpus):
    pass


def _store(task_id):
    st = TaskStore(task_id)
    wipe_task(st)
    return st


GOOD_ROT13 = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""

WRONG_ROT13 = r"""
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv){
    if(argc<2){puts("usage: rot13 WORD");return 2;}
    char buf[256]; strcpy(buf, argv[1]);
    for(size_t i=0;i<strlen(buf);i++){
        char c=buf[i];
        if(c>='a'&&c<='z') buf[i]='a'+(c-'a'+12)%26;
        else if(c>='A'&&c<='Z') buf[i]='A'+(c-'A'+12)%26;
    }
    printf("%s\n", buf);
    return 0;
}
"""

# Same broken code, whitespace/comment churn only: normalized-identical —
# the exact "verbatim-ish loop" shape the guard exists to kill.
WRONG_ROT13_CHURNED = r"""/* second attempt
   — same shape, self-therapy */
#include <stdio.h>
#include   <string.h>
int main(int argc, char **argv){
    if (argc < 2) { puts("usage: rot13 WORD"); return 2; }
    char buf[256];
    strcpy(buf, argv[1]);         // copy arg
    for (size_t i = 0; i < strlen(buf); i++) {
        char c = buf[i];
        if (c >= 'a' && c <= 'z') buf[i] = 'a' + (c - 'a' + 12) % 26; // rotate
        else if (c >= 'A' && c <= 'Z') buf[i] = 'A' + (c - 'A' + 12) % 26;
    }
    printf("%s\n", buf);
    return 0;
}
"""

# One normalized char off WRONG_ROT13 (rotate 11): a minimal REPAIR attempt —
# the class that must always reach the gate even after a same-shape reject.
WRONG_ROT13_REPAIR = WRONG_ROT13.replace("+12", "+11")


SUM_PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-20, 10]},
    {"name": "hi", "kind": "i32", "range": [10, 30]},
]
WRONG_SUM = "#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t a,int32_t b){return a+b;}"
WRONG_SUM_CHURNED = (
    "/* same wrong idea, restated */\n#include <stdint.h>\n"
    "__attribute__((sysv_abi))   int32_t   sum_range(int32_t a, int32_t b) { return a + b; } // sum\n"
)
RIGHT_SUM = """#include <stdint.h>
static int32_t clamp_it(int32_t v, int32_t lo, int32_t hi){
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo, int32_t hi){
    int32_t s=0; for(int32_t i=lo;i<=hi;i++) s=clamp_it(s+i,-1000,1000); return s;
}"""


def _assert_gate_reject(r):
    assert r["accepted"] is False
    assert r.get("reason") != "duplicate"  # reached the gate, got a real verdict


# --- normalization primitives -----------------------------------------------


def test_norm_source_strips_comments_and_whitespace_keeps_literals():
    a = _norm_source('int f() { // a comment\n return printf("a//b /*c*/"); }')
    b = _norm_source('int f(){/*c*/return printf("a//b /*c*/");}')
    assert a == b
    assert '"a//b /*c*/"' in b  # literal content intact (// and /* inside)
    # char literals and escapes survive; line comments die at unterminated EOF
    assert _norm_source("char c='/'; /* unterminated") == "charc='/';"


def test_char_diff_sensitive_and_symmetric():
    assert _char_diff("abc", "abc") == 0
    assert _char_diff("abc", "abd") == 1
    assert _char_diff("abc", "abcX") == 1
    assert _char_diff("abc", "xyz") == _char_diff("xyz", "abc") > 2


# --- program mode ------------------------------------------------------------


def test_first_same_shape_repair_attempt_reaches_the_gate():
    st = _store("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    r1 = submit_program(st, WRONG_ROT13)
    _assert_gate_reject(r1)
    assert r1["reason"] == "io-mismatch"  # real gate verdict, recorded stage
    # one prior fingerprint only: the churned twin must STILL be judged
    r2 = submit_program(st, WRONG_ROT13_CHURNED)
    _assert_gate_reject(r2)
    assert r2["reason"] == "io-mismatch"
    led = st.ledger()
    assert len(led["rejected_norm"]) == 2  # both gate rejects fingerprinted


def test_loop_refused_from_third_same_shape_submission():
    st = _store("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    submit_program(st, WRONG_ROT13)
    submit_program(st, WRONG_ROT13_CHURNED)
    r3 = submit_program(st, WRONG_ROT13)  # verbatim loop, third same-shape
    assert r3["accepted"] is False
    assert r3["reason"] == "duplicate"
    assert (
        "near-duplicate" in r3["detail"] and "change approach or stop" in r3["detail"]
    )
    led = st.ledger()
    # guard rejections count as submissions (E still prices the flail)
    assert led["submissions"] == 3 and led["rejections"] == 3
    assert led["recent"][-1]["stage"] == "duplicate"


def test_one_char_repair_after_reject_is_not_blocked():
    st = _store("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    submit_program(st, WRONG_ROT13)
    r = submit_program(st, WRONG_ROT13_REPAIR)  # 1 normalized-char fix
    _assert_gate_reject(r)  # judged by the gate, not the guard
    assert r["reason"] == "io-mismatch"


def test_minimal_fix_after_two_flails_reaches_gate_and_accepts():
    # Codex P2's exact scenario: two rejected +12 attempts, then the correct
    # minimal +13 edit of the SAME shape. The near-band runway must let the
    # fix through to the verifier — and the verifier accepts it.
    st = _store("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    submit_program(st, WRONG_ROT13)
    submit_program(st, WRONG_ROT13_CHURNED)
    minimal_fix = WRONG_ROT13.replace("+12", "+13")
    r = submit_program(st, minimal_fix)
    assert r["accepted"] is True  # reached the gate and was judged correct


def test_edited_variant_loop_dies_at_fourth_attempt():
    st = _store("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    submit_program(st, WRONG_ROT13)  # +12
    submit_program(st, WRONG_ROT13_CHURNED)  # +12, exact twin
    r3 = submit_program(st, WRONG_ROT13_REPAIR)  # +11: still runway
    _assert_gate_reject(r3)  # ...judged by the gate (2 fingerprints + 1 near)
    r4 = submit_program(st, WRONG_ROT13.replace("+12", "+10"))  # 3 prior shapes
    assert r4["reason"] == "duplicate"  # near-band refusal now fires


def test_genuinely_different_approach_reaches_the_gate_after_two_rejects():
    st = _store("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    submit_program(st, WRONG_ROT13)
    submit_program(st, WRONG_ROT13_CHURNED)
    r = submit_program(st, GOOD_ROT13)  # structurally different, correct
    assert r["accepted"] is True


def test_accepted_source_near_dups_never_blocked():
    st = _store("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    assert submit_program(st, GOOD_ROT13)["accepted"] is True
    churned = "/* reformat */\n" + GOOD_ROT13.replace("argv[1]", "argv[1] ")
    r = submit_program(st, churned)  # near-dup of an ACCEPTED source
    assert r["accepted"] is True  # idempotent re-accept, guard stays out


def test_unjudged_program_outcomes_never_fingerprint(monkeypatch):
    # codex P2 on #116: infra compile failures are transient env failures, not
    # flail — identical resubmissions after infra outages must not be refused
    # as duplicate loops (the function path already excludes infra verdicts).
    from reschema import engine as eng

    st = _store("rot13::gcc-O2-sym")
    monkeypatch.setattr(
        eng, "compile_model", lambda src, out: (False, "compile infra: no podman")
    )
    for _ in range(3):  # three IDENTICAL submissions, all infra-failed
        r = eng.submit_program(st, GOOD_ROT13)
        assert r["reason"] == "compile"
    led = st.ledger()
    assert not led.get("rejected_norm") and not led.get("rejected_sources")

    monkeypatch.undo()
    st.record_case("a", ["abc"], b"")
    r = eng.submit_program(st, GOOD_ROT13)
    assert r["accepted"] is True  # the same source sails through once judged


def test_fingerprints_are_per_task():
    a = _store("rot13::gcc-O2-sym")
    a.record_case("a", ["abc"], b"")
    submit_program(a, WRONG_ROT13)
    submit_program(a, WRONG_ROT13_CHURNED)  # two fingerprints on THIS task
    b = _store("rot13::gcc-O1-sym")  # sibling slot, own ledger
    b.record_case("a", ["abc"], b"")
    r = submit_program(b, WRONG_ROT13)
    _assert_gate_reject(r)  # cross-task residue does not trigger the guard


def test_status_e_accounts_guard_rejections():
    st = _store("rot13::gcc-O2-sym")
    st.record_case("a", ["abc"], b"")
    submit_program(st, WRONG_ROT13)
    submit_program(st, WRONG_ROT13_CHURNED)
    submit_program(st, WRONG_ROT13)
    eff = status_snapshot(st)["efficiency"]
    assert eff["n_sub"] == 3  # submissions==, rejections== implied by counters


# --- function mode -----------------------------------------------------------


def test_function_loop_refused_from_third_same_shape_submission():
    st = _store("calc::gcc-O2-sym")
    r1 = submit_function(st, "sum_range", SUM_PARAMS, WRONG_SUM, seed=1, n_fuzz=8)
    _assert_gate_reject(r1)
    r2 = submit_function(
        st, "sum_range", SUM_PARAMS, WRONG_SUM_CHURNED, seed=1, n_fuzz=8
    )
    _assert_gate_reject(r2)  # churn is not an evasion
    r3 = submit_function(st, "sum_range", SUM_PARAMS, WRONG_SUM, seed=1, n_fuzz=8)
    assert r3["reason"] == "duplicate"
    r4 = submit_function(st, "sum_range", SUM_PARAMS, RIGHT_SUM, seed=1, n_fuzz=8)
    assert r4["accepted"] is True  # the real fix sails through
    # spec rejects are NOT fingerprints: a params-fixed resubmit must not be
    # blocked by an earlier spec reject of the same source
    st2 = _store("calc::gcc-O1-sym")
    bad = submit_function(
        st2, "sum_range", [{"name": "x"}], RIGHT_SUM, seed=1, n_fuzz=8
    )
    assert bad["reason"] == "spec"
    assert not st2.ledger().get("rejected_norm")
