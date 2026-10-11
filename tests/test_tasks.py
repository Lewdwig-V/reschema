"""M8 S5: task specs per slot (D2), the `experiment` operation, family memory and
the duplicate guard, driven through Warranted's Project with a scripted worker.

Mystery-style attacks (Warranted's examples/m7/mystery tests): private files
never reach the workspace, a worker-written verdict or fact is never accepted,
budgets hold, and the guard refuses repeats before any check. Only the
end-to-end accept needs podman jobs and is `golden`-marked; the rest reject
before any job, or stub the checker."""

import json
from pathlib import Path
from types import MappingProxyType

import pytest
from warranted import (
    Applicability,
    OperationContext,
    Outcome,
    PodmanJobs,
    Project,
    RunConfig,
    RunOutcome,
    Verdict,
    VerdictStatus,
)
from warranted._ledger import Ledger, Result
from warranted._worker import AttemptResult

from reschema import operations, tasks
from reschema.domain import ReSchemaDomain
from reschema.engine import TaskStore
from reschema.exec import canonical
from reschema.operations import Experiment, ExperimentFunction

SLOT = "rot13::gcc-O2-sym"
SUBMIT_MARK = b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
CONFIG = RunConfig("scripted-model-v1", max_steps=4)
GOLDEN = {
    c["id"]: c
    for k in ("function", "program")
    for c in json.loads(
        (Path(__file__).parent / "golden" / "m8_decisions.json").read_text()
    )[k]
}


class Model:
    model = "scripted-model-v1"

    def __call__(self, request, payload):
        return AttemptResult(
            Result(Outcome.SUCCEEDED, 0, {"model": 1}, 1),
            {"response": json.dumps({"command": "work"}).encode()},
        )


def request(*arguments) -> bytes:
    items = [{"operation": "experiment", "arguments": a} for a in arguments]
    return b"WARRANTED_REQUEST\n" + json.dumps({"requests": items}).encode()


def submit(**files) -> tuple[bytes, dict]:
    # Warranted captures a candidate only with a result.json beside it.
    files = {"result.json": "{}", **files}
    return SUBMIT_MARK, {f"candidate/{k}": v.encode() for k, v in files.items()}


def bad_program(src: str) -> tuple[bytes, dict]:
    """A program candidate the checker rejects before any recording or job."""
    return submit(**{"model.c": src, "cases.json": "{not json"})


class Steps:
    """Episode i of a run runs plan[i][n] as its nth command: stdout bytes, or
    (stdout, candidate files). Reassign `plan` between runs."""

    def __init__(self, plan):
        self.plan, self.episodes = plan, []

    def __call__(self, ledger_root, episode):
        self.episodes.append(episode)
        steps = self.plan[int(episode.episode_id.rsplit("-s", 1)[1]) - 1]

        class Environment:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def __call__(self, req, data):
                step = steps[int(req.origin.operation_id.rsplit("/", 1)[1]) - 1]
                stdout, extra = (step, {}) if type(step) is bytes else step
                return AttemptResult(
                    Result(Outcome.SUCCEEDED, 0, {"tool": 1}, 1),
                    {"stdout": stdout, "stderr": b"", **extra},
                )

            def deliver(self, path, files):
                pass

        return Environment()


class NoJobs:
    identity = "no-jobs"

    def run(self, *args, **kwargs):
        raise AssertionError("no job may run")


def project(tmp_path, plan, domain=None, jobs=None):
    steps = Steps(plan)
    proj = Project.create(
        tmp_path / "project",
        domain or ReSchemaDomain(),
        {"model": 100, "tool": 100, "check": 20, "probe": 20},
        environment=steps,
        environment_id="scripted-environment-v1",
        jobs=jobs or PodmanJobs(),
    )
    return proj, steps


def ops(proj, kind):
    """(operation, its observation's artifacts) for every completed `kind`."""
    with Ledger.open(proj.ledger_root) as ledger:
        return [
            (
                o,
                {
                    n: ledger.read_artifact(r)
                    for n, r in o.completion.observation.artifacts.items()
                },
            )
            for o in ledger.operations()
            if o.request.origin.kind == kind and o.completion
        ]


