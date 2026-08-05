import json

from tools.dogfood.driver import expand_campaign, run_campaign
from tools.dogfood.slot import SlotGuard

from .fakes import FakeRunner

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


def test_primed_chain_runs_sequentially_and_in_slot_order(tmp_path, stub_corpus):
    cfg = tmp_path / "c.toml"
    cfg.write_text(SMOKE)

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
    assert o1["outcome"] != "infra-error"  # retried: now a real record
    assert len(list(out_dir.glob("*.jsonl"))) >= 6
