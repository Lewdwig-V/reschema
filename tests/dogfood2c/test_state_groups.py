"""Trial boundaries exercised through run_slot, with real on-disk state."""

import json
from dataclasses import replace

import pytest

from tools.dogfood.runners.base import SlotSpec
from tools.dogfood.slot import SlotGuard, run_slot

from .fakes import FakeRunner

STATE = {
    ".reschema/tasks/rot13__gcc-O0-sym/ledger.json": json.dumps(
        {"accepted": ["rot13"], "probes": 9, "submissions": 2}
    ),
    ".reschema/tasks/rot13__gcc-O0-sym/trace_probe.json": '{"events": [1]}',
    ".reschema/memory/rot13.jsonl": '{"kind": "verified_fact", "fn": "rot13"}\n',
}


class StateRunner(FakeRunner):
    """Observe state at spawn; optionally publish a first branch's work.

    Like FakeRunner, use the engine's on-disk contract without importing its
    process-global ROOT or calling the native compiler/LLM.
    """

    def __init__(self, publish=False):
        super().__init__({"ledger": {}})  # exits naturally; spawn owns writes
        self.publish = publish
        self.seen = {}

    def spawn(self, prompt):
        for name, contents in STATE.items():
            path = self.cfg.run_root / name
            self.seen[name] = path.read_text() if path.exists() else None
            if self.publish:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
        (self.cfg.sandbox / self.cfg.transcript).write_text(self.cfg.transcript)


def _spec(**kwargs):
    return replace(
        SlotSpec("rot13", "unprimed", "gcc-O0-sym", 0, 1, "rot13::gcc-O0-sym"),
        state_group="trial-1",
        **kwargs,
    )


def _run(spec, runner, tmp_path, corpus):
    return run_slot(
        spec,
        campaign_dir=tmp_path / "runs",
        corpus_source=corpus,
        runner=runner,
        guards=SlotGuard(timeout_s=2),
        poll_s=0,
    )


@pytest.mark.parametrize("slot_index", [0, 1], ids=["resume", "sibling"])
def test_grouped_lineage_preserves_judge_state(tmp_path, stub_corpus, slot_index):
    spec = _spec()
    first = StateRunner(publish=True)
    _run(spec, first, tmp_path, stub_corpus)
    resumed = StateRunner()
    out = _run(replace(spec, slot_index=slot_index), resumed, tmp_path, stub_corpus)

    assert resumed.seen == STATE
    record = json.loads(out.read_text())
    assert (record["n_exp"], record["n_sub"]) == (9, 2)
    assert record["run_header"]["state_group"] == spec.state_group
    assert resumed.cfg.run_root == first.cfg.run_root


@pytest.mark.parametrize(
    "changes",
    [{"rep": 2}, {"condition": "primed"}, {"state_group": "trial-2"}],
    ids=["repetition", "condition", "group"],
)
def test_independent_trials_start_without_judge_state(tmp_path, stub_corpus, changes):
    spec = _spec()
    first = StateRunner(publish=True)
    out1 = _run(spec, first, tmp_path, stub_corpus)
    original = out1.read_bytes()
    independent = StateRunner()
    out2 = _run(replace(spec, **changes), independent, tmp_path, stub_corpus)

    assert independent.cfg.run_root != first.cfg.run_root
    assert independent.seen == dict.fromkeys(STATE)
    assert out2 != out1
    assert out1.read_bytes() == original
    assert len(list(out1.parent.glob("*.jsonl"))) == 2
    record = json.loads(out2.read_text())
    assert (record["n_exp"], record["n_sub"], record["accepted"]) == (0, 0, False)


@pytest.mark.parametrize(
    "condition,group",
    [("primed", "r1"), ("unprimed", "gcc-O0-sym-r1")],
)
def test_explicit_groups_cannot_alias_legacy_roots(
    tmp_path, stub_corpus, condition, group
):
    legacy = replace(_spec(condition=condition), state_group=None)
    first = StateRunner(publish=True)
    out1 = _run(legacy, first, tmp_path, stub_corpus)
    independent = StateRunner()
    out2 = _run(replace(legacy, state_group=group), independent, tmp_path, stub_corpus)

    assert independent.cfg.run_root != first.cfg.run_root
    assert independent.seen == dict.fromkeys(STATE)
    assert out1 != out2


def test_sibling_results_and_transcripts_survive_resume(tmp_path, stub_corpus):
    spec = _spec()
    first, sibling = StateRunner(publish=True), StateRunner()
    out1 = _run(spec, first, tmp_path, stub_corpus)
    original = out1.read_bytes()
    out2 = _run(replace(spec, slot_index=1), sibling, tmp_path, stub_corpus)
    transcript1 = first.cfg.sandbox / first.cfg.transcript
    transcript2 = sibling.cfg.sandbox / sibling.cfg.transcript

    assert out1 != out2
    assert out1.read_bytes() == original
    assert transcript1 != transcript2
    assert transcript1.read_text() == first.cfg.transcript
    assert transcript2.read_text() == sibling.cfg.transcript
    second_record, second_transcript = out2.read_bytes(), transcript2.read_bytes()

    assert _run(spec, StateRunner(), tmp_path, stub_corpus) == out1
    assert out2.read_bytes() == second_record
    assert transcript2.read_bytes() == second_transcript


def test_group_labels_are_opaque_and_cannot_escape_the_trial_root(
    tmp_path, stub_corpus
):
    spec = replace(_spec(), state_group="trial/../../outside")
    runner = StateRunner()
    _run(spec, runner, tmp_path, stub_corpus)
    assert runner.cfg.run_root.resolve().parent == (tmp_path / "runs").resolve()


def test_trial_identity_preserves_component_boundaries():
    a = replace(_spec(), condition="A-B", state_group="C")
    b = replace(_spec(), condition="A", state_group="B-C")
    assert a.state_root_id != b.state_root_id