def shown(proj):
    return [json.loads(a["shown.json"]) for _, a in ops(proj, "operation")]


def episode_files(proj, episode) -> dict[str, bytes]:
    with Ledger.open(proj.ledger_root) as ledger:
        return {n: ledger.read_artifact(e.artifact) for n, e in episode.files.items()}


@pytest.fixture(scope="module")
def meta(built_corpus):
    return TaskStore(SLOT).meta


@pytest.fixture(scope="module")
def slot(meta):
    return tasks.slot_tasks(meta)


def one(meta, n=1):
    return tasks.program_task(meta, submissions=n, check_budget=n)


# --- builders -----------------------------------------------------------------


def test_one_task_per_function_then_the_program(meta, slot):
    assert [t.id for t in slot] == [
        f"{SLOT}::fn::rot13_char",
        f"{SLOT}::fn::rot13",
        f"{SLOT}::program",
    ]
    assert [t.checks for t in slot] == [("function-fuzz",)] * 2 + [("program-replay",)]
    for t in slot:
        assert (t.memory.scope, t.memory.depends) == ("rot13", ("family.json",))
        assert t.duplicate_guard == tasks.GUARD
    assert json.loads(slot[0].private["task.json"]) == {
        "seed": "rot13",
        "function": {"name": "rot13_char", **meta["functions"]["rot13_char"]},
    }
    assert json.loads(slot[2].private["task.json"]) == {"seed": "rot13"}
    assert set(slot[0].inputs) == {
        "family.json",
        "function.json",
        "disasm.txt",
        "abi_template.c",
    }
    assert set(slot[2].inputs) == {"family.json", "program.json"}
    assert json.loads(slot[2].inputs["program.json"])["input"] == "argv"
    spec = tasks.campaign("c1", [meta], {"cfg": CONFIG})
    assert [e.task.id for e in spec.tasks] == [t.id for t in slot]


def test_private_files_never_reach_inputs(meta, slot):
    binary = Path(meta["binary"]).read_bytes()
    for t in slot:
        assert set(t.private) == {"binary", "task.json"}
        for name, data in t.inputs.items():
            assert binary not in data and binary.hex().encode() not in data, name


def test_family_is_a_digest_shared_by_every_slot_of_the_seed(meta, built_corpus):
    fam = json.loads(tasks.family("rot13"))
    assert set(fam) == {"seed", "source_sha256", "functions", "canonicalizer"}
    assert "argv" not in json.dumps(fam)  # no source text
    other = next(m for m in built_corpus if m["seed"] == "rot13" and m is not meta)
    assert tasks.program_task(other).inputs["family.json"] == tasks.family("rot13")


# --- family staleness ---------------------------------------------------------


class Pass:
    """Stands in for both checkers: passes with one fact, runs nothing."""

    version = "stub"
    isolated = True

    def check(self, ctx):
        return Verdict(VerdictStatus.PASSED, {"accepted": True}, facts=({"f": 1},))


class StubDomain(ReSchemaDomain):
    checkers = MappingProxyType({"function-fuzz": Pass(), "program-replay": Pass()})


def _memory(proj, task):
    return [(e.applicability, e.shown) for e in proj.memory(task)]


def test_family_change_marks_facts_stale(tmp_path, meta, monkeypatch):
    first = tasks.function_task(meta, "rot13_char", submissions=1, check_budget=1)
    plan = [[submit(**{"model.c": "x"})]]
    proj, _ = project(tmp_path, plan, StubDomain(), NoJobs())
    assert proj.start(first, CONFIG, Model()).outcome is RunOutcome.ACCEPTED
    assert _memory(proj, tasks.program_task(meta)) == [(Applicability.CURRENT, True)]

    monkeypatch.setattr(canonical, "CANONICALIZER_VERSION", "9.9")
    assert _memory(proj, tasks.program_task(meta)) == [(Applicability.STALE, False)]
    monkeypatch.undo()

    seeds = tmp_path / "seeds"
    seeds.mkdir()
    edited = (tasks.SEEDS / "rot13.c").read_text() + "\n/* edited */\n"
    (seeds / "rot13.c").write_text(edited)
    monkeypatch.setattr(tasks, "SEEDS", seeds)
    assert _memory(proj, tasks.program_task(meta)) == [(Applicability.STALE, False)]


