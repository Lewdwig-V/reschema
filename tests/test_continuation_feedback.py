"""#127: presentation-only treatment, using real task ledgers and stub judges."""

import copy
import json
from unittest.mock import Mock

import pytest

from reschema import engine as eng
from reschema.validate.function import FnVerdict
from reschema.validate.program import Verdict

ENV = "RESCHEMA_CONTINUATION_FEEDBACK"
VERSION = "rejection-once-v1"
PARAMS = [{"name": "x", "kind": "i32"}]
SOURCE = "int f(int x) { return x; }"
DIVERGENCE = {"input": {"x": 1}, "field": "ret", "expected": "2", "actual": "1"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv("RESCHEMA_FEEDBACK_DEADLINE", raising=False)
    monkeypatch.delenv("RESCHEMA_FEEDBACK_PROBE_CEILING", raising=False)
    monkeypatch.setattr(eng, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(
        eng,
        "load_manifest",
        lambda: [
            {
                "task_id": tid,
                "seed": "calc",
                "binary": "unused",
                "functions": {"f": {"addr": 1, "size": 1}, "g": {"addr": 2, "size": 1}},
            }
            for tid in ("task", "sibling")
        ],
    )
    monkeypatch.setattr(eng, "_record_notes", Mock())
    monkeypatch.setattr(eng, "_topology_digest", lambda *a: {})
    monkeypatch.setattr("reschema.memory.append_fact", Mock())
    return eng.TaskStore("task")


def function_reject(store, monkeypatch, divergence=None):
    monkeypatch.setattr(
        eng,
        "validate_function",
        Mock(
            return_value=FnVerdict(
                False,
                copy.deepcopy(divergence if divergence is not None else DIVERGENCE),
                compared=19,
                skipped=7,
                seed="JUDGE-PRIVATE-SEED",
            )
        ),
    )
    return eng.submit_function(store, "f", PARAMS, SOURCE)


def program_judge(monkeypatch, *, stage="hidden", accepted=False):
    monkeypatch.setattr(eng, "compile_model", Mock(return_value=(True, "")))
    bad = Verdict(False, "io-mismatch", copy.deepcopy(DIVERGENCE))
    monkeypatch.setattr(
        eng,
        "replay_against",
        Mock(
            side_effect=[
                bad if stage == "recorded" else Verdict(True),
                Verdict(True) if accepted else bad,
            ]
        ),
    )
    monkeypatch.setattr(eng.secrets, "token_hex", Mock(return_value="FRESH-SEED"))
    monkeypatch.setattr(
        eng,
        "hidden_input_stream",
        lambda *a, **kw: (([str(i)], b"") for i in range(20)),
    )
    monkeypatch.setattr(eng, "_record_stable", Mock(return_value={"exit_code": 0}))


def test_feedback_payload_is_pinned_and_scoped(store, monkeypatch):
    monkeypatch.setenv(ENV, VERSION)
    led = store.ledger()
    led["accepted"] = [{"f": "PREVIOUS ACCEPTED SOURCE"}, {"g": "ACCEPTED HELPER"}]
    led["audit"] = {"g": {"seed": "PRIVATE-AUDIT"}}
    store.save_ledger(led)
    out = function_reject(store, monkeypatch)
    feedback = out.pop("continuation_feedback")
    assert out == {"accepted": False, "divergence": DIVERGENCE}
    assert feedback == {
        "version": VERSION,
        "provenance": "procedural coaching, not a verified fact",
        "task_complete": False,
        "candidate": {"mode": "function", "function": "f"},
        "message": "This candidate was rejected; the program task remains unfinished. "
        "Rejection is an expected part of recovery. The reported rejection "
        "gives you a concrete discrepancy to investigate.",
        "evidence": "divergence in this response",
        "accepted_functions": ["f", "g"],
        "scope": "These are previously accepted function models. Their acceptance "
        "does not validate this candidate or any revised candidate.",
        "next_actions": "Within the harness limits, repair against the reported "
        "discrepancy, run a distinguishing experiment, or revisit an assumption.",
        "repair_directive": {
            "trigger": "1 recent rejection(s); last stage: divergence",
            "order": [
                "1) abstract bit-logic repair: fixed-width integers and exact byte-level behavior, no idiomatic or semantic attempts; satisfy the verifier bare-minimum",
                "2) idiomatic annotation and semantic refinement (types, names, structure): only AFTER the submission is accepted",
            ],
            "provenance": "coaching guidance derived from your rejection history, not a verified fact",
        },
    }
    assert feedback["repair_directive"] == eng._repair_directive(store)
    assert store.ledger()["continuation_feedback"] == VERSION
    from reschema.memory import append_fact

    append_fact.assert_not_called()


@pytest.mark.parametrize("mode", ["function", "recorded", "hidden"])
def test_treatment_preserves_verdict_ledger_entropy_and_cost(store, monkeypatch, mode):
    results = []
    for setting in ("off", VERSION):
        monkeypatch.setenv(ENV, setting)
        store.save_ledger(
            {"accepted": [], "submissions": 0, "rejections": 0, "probes": 3}
        )
        if mode == "function":
            out = function_reject(store, monkeypatch)
            eng.validate_function.assert_called_once()
            assert eng.validate_function.call_args.kwargs["seed"] is None
            assert eng.validate_function.call_args.kwargs["n_fuzz"] == eng.N_FUZZ
        else:
            program_judge(monkeypatch, stage=mode)
            out = eng.submit_program(store, SOURCE)
            assert eng.secrets.token_hex.call_count == (mode == "hidden")
            assert eng.replay_against.call_count == (2 if mode == "hidden" else 1)
        led = store.ledger()
        if setting == VERSION:
            feedback = out.pop("continuation_feedback")
            assert feedback["candidate"]["mode"] == (
                "function" if mode == "function" else "program"
            )
            led.pop("continuation_feedback")
            assert "FRESH-SEED" not in json.dumps(feedback)
            assert "JUDGE-PRIVATE" not in json.dumps(feedback)
        else:
            assert "continuation_feedback" not in out
            assert "continuation_feedback" not in led
        assert (led["submissions"], led["rejections"], led["probes"]) == (1, 1, 3)
        results.append((out, led, eng.status_snapshot(store)["efficiency"]))
    assert results[0] == results[1]


@pytest.mark.parametrize("stage", ["spec", "arity", "compile", "link", "symbol"])
def test_repairable_mechanical_rejects_get_feedback(store, monkeypatch, stage):
    monkeypatch.setenv(ENV, VERSION)
    out = function_reject(
        store, monkeypatch, {"stage": stage, "detail": "reported fault"}
    )
    assert out["continuation_feedback"]["evidence"] == "divergence in this response"
    assert "counterexample" not in json.dumps(out["continuation_feedback"])


@pytest.mark.parametrize(
    "mode,stage",
    [
        ("function", "infra"),
        ("function", "skip-starvation"),
        ("function", "unknown-future-stage"),
        ("program", "infra"),
        ("program", "hidden-starvation"),
    ],
)
def test_faults_do_not_consume_feedback_allowance(store, monkeypatch, mode, stage):
    monkeypatch.setenv(ENV, VERSION)
    if mode == "function":
        out = function_reject(store, monkeypatch, {"stage": stage, "detail": "fault"})
    elif stage == "infra":
        monkeypatch.setattr(
            eng, "compile_model", lambda *a: (False, "compile infra: offline")
        )
        out = eng.submit_program(store, SOURCE)
    else:
        program_judge(monkeypatch)
        monkeypatch.setattr(eng, "_record_stable", lambda *a: None)
        out = eng.submit_program(store, SOURCE)
    assert "continuation_feedback" not in out
    assert "continuation_feedback" not in store.ledger()
    assert "continuation_feedback" in function_reject(store, monkeypatch)


def test_malformed_spec_and_duplicate_early_returns(store, monkeypatch):
    monkeypatch.setenv(ENV, VERSION)
    out = eng.submit_function(store, "f", [{"name": "x"}], SOURCE)
    assert out["reason"] == "spec"
    assert out["continuation_feedback"]["evidence"] == "detail in this response"
    # A legacy task can first enable coaching when it already hits the guard.
    led = {
        "accepted": [],
        "submissions": 2,
        "rejections": 2,
        "rejected_norm": [eng._norm_source(SOURCE)] * 2,
    }
    for mode in ("function", "program"):
        store.save_ledger(copy.deepcopy(led))
        out = (
            eng.submit_function(store, "f", PARAMS, SOURCE)
            if mode == "function"
            else eng.submit_program(store, SOURCE)
        )
        assert out["reason"] == "duplicate"
        assert out["continuation_feedback"]["candidate"]["mode"] == mode


def test_once_per_task_across_modes_reopens_and_sibling_tasks(store, monkeypatch):
    # Disabled feedback must not consume the later opt-in.
    assert "continuation_feedback" not in function_reject(store, monkeypatch)
    monkeypatch.setenv(ENV, VERSION)
    assert "continuation_feedback" in function_reject(store, monkeypatch)
    reopened = eng.TaskStore("task")
    monkeypatch.setattr(eng, "compile_model", lambda *a: (False, "syntax error"))
    for i in range(20):  # survive eviction from the capped recent journal
        assert "continuation_feedback" not in eng.submit_program(reopened, f"bad {i}")
    assert "continuation_feedback" in function_reject(
        eng.TaskStore("sibling"), monkeypatch
    )


@pytest.mark.parametrize("program_done", [False, True])
def test_function_acceptance_keeps_existing_completion_framing(
    store, monkeypatch, program_done
):
    monkeypatch.setenv(ENV, VERSION)
    led = store.ledger()
    if program_done:
        led["accepted"] = ["program"]
    store.save_ledger(led)
    monkeypatch.setattr(
        eng, "validate_function", lambda *a, **kw: FnVerdict(True, compared=64, seed=42)
    )
    out = eng.submit_function(store, "f", PARAMS, SOURCE)
    assert out == {
        "accepted": True,
        "compared": 64,
        "skipped": 0,
        "seed": 42,
        "task_complete": program_done,
        **({} if program_done else {"note": eng.TASK_INCOMPLETE_NOTE}),
    }
    assert "continuation_feedback" not in store.ledger()
    rejected = function_reject(store, monkeypatch)
    assert ("continuation_feedback" in rejected) is not program_done


def test_program_completion_never_receives_unfinished_coaching(store, monkeypatch):
    monkeypatch.setenv(ENV, VERSION)
    program_judge(monkeypatch, accepted=True)
    out = eng.submit_program(store, SOURCE)
    assert out == {
        "accepted": True,
        "recorded_cases": 0,
        "hidden_cases": eng.HIDDEN_N,
        "hidden_seed": "hidden:task:FRESH-SEED",
        "task_complete": True,
    }
    assert "continuation_feedback" not in function_reject(store, monkeypatch)


@pytest.mark.parametrize(
    "key,value",
    [
        ("RESCHEMA_FEEDBACK_DEADLINE", "1"),
        ("RESCHEMA_FEEDBACK_PROBE_CEILING", "2"),
        ("RESCHEMA_FEEDBACK_DEADLINE", "invalid"),
    ],
)
def test_owner_supplied_hard_stops_suppress_coaching_not_verdict(
    store, monkeypatch, key, value
):
    monkeypatch.setenv(ENV, VERSION)
    monkeypatch.setenv(key, value)
    led = store.ledger()
    led["probes"] = 3
    store.save_ledger(led)
    out = function_reject(store, monkeypatch)
    assert out == {"accepted": False, "divergence": DIVERGENCE}
    assert "continuation_feedback" not in store.ledger()


def test_mcp_treatment_keeps_schema_and_fuzz_floor(store, monkeypatch):
    from conftest import mcp_call

    monkeypatch.setenv(ENV, VERSION)
    judge = Mock(return_value=FnVerdict(False, DIVERGENCE))
    monkeypatch.setattr(eng, "validate_function", judge)
    out = mcp_call(
        "submit_model",
        task_id="task",
        function="f",
        params=PARAMS,
        c_source=SOURCE,
        seed=17,
        n_fuzz=1,
    )
    assert out["continuation_feedback"]["version"] == VERSION
    assert judge.call_args.kwargs["n_fuzz"] == eng.N_FUZZ
    assert judge.call_args.kwargs["seed"] == 17
