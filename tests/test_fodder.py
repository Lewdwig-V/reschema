"""ISSUE-111: fodder-yield experiment tooling.

The self-play engine's supply chain question: of sources that reached the
gate and lost, how many survive compile + behavioral-stability filtering?
Tooling is offline (no engine changes); tests pin the miner, the
rejected-vs-accepted tagger, and the stability filter's verdict classes
including its Two negative gates: garbage C must NOT compile, and
nondeterministic behavior must NOT pass as stable.
"""

import json

from tools.fodder import (
    classify_source,
    parse_transcript,
    stability_verdict,
    tag_verdicts,
)

ESC = "\x1b[0m"

TRANSCRIPT_SAMPLE = f"""{ESC}⚙ {ESC}reschema_task_open {{"task_id":"rot13::gcc-O0-sym"}}
{ESC}⚙ {ESC}reschema_submit_model {{"c_source":"#include <stdio.h>\\nint main() {{ puts(\\"nop\\"); }}","task_id":"rot13::gcc-O0-sym"}}
The model was rejected.
{ESC}⚙ {ESC}reschema_submit_model {{"c_source":"int main() {{ return 42; }}","function":"rot13_char","task_id":"rot13::gcc-O0-sym"}}
not json on this line {{broken
{ESC}⚙ {ESC}reschema_submit_model {{"c_source":"int main( {{","task_id":"rot13::gcc-O0-sym"}}
"""


def test_parse_transcript_extracts_only_valid_submit_payloads():
    entries = parse_transcript(TRANSCRIPT_SAMPLE)
    assert len(entries) == 3  # the malformed line degrades silently
    first = entries[0]
    assert first["task_id"] == "rot13::gcc-O0-sym"
    assert first["mode"] == "program"
    assert first["c_source"] == '#include <stdio.h>\nint main() { puts("nop"); }'
    assert entries[1]["mode"] == "function" and entries[1]["function"] == "rot13_char"
    # third entry parses fine even though the C itself is broken (compile's job)
    assert entries[2]["c_source"] == "int main( {"


def test_duplicate_submissions_deduped_verbatim():
    t = TRANSCRIPT_SAMPLE + TRANSCRIPT_SAMPLE
    entries = parse_transcript(t)
    assert len(entries) == 3  # verbatim re-appeats collapse


def test_tag_verdicts_matches_ledger_accepted_by_exact_equality():
    # Exact-string equality ONLY: a comment/whitespace-churned twin of the
    # accepted source is a flail-class REJECT (fodder), not the winner — the
    # ledger holds the one exact string that passed the gate.
    entries = [
        {"task_id": "t::s", "mode": "program", "c_source": "int main() { return 0; }"},
        {
            "task_id": "t::s",
            "mode": "program",
            "c_source": "/* churn */ int   main() {return 0; }",
        },
        {"task_id": "t::s", "mode": "program", "c_source": "int main() { return 1; }"},
    ]
    accepted = {"int main() { return 0; }"}
    tag_verdicts(entries, accepted)
    assert [e["verdict"] for e in entries] == ["accepted", "rejected", "rejected"]


def test_source_classification_tags():
    assert classify_source("int main(){ return 0x7E3A99B1 == x; }") == "magic-stub"
    assert (
        classify_source("int main(){ if (x == 0xDEADBEEF) return 1; }") == "magic-stub"
    )
    assert classify_source("int main(){ char c = '\\\\0'; }") == "escape-slip"
    assert classify_source("int main(){ return 0; }") == "standard"
    assert classify_source("int main() { return 0; }") == "standard"


def test_stability_filter_verdict_classes(tmp_path):
    # deterministic: compiles and double-records identically
    stable = '#include <stdio.h>\nint main(){ puts("nop\\n"); return 0; }'
    assert stability_verdict(stable, tmp_path) == "stable"
    # garbage C must NOT survive (the compile negative gate)
    assert stability_verdict("int main( {", tmp_path) == "compile-fail"
    # host-time-driven output must NOT pass as stable (the negative gate;
    # NOTE: qiling seeds /dev/urandom deterministically — its fake-fs IS
    # stable ground truth — but host wall-clock leaks through time syscalls)
    clocks = (
        "#include <stdio.h>\n#include <sys/time.h>\n"
        "int main(){ struct timeval tv; gettimeofday(&tv, 0); "
        'printf("%ld\\n", (long)tv.tv_usec); return 0; }'
    )
    assert stability_verdict(clocks, tmp_path) == "nondeterministic"