# --- experiment ---------------------------------------------------------------


def test_experiment_records_a_canonical_trace_and_charges_probes(tmp_path, meta):
    probe = request({"argv": ["hello"]}, {"argv": ["hi"], "quiet": True})
    proj, _ = project(tmp_path, [[probe, bad_program("a")]], jobs=NoJobs())
    run = proj.start(one(meta), CONFIG, Model())
    full, quiet = shown(proj)
    assert bytes.fromhex(full["stdout"]) == b"uryyb\n"
    assert full["argv"] == ["prog", "hello"] and full["events"]
    assert "events" not in quiet and bytes.fromhex(quiet["stdout"]) == b"uv\n"
    assert proj.status(run.run_id).accounting["probe"].spent == 2


@pytest.mark.parametrize(
    "arguments",
    [
        {"argv": "hello"},
        {"argv": [1]},
        {"argv": ["a\0b"]},
        {"argv": ["x"] * 17},
        {"argv": ["x" * 257]},
        {"stdin": "x" * 4097},
        {"stdin": 3},
        {"quiet": "yes"},
        {"file": "binary"},  # no argument names a task file
        [],
    ],
)
def test_malformed_experiment_is_an_error_not_a_probe(tmp_path, meta, arguments):
    plan = [[request(arguments), bad_program("a")]]
    proj, _ = project(tmp_path, plan, jobs=NoJobs())
    run = proj.start(one(meta), CONFIG, Model())
    assert run.outcome is RunOutcome.REJECTED  # a bad request is not a host fault
    assert ops(proj, "operation") == []
    assert proj.status(run.run_id).accounting["probe"].spent == 0


def test_oversized_trace_is_capped_and_delivered_whole(monkeypatch):
    big = {
        "argv": ["prog"],
        "stdin_hex": "",
        "stdout": "00" * 100_000,
        "stderr": "",
        "exit_code": 0,
        "files_written": {},
        "events": [{"phase": "enter", "sc": "write", "args": ["1"]}] * 5000,
    }
    monkeypatch.setattr(operations, "_record_stable", lambda *a: big)
    args = Experiment().parse({"argv": []})
    r = Experiment().execute(OperationContext({"binary": b""}), args)
    assert r.outcome is Outcome.SUCCEEDED and dict(r.usage) == {"probe": 1}
    assert len(json.dumps(r.shown)) < operations.SHOWN_LIMIT
    assert r.shown["events_omitted"] == 5000
    assert json.loads(r.files["trace.json"]) == big

    monkeypatch.setattr(operations, "_record_stable", lambda *a: None)
    r = Experiment().execute(OperationContext({"binary": b""}), args)
    assert r.outcome is Outcome.FAILED and "flaky" in r.shown["error"]


CHAR = [{"name": "c", "kind": "i32", "range": [0, 255]}]
STR = [{"name": "s", "kind": "cstring", "ret": "void"}]


def fn_request(*arguments) -> bytes:
    items = [{"operation": "experiment_function", "arguments": a} for a in arguments]
    return b"WARRANTED_REQUEST\n" + json.dumps({"requests": items}).encode()


def bad_function() -> tuple[bytes, dict]:
    """A function candidate the checker rejects at the spec stage, no job."""
    return submit(**{"model.c": "int x;", "params.json": "not json"})


def test_experiment_function_calls_the_task_function(tmp_path, meta):
    probe = fn_request(
        {"params": CHAR, "case": {"c": ord("h")}},
        {"params": STR, "case": {"s": b"hello\0".hex()}},
    )
    proj, _ = project(tmp_path, [[probe, bad_function()]], jobs=NoJobs())
    task = tasks.function_task(meta, "rot13_char", submissions=1, check_budget=1)
    run = proj.start(task, CONFIG, Model())
    char, string = shown(proj)
    assert (char["ret"], char["mem"]) == (ord("u"), {})
    # the target is the task's function (rot13_char) whatever the worker's
    # params say: a byte in, never the string rot13
    assert "s" in string["mem"] and string["mem"]["s"] != b"uryyb\0".hex()
    for _, a in ops(proj, "operation"):
        assert len(a["shown.json"]) < operations.SHOWN_LIMIT
    assert proj.status(run.run_id).accounting["probe"].spent == 2


