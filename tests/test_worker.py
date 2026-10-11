"""M8 S6: the worker's helpers (tools/worker/, on the worker image's PATH), the
candidate's result.json form, task.md, and the CLI task files. The helpers are
stdlib Python and run here on the host; the CLI campaign that runs them in the
real sandbox is tests/test_cli_campaign.py (golden)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from warranted import CheckContext, TaskSpec, VerdictStatus

from reschema import tasks
from reschema.checks import candidate_files
from reschema.checks.function_fuzz import FunctionFuzz
from reschema.checks.program_replay import ProgramReplay
from reschema.domain import ReSchemaDomain
from reschema.engine import TaskStore

HELPERS = Path(__file__).resolve().parent.parent / "tools" / "worker"
MARK = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
PARAMS = [{"name": "c", "kind": "i32", "range": [0, 255]}]


def helper(name, *args, work=None):
    env = {**os.environ, "RESCHEMA_WORK": str(work)} if work else None
    return subprocess.run(
        [sys.executable, "-I", str(HELPERS / name), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def request(out: str) -> list[dict]:
    marker, body = out.split("\n", 1)
    assert marker == "WARRANTED_REQUEST"
    return json.loads(body)["requests"]


# --- probing helpers ----------------------------------------------------------


def test_experiment_prints_one_request_per_object():
    r = helper("experiment", '{"argv": ["hello"]}', '{"stdin": "x", "quiet": true}')
    assert r.returncode == 0
    assert request(r.stdout) == [
        {"operation": "experiment", "arguments": {"argv": ["hello"]}},
        {"operation": "experiment", "arguments": {"stdin": "x", "quiet": True}},
    ]


def test_experiment_function_adds_the_params(tmp_path):
    (tmp_path / "params.json").write_text(json.dumps(PARAMS))
    r = helper(
        "experiment-function", "--params", str(tmp_path / "params.json"), '{"c": 1}'
    )
    assert r.returncode == 0
    assert request(r.stdout) == [
        {
            "operation": "experiment_function",
            "arguments": {"params": PARAMS, "case": {"c": 1}},
        }
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("experiment",),
        ("experiment", "hello"),  # not JSON
        ("experiment", "[]"),  # not an object
        ("experiment", *['{"argv": []}'] * 33),  # over Warranted's batch limit
        ("experiment-function", "--params", "/nonexistent", '{"c": 1}'),
    ],
)
def test_a_bad_probe_is_no_request(args):
    # a failing command is never read as a request
    r = helper(*args)
    assert r.returncode != 0 and "WARRANTED_REQUEST" not in r.stdout


# --- submit -------------------------------------------------------------------


def work(tmp_path, kind, **files):
    (tmp_path / "workspace").mkdir(parents=True)
    (tmp_path / f"{kind}.json").write_text("{}")
    for name, text in files.items():
        (tmp_path / "workspace" / name).write_text(text)
    return tmp_path


def test_submit_packs_the_task_kinds_files(tmp_path):
    w = work(
        tmp_path / "fn",
        "function",
        **{"model.c": "int f;", "params.json": "[]", "cases.json": "[]"},
    )
    r = helper("submit", work=w)
    assert (r.returncode, r.stdout.splitlines()[0]) == (0, MARK)
    # only the function task's declaration: a stray cases.json would be a reject
    assert json.loads((w / "result.json").read_text()) == {
        "model.c": "int f;",
        "params.json": "[]",
    }
    w = work(tmp_path / "prog", "program", **{"model.c": "int main;"})
    assert helper("submit", work=w).returncode == 0
    assert json.loads((w / "result.json").read_text()) == {"model.c": "int main;"}


@pytest.mark.parametrize(
    ("kind", "files"),
    [
        ("function", {"model.c": "int f;"}),  # no params.json
        ("program", {"cases.json": "[]"}),  # no model.c
        ("program", {"model.c": "x", "cases.json": "{not json"}),
        ("function", {"model.c": "x", "params.json": "{}"}),
    ],
)
def test_submit_refuses_without_marking(tmp_path, kind, files):
    w = work(tmp_path, kind, **files)
    r = helper("submit", work=w)
    assert r.returncode != 0 and MARK not in r.stdout
    assert not (w / "result.json").exists()


# --- the candidate's result.json form -----------------------------------------


def bundle(**files) -> dict:
    return {"result.json": json.dumps(files).encode()}


def test_result_json_entries_are_the_candidate_files():
    got = candidate_files(bundle(**{"model.c": "int x;", "cases.json": "[]"}))
    assert got == {"model.c": b"int x;", "cases.json": b"[]"}
    direct = {"result.json": b"{}", "model.c": b"int x;", "workspace.json": b"?"}
    assert candidate_files(direct) == {"model.c": b"int x;"}


@pytest.mark.parametrize(
    "candidate",
    [
        {"result.json": b"{not json"},
        {"result.json": b"[]"},
        bundle(**{"verdict.json": "passed"}),  # only the model's files
        {"result.json": b'{"model.c": 1}'},
        {**bundle(**{"model.c": "a"}), "model.c": b"b"},  # two readings
    ],
)
def test_a_malformed_result_json_is_a_structured_reject(candidate):
    assert type(candidate_files(candidate)) is str
    v = ProgramReplay().check(CheckContext(candidate, {}, {}))
    assert (v.status, v.feedback["stage"]) == (VerdictStatus.REJECTED, "candidate")
    task = {"task.json": json.dumps({"function": {"name": "f"}}).encode()}
    v = FunctionFuzz().check(CheckContext(candidate, {}, task))
    assert (v.status, v.feedback["reason"]) == (VerdictStatus.REJECTED, "spec")


def test_a_lone_surrogate_is_a_reject_not_a_crash():
    v = ProgramReplay().check(
        CheckContext({"result.json": b'{"model.c": "\\ud800"}'}, {}, {})
    )
    assert v.status is VerdictStatus.REJECTED and "UTF-8" in v.feedback["detail"]


def test_guard_fingerprints_the_files_not_the_workspace():
    files = {"model.c": "int main(){return 0;}", "cases.json": "[]"}
    a = ReSchemaDomain.normalize({**bundle(**files), "workspace.json": b"one"})
    b = ReSchemaDomain.normalize({**bundle(**files), "workspace.json": b"two"})
    direct = {k: v.encode() for k, v in files.items()}
    assert a == b == ReSchemaDomain.normalize(direct)


# --- task.md and task files ---------------------------------------------------


@pytest.fixture(scope="module")
def meta(built_corpus):
    return TaskStore("rot13::gcc-O2-sym").meta


def test_task_md_per_kind(meta):
    fn = tasks.function_task(meta, "rot13_char").inputs["task.md"].decode()
    prog = tasks.program_task(meta).inputs["task.md"].decode()
    assert "`rot13_char`" in fn and "experiment-function '" in fn
    assert "is for\nfunction tasks only" in fn and "workspace/params.json" in fn
    assert "experiment '" in prog and "here it fails" in prog
    assert "workspace/cases.json" in prog and "params.json" not in prog
    for text in (fn, prog):
        assert "submit" in text and "memory.json" in text and "$" not in text


def test_task_md_holds_nothing_private(meta, built_corpus):
    # a function of the task kind, budget and function name only: no seed,
    # binary, hidden grammar or slot detail can reach it
    other = next(m for m in built_corpus if m["seed"] != "rot13")
    for t in tasks.slot_tasks(meta):
        binary = t.private["binary"]
        assert binary[:64] not in t.inputs["task.md"]
    assert (
        tasks.program_task(meta).inputs["task.md"]
        == tasks.program_task(other).inputs["task.md"]
    )


def test_written_tasks_load_back_equal(meta, tmp_path):
    specs = tasks.slot_tasks(meta)
    path = tasks.write_campaign("c1", specs, {"cfg": "run.toml"}, tmp_path)
    for i, t in enumerate(specs):
        [d] = tmp_path.glob(f"tasks/{i:03d}-*")
        assert TaskSpec.load(d / "task.toml") == t
    assert path.read_text().count("[[tasks]]") == len(specs)
