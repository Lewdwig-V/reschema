"""#127 treatment wiring; no runner continuation, model, endpoint, or container."""

import json

import pytest

from tools.dogfood.driver import run_campaign
from tools.dogfood.measure import render_report
from tools.dogfood.runners.base import RunnerConfig, SlotSpec
from tools.dogfood.runners.opencode_v1 import OpenCodeV1Runner
from tools.dogfood.slot import SlotGuard, run_slot

from .fakes import FakeRunner, PreflightFakeRunner

VERSION = "rejection-once-v1"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("infra", [False, True])
def test_slot_stamps_effective_treatment_before_preflight(
    tmp_path, stub_corpus, enabled, infra
):
    runner = (PreflightFakeRunner if infra else FakeRunner)(
        {"ledger": {"accepted": [], "submissions": 2, "rejections": 2, "probes": 3}}
    )
    out = run_slot(
        SlotSpec("rot13", "unprimed", "gcc-O0-sym", 0, 1, "rot13::gcc-O0-sym"),
        campaign_dir=tmp_path / "runs",
        runner=runner,
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
        continuation_feedback=enabled,
        run_header={"continuation_feedback": "untrusted-header"},
    )
    rec = json.loads(out.read_text())
    assert rec["run_header"]["continuation_feedback"] == (VERSION if enabled else "off")
    assert rec["outcome"] == ("infra-error" if infra else "aborted: agent-exit")
    if not infra:
        assert (rec["n_exp"], rec["n_sub"]) == (3, 2)
        assert runner.cfg.continuation_feedback is enabled
        if enabled:
            assert runner.cfg.feedback_deadline > 0
            assert runner.cfg.feedback_probe_ceiling == 5


@pytest.mark.parametrize("enabled", [False, True])
def test_runner_pins_feedback_and_limits_over_inherited_environment(
    tmp_path, monkeypatch, enabled
):
    for key in (
        "RESCHEMA_CONTINUATION_FEEDBACK",
        "RESCHEMA_FEEDBACK_DEADLINE",
        "RESCHEMA_FEEDBACK_PROBE_CEILING",
    ):
        monkeypatch.setenv(key, "foreign-setting")
    cfg = RunnerConfig(
        "model",
        None,
        tmp_path / "sandbox",
        tmp_path / "run",
        continuation_feedback=enabled,
        feedback_deadline=12345.0,
        feedback_probe_ceiling=5,
    )
    OpenCodeV1Runner().prepare(cfg)
    env = json.loads((cfg.sandbox / "opencode.json").read_text())["mcp"]["reschema"][
        "environment"
    ]
    assert env["RESCHEMA_CONTINUATION_FEEDBACK"] == (VERSION if enabled else "off")
    assert env["RESCHEMA_FEEDBACK_DEADLINE"] == ("12345.0" if enabled else "")
    assert env["RESCHEMA_FEEDBACK_PROBE_CEILING"] == ("5" if enabled else "")


def campaign(tmp_path, stub_corpus, enabled):
    cfg = tmp_path / "campaign.toml"
    cfg.write_text(
        '[[chains]]\nfamily = "rot13"\nslots = ["gcc-O0-sym", "gcc-O1-sym"]\nreps = 1\n'
    )
    return run_campaign(
        cfg,
        runner_factory=lambda: FakeRunner(
            {"ledger": {"accepted": [], "submissions": 2, "rejections": 2, "probes": 3}}
        ),
        corpus_source=stub_corpus,
        out_dir=tmp_path / "out",
        pool_size=1,
        poll_s=0,
        continuation_feedback=enabled,
    )


def test_campaign_stamps_synthetic_failures_and_refuses_mixed_resume(
    tmp_path, stub_corpus
):
    campaign(tmp_path, stub_corpus, True)
    recs = [json.loads(p.read_text()) for p in (tmp_path / "out").glob("*.jsonl")]
    assert len(recs) == 4
    assert any(r["outcome"] == "aborted: priming-failed" for r in recs)
    assert all(r["run_header"]["continuation_feedback"] == VERSION for r in recs)
    with pytest.raises(ValueError, match="continuation feedback treatment"):
        campaign(tmp_path, stub_corpus, False)
    assert campaign(tmp_path, stub_corpus, True) == 0


def test_reports_refuse_to_pool_legacy_baseline_with_treatment(tmp_path, stub_corpus):
    campaign(tmp_path, stub_corpus, True)
    path = next((tmp_path / "out").glob("*.jsonl"))
    rec = json.loads(path.read_text())
    del rec["run_header"]["continuation_feedback"]  # historical baseline
    path.write_text(json.dumps(rec) + "\n")
    with pytest.raises(ValueError, match="continuation feedback treatment"):
        render_report(tmp_path / "out", family="rot13", out_dir=tmp_path / "out")