def test_experiment_function_on_a_program_task_fails_uncharged(tmp_path, meta):
    probe = fn_request({"params": CHAR, "case": {"c": 1}})
    proj, _ = project(tmp_path, [[probe, bad_program("a")]], jobs=NoJobs())
    run = proj.start(one(meta), CONFIG, Model())
    [(op, a)] = ops(proj, "operation")
    assert op.completion.result.outcome is Outcome.FAILED
    assert "function task" in json.loads(a["shown.json"])["error"]
    # listed for every task (operations are per domain), but runs nothing here
    assert proj.status(run.run_id).accounting["probe"].spent == 0


def test_experiment_function_target_is_not_the_workers(tmp_path, meta):
    probe = fn_request({"params": CHAR, "case": {"c": 1}, "function": "rot13"})
    proj, _ = project(tmp_path, [[probe, bad_function()]], jobs=NoJobs())
    task = tasks.function_task(meta, "rot13_char", submissions=1, check_budget=1)
    run = proj.start(task, CONFIG, Model())
    assert ops(proj, "operation") == []
    assert proj.status(run.run_id).accounting["probe"].spent == 0


BUF = [
    {"name": "n", "kind": "i32", "range": [0, 8]},
    {"name": "b", "kind": "buffer_i32", "length_param": "n"},
]


@pytest.mark.parametrize(
    ("params", "case"),
    [
        (BUF, {"n": 1, "b": 33}),  # out-buffer length over MAX_BUFFER_LEN
        (BUF, {"n": 1, "b": -1}),
        (BUF, {"n": 1, "b": [0] * 33}),
        (BUF, {"n": 1, "b": [2**31]}),
        (STR, {"s": ("61" * 256) + "00"}),  # 257 bytes
        (STR, {"s": "6162"}),  # no NUL
        (STR, {"s": "zz"}),
        (CHAR, {"c": 2**31}),
        (CHAR, {"c": True}),
        (CHAR, {"c": 1, "d": 2}),  # extra key
        (CHAR, {}),  # missing key
        ([{"name": f"a{i}", "kind": "i32"} for i in range(7)], {}),
        ([{"name": "c", "kind": "f64"}], {"c": 1}),
        ([{"name": "c", "kind": "i32", "range": [1]}], {"c": 1}),
        ("not a list", {}),
    ],
)
def test_experiment_function_refuses_bad_input_in_parse(params, case):
    with pytest.raises(ValueError):
        ExperimentFunction().parse({"params": params, "case": case})


def test_experiment_function_flaky_original_is_failed(monkeypatch, meta, slot):
    calls = iter([{"ret": 1, "mem": {}}, {"ret": 2, "mem": {}}])
    monkeypatch.setattr(operations, "call_original", lambda *a: next(calls))
    args = ExperimentFunction().parse({"params": CHAR, "case": {"c": 1}})
    ctx = OperationContext({"binary": b"", "task.json": slot[0].private["task.json"]})
    r = ExperimentFunction().execute(ctx, args)
    assert r.outcome is Outcome.FAILED and "flaky" in r.shown["error"]


def test_probe_budget_cannot_be_bypassed(tmp_path, meta):
    probe = request({"argv": ["a"]}, {"argv": ["b"]}, {"argv": ["c"]})
    proj, _ = project(tmp_path, [[probe, bad_program("a")]], jobs=NoJobs())
    capped = RunConfig(CONFIG.model, CONFIG.max_steps, {"probe": 1})
    run = proj.start(one(meta), capped, Model())
    assert len(ops(proj, "operation")) == 1
    assert proj.status(run.run_id).accounting["probe"].spent == 1
    with Ledger.open(proj.ledger_root) as ledger:
        refused = [o for o in ledger.history() if o.origin.kind == "operation-refused"]
    assert len(refused) == 2


