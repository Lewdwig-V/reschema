"""M8 S4: the program-replay checker (src/reschema/checks/program_replay.py).

Default-suite tests stub the job runner (no podman job; host recording only):
candidate validation, and the job's output treated as the model's claim. Real
in-job replays are `golden`-marked, here and in test_golden_decisions.py."""

import json
from pathlib import Path

import pytest
from conftest import check_context
from warranted import FEEDBACK_LIMIT, CheckContext, JobResult, VerdictStatus

from reschema.checks.program_replay import MAX_CASES, ProgramReplay, _capped
from reschema.engine import HIDDEN_N, TaskStore, _record_stable

ROT13 = "rot13::gcc-O1-sym"
GOOD = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""
HELLO = [{"argv": ["hello"], "stdin_hex": ""}]


def _check(candidate):
    # Rejected before any private file, recording, or job is touched.
    return ProgramReplay().check(CheckContext(candidate, {}, {}))


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ({"cases.json": b"[]"}, "candidate"),
        ({"model.c": b"\xff\xfe", "cases.json": b"[]"}, "candidate"),
        ({"model.c": b"", "cases.json": b"{not json"}, "cases"),
        ({"model.c": b"", "cases.json": b'{"argv": []}'}, "cases"),
        ({"model.c": b"", "cases.json": json.dumps(HELLO * (MAX_CASES + 1))}, "cases"),
        (
            {"model.c": b"", "cases.json": b'[{"argv": "hello", "stdin_hex": ""}]'},
            "cases",
        ),
        ({"model.c": b"", "cases.json": b'[{"argv": [1], "stdin_hex": ""}]'}, "cases"),
        ({"model.c": b"", "cases.json": b'[{"argv": [], "stdin_hex": "zz"}]'}, "cases"),
        (
            {
                "model.c": b"",
                "cases.json": b'[{"argv": ["a\\u0000"], "stdin_hex": ""}]',
            },
            "cases",
        ),
        (
            {"model.c": b"", "cases.json": b'[{"argv": [], "stdin_hex": "", "x": 1}]'},
            "cases",
        ),
        (
            {
                "model.c": b"",
                "cases.json": json.dumps([{"argv": ["a" * 257], "stdin_hex": ""}]),
            },
            "cases",
        ),
    ],
)
def test_malformed_candidates_are_structured_rejects(candidate, reason):
    candidate = {k: v if type(v) is bytes else v.encode() for k, v in candidate.items()}
    v = _check(candidate)
    assert v.status is VerdictStatus.REJECTED
    assert v.feedback["reason"] == reason and v.feedback["accepted"] is False


def test_oversized_divergence_is_capped_under_the_feedback_limit():
    huge = {
        "argv": ["x" * 10**6] * 100,
        "expected": {f"f{i}": "ab" * 10**5 for i in range(500)},
    }
    out = _capped({"accepted": False, "reason": "files-mismatch", "divergence": huge})
    assert len(json.dumps(out, sort_keys=True, indent=2)) < FEEDBACK_LIMIT
    assert out["reason"] == "files-mismatch"


class Forge:
    """A job runner standing in for a model that escaped qiling: it sees the
    job's files and writes whatever stdout it likes."""

    identity = "forge"

    def __init__(self, emit, rc=0, timed_out=False, truncated=False):
        self.emit, self.rc, self.timed_out, self.truncated = (
            emit,
            rc,
            timed_out,
            truncated,
        )

    def run(self, image, argv, files, **_):
        self.files = dict(files)
        out = self.emit(json.loads(files["job.json"])["cases"])
        return JobResult(self.rc, out, b"", self.timed_out, self.truncated)


def test_job_sees_inputs_never_ground_truth(built_corpus):
    jobs = Forge(lambda cases: b"")
    ProgramReplay().check(
        check_context(
            ROT13, {"model.c": GOOD, "cases.json": json.dumps(HELLO)}, jobs, seed=1
        )
    )
    assert set(jobs.files) == {
        "model.c",
        "replay.py",
        "recorder.py",
        "canonical.py",
        "job.json",
    }
    cases = json.loads(jobs.files["job.json"])["cases"]
    assert len(cases) == 1 + HIDDEN_N and cases[0] == HELLO[0]
    assert all(set(c) == {"argv", "stdin_hex"} for c in cases)
    code = Path(TaskStore(ROT13).meta["binary"]).read_bytes()[4096:8192]
    assert not any(code in data for data in jobs.files.values())


