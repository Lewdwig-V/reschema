"""M8 parity fixture (docs/proposals/m8-rebuild-on-warranted.md, slice S1):
today's gates reproduce the accept/reject decisions recorded in
tests/golden/m8_decisions.json. The Warranted-backed checkers must later pass
the same file, so a decision that changes is caught on either side.

Cases come from the existing gate tests (each names its `source_test`). Seeds
are pinned so a hidden or fuzz draw cannot move a verdict between runs; a case
whose source test drew fresh entropy is pinned to a derived seed here.

Program cases follow decision D3: the recorded stage replays the case's own
nominated inputs, double-recorded from the corpus binary for this run, never a
task directory's stored traces.

Marked `golden`: CI runs it as its own step, outside the default suite's
wall-clock budget."""

import json
import tempfile
from pathlib import Path

import pytest

from reschema.driver.spec import Param
from reschema.engine import TaskStore, _record_stable, program_gate
from reschema.validate.function import validate_function

pytestmark = pytest.mark.golden

FIXTURE = json.loads(
    (Path(__file__).parent / "golden" / "m8_decisions.json").read_text()
)


def _id(case):
    return case["id"]


def test_fixture_ids_are_unique():
    ids = [c["id"] for c in FIXTURE["function"] + FIXTURE["program"]]
    assert len(ids) == len(set(ids)), ids


@pytest.mark.parametrize("case", FIXTURE["function"], ids=_id)
def test_function_gate_decision(built_corpus, case):
    meta = TaskStore(case["task_id"]).meta
    fmeta = meta["functions"][case["func"]]
    seed = case["seed"] if case["seed"] is not None else f"golden:{case['id']}"
    with tempfile.TemporaryDirectory(prefix="reschema-golden-") as d:
        v = validate_function(
            meta["binary"],
            fmeta["addr"],
            case["func"],
            [Param.from_json(p) for p in case["params"]],
            case["c_source"],
            Path(d) / f"{case['func']}.so",
            seed=seed,
            n_fuzz=case["n_fuzz"],
            size=fmeta["size"],
        )
    stage = None if v.ok else v.divergence.get("stage")
    assert {"ok": v.ok, "stage": stage} == case["expect"], v.divergence


@pytest.mark.parametrize("case", FIXTURE["program"], ids=_id)
def test_program_gate_decision(built_corpus, case):
    store = TaskStore(case["task_id"])
    rec = []
    for c in case["cases"]:
        t = _record_stable(
            store.meta["binary"], c["argv"], bytes.fromhex(c["stdin_hex"])
        )
        assert t is not None, f"flaky ground truth for {c}"
        rec.append(t)
    seed = case["hidden_seed"] or f"golden:{case['id']}"
    with tempfile.TemporaryDirectory(prefix="reschema-golden-") as d:
        fail, _ = program_gate(
            store, case["c_source"], Path(d) / "model", hidden_seed=seed, rec=rec
        )
    got = {
        "accepted": fail is None,
        "reason": None if fail is None else fail["reason"],
        "stage": None if fail is None else fail.get("stage"),
    }
    assert got == case["expect"], fail