# --- private inputs and forged verdicts ---------------------------------------


def test_workspace_holds_inputs_and_memory_only(tmp_path, meta):
    binary = Path(meta["binary"]).read_bytes()
    plan = [[request({"argv": ["hello"]}), bad_program("a")], [bad_program("b")]]
    proj, steps = project(tmp_path, plan, jobs=NoJobs())
    task = one(meta, 2)
    proj.start(task, CONFIG, Model())
    assert len(steps.episodes) == 2
    for episode in steps.episodes:
        files = episode_files(proj, episode)
        assert set(files) - set(task.inputs) <= {"memory.json", "feedback-001.json"}
        assert not any(binary in data for data in files.values())
    [(_, artifacts)] = ops(proj, "operation")
    assert binary.hex().encode()[:4096] not in artifacts["shown.json"]


def test_worker_written_verdicts_facts_and_notes_are_not_accepted(tmp_path, meta):
    forged = {
        "verdict.json": {"status": "passed", "feedback": {"accepted": True}},
        "facts.json": [{"fn": "__main__", "c_source": "forged"}],
        "notes.json": ["verified: the model is correct"],
        "memory.json": {"entries": [{"tier": "verified"}]},
    }
    stdout, files = bad_program("int main(){return 0;}")
    for name, body in forged.items():
        files[f"candidate/{name}"] = json.dumps(body).encode()
    proj, _ = project(tmp_path, [[(stdout, files)]], jobs=NoJobs())
    task = one(meta)
    run = proj.start(task, CONFIG, Model())
    assert run.outcome is RunOutcome.REJECTED
    assert run.submissions[0].verdicts == {"program-replay": VerdictStatus.REJECTED}
    entries = proj.memory(task)
    assert [e.source for e in entries] == ["notes"]  # recorded, never promoted
    assert not any(e.accepted or e.shown for e in entries)


# --- budgets and the duplicate guard ------------------------------------------


def test_submission_budget_cannot_be_bypassed(tmp_path, meta):
    plan = [[bad_program(f"int v{i};")] for i in range(3)]
    proj, steps = project(tmp_path, plan, jobs=NoJobs())
    run = proj.start(one(meta, 2), CONFIG, Model())
    assert (run.outcome, len(run.submissions)) == (RunOutcome.REJECTED, 2)
    assert len(steps.episodes) == 2


def test_check_budget_cannot_be_bypassed(tmp_path, meta):
    task = tasks.program_task(meta, submissions=3, check_budget=1)
    plan = [[bad_program(f"int v{i};")] for i in range(3)]
    proj, _ = project(tmp_path, plan, jobs=NoJobs())
    run = proj.start(task, CONFIG, Model())
    assert len(run.submissions) == 1 and "budget exhausted" in run.detail
    assert len(ops(proj, "check")) == 1


def _decisions(tmp_path, meta, sources):
    proj, _ = project(tmp_path, [[bad_program(s)] for s in sources], jobs=NoJobs())
    run = proj.start(one(meta, len(sources)), CONFIG, Model())
    return [s.decision for s in run.submissions], len(ops(proj, "check"))


BASE = "int main(void){ int t = 0; for (int i = 0; i < 10; i++) t += i; return t; }"


def test_guard_refuses_an_exact_repeat_before_any_check(tmp_path, meta):
    # comments and whitespace are no change (engine._norm_source)
    sources = [BASE, "/* again */ " + BASE.replace(" ", "  "), BASE + "\n// 3\n"]
    assert _decisions(tmp_path, meta, sources) == (
        ["rejected", "rejected", "duplicate"],
        2,
    )


def test_guard_refuses_a_near_repeat_from_the_fourth(tmp_path, meta):
    sources = [BASE.replace("10", n) for n in ("10", "11", "12", "13")]
    assert _decisions(tmp_path, meta, sources) == (["rejected"] * 3 + ["duplicate"], 3)