def test_accepted_sources_read_the_immutable_ledger_record(tmp_path):
    # codex P2 on #118: model.c is rewritten by every later compile (rejected
    # attempts included) — accepted bodies come ONLY from the ledger's
    # persisted `program_source`; absence degrades to skipping nothing.
    from tools.fodder import accepted_sources_for_runs

    tasks = tmp_path / "runs" / "x" / ".reschema" / "tasks" / "t__s"
    tasks.mkdir(parents=True)
    (tasks / "ledger.json").write_text(
        json.dumps(
            {
                "accepted": ["program", {"fn1": "int fn1(){ return 1; }"}],
                "submissions": 0,
                "rejections": 0,
                "program_source": "int main(){ return 0; }",
            }
        )
    )
    (tasks / "model.c").write_text("int main(){ return 99; }")  # later loser
    accepted = accepted_sources_for_runs(tmp_path / "runs")
    assert accepted == {
        "int main(){ return 0; }",
        "int fn1(){ return 1; }",
    }


def test_legacy_ledgers_fall_back_to_model_c_by_lifecycle(tmp_path):
    # pre-store floors carry no program_source: dogfood slots die on accept,
    # so the surviving model.c coincides with the winner by lifecycle.
    from tools.fodder import accepted_sources_for_runs

    tasks = tmp_path / "runs" / "x" / ".reschema" / "tasks" / "t__s"
    tasks.mkdir(parents=True)
    (tasks / "ledger.json").write_text(
        json.dumps({"accepted": ["program"], "submissions": 0, "rejections": 0})
    )
    (tasks / "model.c").write_text("int main(){ return 7; }")
    assert accepted_sources_for_runs(tmp_path / "runs") == {"int main(){ return 7; }"}


def test_run_experiment_dedupes_candidates_globally(tmp_path, monkeypatch):
    # codex P2 on #118: identical bodies across slot logs/floor roots are ONE
    # candidate, not N observations — repetition must not move keep-rates.
    from tools import fodder

    (tmp_path / "runs" / "a" / "sandbox").mkdir(parents=True)
    (tmp_path / "runs" / "b" / "sandbox").mkdir(parents=True)
    body = "int main(){ return 0; }"
    for d in ("a", "b"):
        (tmp_path / "runs" / d / "sandbox" / "transcript-x.log").write_text(
            f'⚙ reschema_submit_model {{"c_source":"{body}","task_id":"t::s"}}\n'
        )
    monkeypatch.setattr(fodder, "accepted_sources_for_runs", lambda runs_dir: set())
    monkeypatch.setattr(fodder, "stability_verdict", lambda src, d: "stable")
    report = fodder.run_experiment([tmp_path], tmp_path / "out", verbose=False)
    assert report["submit_model_calls_found"] == 2
    assert report["dupes_collapsed"] == 1
    assert len(report["candidates"]) == 1


# --- function-mode verification (params-persistent supply) ---

import pytest

from tools.fodder import function_verdict, load_manifests

GOOD_SUM = """#include <stdint.h>
static int32_t clamp_it(int32_t v, int32_t lo, int32_t hi){
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo, int32_t hi){
    int32_t s=0; for(int32_t i=lo;i<=hi;i++) s=clamp_it(s+i,-1000,1000); return s;
}"""
BAD_SUM = "int main(){ return 0 } // not even a function Compile-wait"
WRONG_SUM = """#include <stdint.h>
__attribute__((sysv_abi)) int32_t sum_range(int32_t a,int32_t b){return a+b;}"""
SUM_PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-20, 10]},
    {"name": "hi", "kind": "i32", "range": [10, 30]},
]


@pytest.fixture(scope="module")
def manifests(built_corpus):
    from pathlib import Path

    return load_manifests([Path(".reschema/corpus")])


def test_function_verdict_classes(manifests, tmp_path):
    # behavioral: compiles + runs, loses vs the original — THE usable fodder
    v = function_verdict(
        manifests, "calc::gcc-O2-sym", "sum_range", SUM_PARAMS, WRONG_SUM, tmp_path
    )
    assert v == "behavioral"
    # compile gate again: garbage must not even reach cases
    assert (
        function_verdict(
            manifests, "calc::gcc-O2-sym", "sum_range", SUM_PARAMS, BAD_SUM, tmp_path
        )
        == "compile-fail"
    )
    # low-budget lucky pass: RIGHT at tiny fuzz — reported honestly, never merged
    # into behavioral counts
    v = function_verdict(
        manifests, "calc::gcc-O2-sym", "sum_range", SUM_PARAMS, GOOD_SUM, tmp_path
    )
    assert v == "ok-lucky"
    # malformed decls from transcripts: spec, never judged
    assert (
        function_verdict(
            manifests,
            "calc::gcc-O2-sym",
            "sum_range",
            [{"name": "x"}],
            GOOD_SUM,
            tmp_path,
        )
        == "spec-malformed"
    )
    # resolvability: unknown task/function can't be verified at all
    assert (
        function_verdict(
            manifests, "nope::x", "sum_range", SUM_PARAMS, GOOD_SUM, tmp_path
        )
        == "unresolvable"
    )
