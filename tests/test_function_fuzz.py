"""M8 S3: the function-fuzz checker's own attacks. Golden parity (accepts and
the old gate's rejects) lives in tests/test_golden_decisions.py; here are the
paths only the job boundary has: a candidate that forges the worker's output,
or feeds the host a spec its fuzz draw cannot use."""

import json

import pytest
from warranted import FEEDBACK_LIMIT, CheckContext, JobResult, PodmanJobs, VerdictStatus

from reschema.checks.function_fuzz import FunctionFuzz
from reschema.domain import ReSchemaDomain
from reschema.engine import TaskStore

SUM_PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-20, 10]},
    {"name": "hi", "kind": "i32", "range": [10, 30]},
]
SUM = (
    "#include <stdint.h>\n__attribute__((sysv_abi)) int32_t sum_range(int32_t lo,"
    "int32_t hi){int32_t s=0;for(int32_t i=lo;i<=hi;i++)"
    "{s+=i;s=s<-1000?-1000:s>1000?1000:s;}return s;}"
)


class FakeJobs:
    """A job runner whose 'candidate' prints `stdout`; records what it was sent."""

    identity = "fake"

    def __init__(self, stdout: bytes):
        self.stdout, self.sent = stdout, []

    def run(self, image, argv, files, *, stdin=b"", timeout_seconds=10, limits=None):
        self.sent.append((dict(files), json.loads(stdin)))
        return JobResult(0, self.stdout, b"")


class Pinned(CheckContext):
    def draw_seed(self):
        self.seeds.append(1)
        return 1


@pytest.fixture(scope="module")
def sum_inputs(built_corpus):
    meta = TaskStore("calc::gcc-O2-sym").meta
    f = meta["functions"]["sum_range"]
    fn = {"name": "sum_range", "addr": f["addr"], "size": f["size"]}
    with open(meta["binary"], "rb") as b:
        return {"binary": b.read(), "function.json": json.dumps(fn).encode()}


def _check(inputs, jobs, source=SUM, params=SUM_PARAMS, n_fuzz=8):
    cand = {"model.c": source.encode(), "params.json": json.dumps(params).encode()}
    ctx = Pinned(cand, inputs, {}, jobs=jobs)
    return FunctionFuzz(n_fuzz=n_fuzz).check(ctx), ctx


def test_registered_in_domain():
    assert isinstance(ReSchemaDomain.checkers["function-fuzz"], FunctionFuzz)


def test_job_never_sees_ground_truth(sum_inputs):
    jobs = FakeJobs(b'{"ok": true, "results": []}')
    _check(sum_inputs, jobs)
    ((files, job),) = jobs.sent
    assert set(files) == {"native_worker.py", "spec.py"}
    assert set(job) == {"mode", "c_source", "fname", "params", "cases"}
    assert all(set(c) == {"lo", "hi"} for c in job["cases"])  # inputs only


@pytest.mark.parametrize(
    "forged",
    [
        {"ok": True, "results": []},  # vacuous: zero cases compared
        {"ok": True, "results": [{"ret": 0, "mem": {}}]},  # short, no crash
        {"ok": True, "results": [{"ret": "0", "mem": {}}] * 8},
        {"ok": True, "results": [{"ret": 0, "mem": {"x": []}}] * 8},
        {"ok": True, "results": [{"crash": {}}, {"ret": 0, "mem": {}}]},
        [],
        "not an object",
    ],
)
def test_forged_worker_output_is_rejected(sum_inputs, forged):
    v, _ = _check(sum_inputs, FakeJobs(json.dumps(forged).encode()))
    assert v.status is VerdictStatus.REJECTED
    assert v.feedback["divergence"]["stage"] == "worker"


def test_forged_infra_stage_is_a_reject_not_an_infra_failure(sum_inputs):
    # Infra failures do not count against the candidate; the job cannot claim one.
    huge = {"stage": "infra", "detail": "x" * 200_000, "stderr": "y" * 200_000}
    v, _ = _check(sum_inputs, FakeJobs(json.dumps(huge).encode()))
    assert v.status is VerdictStatus.REJECTED
    assert v.feedback["divergence"]["stage"] == "infra"
    assert len(json.dumps(v.feedback)) < FEEDBACK_LIMIT // 4