def test_forged_trace_passes_nominated_but_not_hidden(built_corpus):
    # The worker knows its nominated cases' ground truth (it recorded them),
    # never the hidden ones: echoing what it knows fails the hidden stage.
    known = _record_stable(TaskStore(ROT13).meta["binary"], ["hello"], b"")
    jobs = Forge(lambda cases: b"\n".join(json.dumps(known).encode() for _ in cases))
    v = ProgramReplay().check(
        check_context(
            ROT13, {"model.c": GOOD, "cases.json": json.dumps(HELLO)}, jobs, seed=2
        )
    )
    assert v.status is VerdictStatus.REJECTED
    assert (v.feedback["reason"], v.feedback["stage"]) == ("io-mismatch", "hidden")


_TRACE = {"stdout": "", "stderr": "", "exit_code": 0, "files_written": {}, "events": []}


@pytest.mark.parametrize(
    ("forge", "reason"),
    [
        (Forge(lambda cases: b"not json"), "replay-fault"),
        (Forge(lambda cases: json.dumps(_TRACE).encode()), "replay-fault"),  # too few
        (
            Forge(
                lambda cases: b"\n".join(
                    json.dumps({**_TRACE, "stdout": 1}).encode() for _ in cases
                )
            ),
            "replay-fault",
        ),
        (
            Forge(
                lambda cases: b"\n".join(
                    json.dumps({**_TRACE, "events": [{"sc": 1}]}).encode()
                    for _ in cases
                )
            ),
            "replay-fault",
        ),
        (Forge(lambda cases: b"\n".join(b"[]" for _ in cases)), "replay-fault"),
        # Codex #163: a scalar or a list first record crashed the compile check
        (Forge(lambda cases: b"1"), "replay-fault"),
        (Forge(lambda cases: b'["compile"]'), "replay-fault"),
        (Forge(lambda cases: b"\xff\xfe"), "replay-fault"),
        (Forge(lambda cases: b"", rc=137), "replay-fault"),
        (Forge(lambda cases: b"", truncated=True), "replay-output"),
        (Forge(lambda cases: b"", timed_out=True), "replay-timeout"),
    ],
)
def test_malformed_job_output_is_a_reject_not_a_crash(built_corpus, forge, reason):
    v = ProgramReplay().check(
        check_context(ROT13, {"model.c": GOOD, "cases.json": "[]"}, forge, seed=3)
    )
    assert v.status is VerdictStatus.REJECTED
    assert v.feedback["reason"] == reason


# --- real jobs (podman, qiling in the checker image) -------------------------

PROBE_JOB = r"""
#include <stdio.h>
int main(int argc, char **argv){
 /* any file the job holds, or the corpus binary, would change the output */
 const char *p[] = {"/work/job.json", "job.json", "../../../../work/job.json",
   "/work/replay.py", "/work/model.c", "binary", "/proc/self/cwd/job.json",
   "/proc/1/root/work/job.json"};
 for (unsigned i = 0; i < sizeof p / sizeof *p; i++)
  if (fopen(p[i], "r")) { puts(p[i]); return 99; }
 if(argc<2){puts("usage: rot13 WORD");return 2;}
 for(char*q=argv[1];*q;q++){char c=*q;
  if(c>='a'&&c<='z')*q='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*q='A'+(c-'A'+13)%26;}
 puts(argv[1]); return 0; }
"""
ECHO = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
puts(argv[1]); return 0; }
"""


@pytest.mark.golden
def test_model_cannot_reach_job_files_from_the_emulator(built_corpus):
    v = ProgramReplay().check(
        check_context(
            ROT13, {"model.c": PROBE_JOB, "cases.json": json.dumps(HELLO)}, seed=4
        )
    )
    assert v.status is VerdictStatus.PASSED, v.feedback


@pytest.mark.golden
def test_flattering_nominations_lose_at_the_hidden_stage(built_corpus):
    # Wrong spec (echo, not rot13) nominating only the case it gets right.
    usage = [{"argv": [], "stdin_hex": ""}]
    v = ProgramReplay().check(
        check_context(ROT13, {"model.c": ECHO, "cases.json": json.dumps(usage)}, seed=5)
    )
    assert v.status is VerdictStatus.REJECTED
    assert (v.feedback["reason"], v.feedback["stage"]) == ("io-mismatch", "hidden")
