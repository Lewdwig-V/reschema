import json

import pytest

from tools.dogfood.driver import expand_campaign, run_campaign
from tools.dogfood.slot import SlotGuard

from .fakes import FakeRunner, PreflightFakeRunner

SMOKE = """
[[chains]]
family = "rot13"
slots = ["gcc-O0-sym", "gcc-O1-sym", "gcc-O2-sym"]
reps = 1
"""


def test_expand_campaign_counts_and_conditions(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE)
    specs = expand_campaign(cfg)
    assert len(specs) == 2 * 3
    assert {s.condition for s in specs} == {"primed", "unprimed"}


def test_expand_campaign_rejects_duplicate_result_stems(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE + SMOKE)  # same family twice: every stem doubles
    with pytest.raises(ValueError, match="duplicate result stems"):
        expand_campaign(cfg)


def test_primed_chain_runs_sequentially_and_in_slot_order(tmp_path, stub_corpus):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE)
    created = []

    def mk():
        r = FakeRunner(
            {
                "task_id": "rot13::gcc-O0-sym",
                "ledger": {"accepted": ["program"], "submissions": 1, "probes": 3},
            }
        )
        created.append(r)
        return r

    rc = run_campaign(
        cfg,
        runner_factory=mk,
        corpus_source=stub_corpus,
        pool_size=2,
        out_dir=tmp_path / "out",
        guards=SlotGuard(timeout_s=30),
        poll_s=0,
    )
    assert rc == 0
    names = sorted(tmp_path.glob("out/rot13-primed-*.jsonl"))
    assert [p.name for p in names] == [
        "rot13-primed-gcc-O0-sym-r1-s0.jsonl",
        "rot13-primed-gcc-O1-sym-r1-s1.jsonl",
        "rot13-primed-gcc-O2-sym-r1-s2.jsonl",
    ]
    # fake always accepts; sequentiality is structural (chain() is a plain
    # loop), so the whole chain must land accepted, in index order
    assert [json.loads(p.read_text())["outcome"] for p in names] == ["accepted"] * 3
    assert [json.loads(p.read_text())["slot_index"] for p in names] == [0, 1, 2]
    # the headline claim: ONE shared memory root per primed chain
    roots = [str(r.cfg.run_root) for r in created]
    assert roots.count(str(tmp_path / "out/runs/rot13-primed-r1")) == 3


def _run_smoke(tmp_path, stub_corpus, mk, out_name="out", pool_size=2):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE)
    return run_campaign(
        cfg,
        runner_factory=mk,
        corpus_source=stub_corpus,
        pool_size=pool_size,
        out_dir=tmp_path / out_name,
        guards=SlotGuard(timeout_s=30),
        poll_s=0,
    )


def test_infra_streak_aborts_campaign(tmp_path, stub_corpus):
    def mk():  # endpoint dead for EVERY slot: 3 consecutive => campaign aborts
        return PreflightFakeRunner({"task_id": "rot13::gcc-O0-sym"})

    with pytest.raises(RuntimeError, match="infra-error streak"):
        _run_smoke(tmp_path, stub_corpus, mk)


def test_priming_failure_shortcircuits_the_chain(tmp_path, stub_corpus):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE)

    def mk():  # slot 0 NEVER accepts
        return FakeRunner(
            {
                "task_id": "rot13::gcc-O0-sym",
                "ledger": {"accepted": [], "submissions": 2, "probes": 3},
            }
        )

    rc = run_campaign(
        cfg,
        runner_factory=mk,
        corpus_source=stub_corpus,
        pool_size=2,
        out_dir=tmp_path / "out",
        guards=SlotGuard(timeout_s=30),
        poll_s=0,
    )
    assert rc == 0
    s1 = json.loads((tmp_path / "out/rot13-primed-gcc-O1-sym-r1-s1.jsonl").read_text())
    assert s1["outcome"] == "aborted: priming-failed"
    assert s1["E"] == 0.0
    # abort_reason names the real cause: which slot, and its actual outcome
    assert "gcc-O0-sym" in s1["abort_reason"]
    assert "aborted: agent-exit" in s1["abort_reason"]