def test_forged_crash_text_is_capped(sum_inputs):
    forged = {"results": [{"crash": {"error": "z" * 200_000}}]}
    v, _ = _check(sum_inputs, FakeJobs(json.dumps(forged).encode()))
    assert v.feedback["divergence"]["field"] == "crash"
    assert len(json.dumps(v.feedback)) < 1024


@pytest.mark.parametrize(
    "params",
    [
        "not json",
        {"lo": "i32"},
        [{"name": "lo", "kind": "i32", "range": ["a", "b"]}],
        [{"name": "lo", "kind": "i32", "range": [5, 1]}],
        [{"name": "lo", "kind": "i32", "range": [1]}],
        [{"name": "b", "kind": "buffer_i32", "length_param": "nope"}],
        [{"name": "lo", "kind": "i32", "ret": "float"}],
        [{"kind": "i32"}],
        [{"name": "lo", "kind": "f64"}],
        # Codex #162: values the i32 marshal cannot pack, and a name collision
        # that overwrites a scalar draw with a list, both crashed the host.
        [{"name": "b", "kind": "buffer_i32", "range": [2**31, 2**31 + 1]}],
        [{"name": "lo", "kind": "i32", "range": [-(2**31) - 1, 0]}],
        [{"name": "x", "kind": "i32"}, {"name": "x", "kind": "buffer_i32"}],
        # Codex #162: past the cap, honest output overflows the job's stdout cap
        [
            {"name": "n", "kind": "i32", "range": [0, 33]},
            {"name": "b", "kind": "buffer_i32", "length_param": "n"},
        ],
        # Codex #162 P1: a huge length range allocated 2**31 cells on the host
        [
            {"name": "n", "kind": "i32", "range": [2**31 - 1, 2**31 - 1]},
            {"name": "b", "kind": "buffer_i32", "length_param": "n"},
        ],
    ],
)
def test_malformed_spec_is_a_spec_reject(sum_inputs, params):
    jobs = FakeJobs(b"")
    cand = {
        "model.c": SUM.encode(),
        "params.json": params.encode()
        if isinstance(params, str)
        else json.dumps(params).encode(),
    }
    v = FunctionFuzz(n_fuzz=8).check(Pinned(cand, sum_inputs, {}, jobs=jobs))
    assert (v.status, v.feedback["reason"]) == (VerdictStatus.REJECTED, "spec")
    assert jobs.sent == []


def test_missing_candidate_file_is_a_spec_reject(sum_inputs):
    ctx = Pinned({"model.c": SUM.encode()}, sum_inputs, {}, jobs=FakeJobs(b""))
    v = FunctionFuzz(n_fuzz=8).check(ctx)
    assert v.feedback == {
        "accepted": False,
        "reason": "spec",
        "detail": "missing ['params.json']",
    }


def test_accept_in_a_real_job_records_fact_and_seed(sum_inputs):
    v, ctx = _check(sum_inputs, PodmanJobs())
    assert v.status is VerdictStatus.PASSED, v.feedback
    assert v.feedback["compared"] > 0
    assert v.facts == (
        {"function": "sum_range", "params": v.facts[0]["params"], "source": SUM},
    )
    assert ctx.seeds == [1] and len(ctx.job_log) == 1


def test_constructor_forging_stdout_in_a_real_job_is_rejected(sum_inputs):
    # The .so's constructor runs inside the worker process (symbol check) and
    # prints a vacuous pass before the worker can: the forged claim is refused.
    forge = (
        "#include <stdio.h>\n#include <unistd.h>\n"
        "__attribute__((constructor)) static void forge(void){"
        'fputs("{\\"ok\\": true, \\"results\\": []}", stdout);'
        "fflush(stdout);_exit(0);}\n" + SUM.replace("s+=i", "s+=1")
    )
    v, _ = _check(sum_inputs, PodmanJobs(), source=forge)
    assert v.status is VerdictStatus.REJECTED
    assert v.feedback["divergence"]["stage"] == "worker"
