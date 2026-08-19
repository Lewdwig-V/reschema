"""ISSUE-111: fodder-yield experiment tooling.

The self-play engine's supply chain question: of sources that reached the
gate and lost, how many survive compile + behavioral-stability filtering?
Tooling is offline (no engine changes); tests pin the miner, the
rejected-vs-accepted tagger, and the stability filter's verdict classes
including its Two negative gates: garbage C must NOT compile, and
nondeterministic behavior must NOT pass as stable.
"""

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