def test_priming_failed_record_key_parity_with_accepted(tmp_path, stub_corpus):
    accept = {
        "task_id": "rot13::gcc-O0-sym",
        "ledger": {"accepted": ["program"], "submissions": 1, "probes": 3},
    }
    reject = {
        "task_id": "rot13::gcc-O0-sym",
        "ledger": {"accepted": [], "submissions": 2, "probes": 3},
    }
    _run_smoke(tmp_path, stub_corpus, lambda: FakeRunner(accept), out_name="a")
    _run_smoke(tmp_path, stub_corpus, lambda: FakeRunner(reject), out_name="b")
    acc = json.loads((tmp_path / "a/rot13-primed-gcc-O1-sym-r1-s1.jsonl").read_text())
    failed = json.loads(
        (tmp_path / "b/rot13-primed-gcc-O1-sym-r1-s1.jsonl").read_text()
    )
    assert acc["outcome"] == "accepted"
    assert failed["outcome"] == "aborted: priming-failed"
    # key parity pinned BOTH directions: no missing keys, no extras
    assert set(acc) <= set(failed)
    assert set(failed) <= set(acc)


def test_infra_flap_leaves_chain_resumable(tmp_path, stub_corpus):
    out_dir = tmp_path / "out"
    state = {"calls": 0, "flapped": False}

    def mk():
        # pool_size=1: factory call #2 is primed s1's runner — flap it ONCE
        state["calls"] += 1
        if state["calls"] == 2 and not state["flapped"]:
            state["flapped"] = True
            return PreflightFakeRunner({"task_id": "rot13::gcc-O0-sym"})
        return FakeRunner(
            {
                "task_id": "rot13::gcc-O0-sym",
                "ledger": {"accepted": ["program"], "submissions": 1, "probes": 3},
            }
        )

    rc = _run_smoke(tmp_path, stub_corpus, mk, pool_size=1)
    assert rc == 0  # a single flap is below the infra-streak abort
    s1_path = out_dir / "rot13-primed-gcc-O1-sym-r1-s1.jsonl"
    s2_path = out_dir / "rot13-primed-gcc-O2-sym-r1-s2.jsonl"
    assert json.loads(s1_path.read_text())["outcome"] == "infra-error"
    # a transient flap is NOT a priming rejection: the rest of the chain
    # stays UNRECORDED so resume retries it instead of truncating the arm
    assert not s2_path.exists()

    rc = _run_smoke(tmp_path, stub_corpus, mk, pool_size=1)
    assert rc == 0
    assert json.loads(s1_path.read_text())["outcome"] == "accepted"  # retried
    assert json.loads(s2_path.read_text())["outcome"] == "accepted"


def test_resume_skips_completed_but_retries_infra_error(tmp_path, stub_corpus):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    done = out_dir / "rot13-unprimed-gcc-O0-sym-r1.jsonl"
    done.write_text(
        json.dumps({"slot_id": "rot13-unprimed-gcc-O0-sym-r1", "outcome": "accepted"})
        + "\n"
    )
    infra = out_dir / "rot13-unprimed-gcc-O1-sym-r1.jsonl"
    infra.write_text(
        json.dumps(
            {"slot_id": "rot13-unprimed-gcc-O1-sym-r1", "outcome": "infra-error"}
        )
        + "\n"
    )

    def mk():
        return FakeRunner(
            {
                "task_id": "rot13::gcc-O0-sym",
                "ledger": {"accepted": ["program"], "submissions": 1, "probes": 3},
            }
        )

    rc = run_campaign(
        cfg,
        runner_factory=mk,
        corpus_source=stub_corpus,
        pool_size=2,
        out_dir=out_dir,
        guards=SlotGuard(timeout_s=30),
        poll_s=0,
    )
    assert rc == 0
    assert json.loads(done.read_text())["outcome"] == "accepted"  # skipped, untouched
    o1 = json.loads(infra.read_text())
    assert o1["outcome"] == "accepted"  # retried: now a real record
    assert len(list(out_dir.glob("*.jsonl"))) == 6