def test_guard_lets_a_real_change_through(tmp_path, meta):
    other = "#include <stdio.h>\nint main(int c, char **v){ puts(v[1]); return c; }"
    assert _decisions(tmp_path, meta, [BASE, BASE, other]) == (["rejected"] * 3, 3)


class Reject:
    """Stands in for both checkers: rejects without running anything."""

    version = "stub-reject"
    isolated = True

    def check(self, ctx):
        return Verdict(VerdictStatus.REJECTED, {"accepted": False})


class RejectDomain(ReSchemaDomain):  # keeps ReSchemaDomain.normalize
    checkers = MappingProxyType({"function-fuzz": Reject(), "program-replay": Reject()})


GOOD_PARAMS = json.dumps([{"name": "c", "kind": "i32", "range": [0, 255]}])


@pytest.mark.parametrize(
    ("task_of", "decl", "bad", "fixed"),
    [
        (
            lambda m: tasks.function_task(m, "rot13_char", submissions=3),
            "params.json",
            ['[{"name": "c"}]', '[{"name": "c", "kind": "f64"}]'],
            GOOD_PARAMS,
        ),
        (
            lambda m: tasks.program_task(m, submissions=3),
            "cases.json",
            ["{not json", '[{"argv": "x"}]'],
            '[{"argv": ["hello"], "stdin_hex": ""}]',
        ),
    ],
)
def test_guard_a_corrected_declaration_is_a_new_candidate(
    tmp_path, meta, task_of, decl, bad, fixed
):
    sources = [*bad, fixed]
    plan = [[submit(**{"model.c": BASE, decl: d})] for d in sources]
    proj, _ = project(tmp_path, plan, RejectDomain(), NoJobs())
    run = proj.start(task_of(meta), CONFIG, Model())
    assert [s.decision for s in run.submissions] == ["rejected"] * 3
    assert len(ops(proj, "check")) == 3  # the fix reached the checker

    # identical source and declaration (reformatted JSON) is still a repeat
    same = json.dumps(json.loads(fixed), indent=4)
    proj, _ = project(
        tmp_path / "again",
        [[submit(**{"model.c": BASE, decl: d})] for d in (fixed, same, fixed)],
        RejectDomain(),
        NoJobs(),
    )
    run = proj.start(task_of(meta), CONFIG, Model())
    assert [s.decision for s in run.submissions] == ["rejected"] * 2 + ["duplicate"]
    assert len(ops(proj, "check")) == 2


# --- end to end ---------------------------------------------------------------


@pytest.mark.golden
def test_function_fact_reaches_the_program_task_which_is_accepted(tmp_path, slot):
    fn_case, prog_case = GOLDEN["rot13-o2-rot13char-s1-n8"], GOLDEN["rot13-o2-good"]
    fn_files = {
        "model.c": fn_case["c_source"],
        "params.json": json.dumps(fn_case["params"]),
    }
    prog_files = {
        "model.c": prog_case["c_source"],
        "cases.json": json.dumps(prog_case["cases"]),
    }
    proj, steps = project(tmp_path, [[submit(**fn_files)]])
    fn_task, prog_task = slot[0], slot[2]
    assert proj.start(fn_task, CONFIG, Model()).outcome is RunOutcome.ACCEPTED
    first = json.loads(episode_files(proj, steps.episodes[0])["memory.json"])
    assert first["entries"] == []

    [entry] = proj.memory(prog_task)
    assert (entry.tier, entry.shown, entry.task_id) == ("verified", True, fn_task.id)
    assert entry.items[0]["function"] == "rot13_char"

    steps.plan = [[request({"argv": ["hello"]}), submit(**prog_files)]]
    run = proj.start(prog_task, CONFIG, Model())
    assert run.outcome is RunOutcome.ACCEPTED, ops(proj, "check")[-1][1]
    memory = json.loads(episode_files(proj, steps.episodes[1])["memory.json"])
    [fact] = memory["entries"]
    assert fact["tier"] == "verified" and fact["body"]["source"] == fn_case["c_source"]
    [trace] = shown(proj)
    assert bytes.fromhex(trace["stdout"]) == b"uryyb\n"
