from tools.dogfood.driver import run_campaign
from tools.dogfood.measure import render_report
from tools.dogfood.slot import SlotGuard

from .fakes import FakeRunner


def test_mini_campaign_end_to_end(tmp_path, stub_corpus):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[[chains]]\nfamily="rot13"\nslots=["gcc-O0-sym","gcc-O1-sym"]\nreps=1\n'
    )

    def mk():
        return FakeRunner(
            {
                "task_id": "rot13::gcc-O0-sym",
                "ledger": {"accepted": ["program"], "submissions": 1, "probes": 4},
            }
        )

    rc = run_campaign(
        cfg,
        runner_factory=mk,
        corpus_source=stub_corpus,
        pool_size=2,
        out_dir=tmp_path / "res",
        guards=SlotGuard(timeout_s=30),
        poll_s=0,
    )
    assert rc == 0
    md = render_report(tmp_path / "res", family="rot13", out_dir=tmp_path / "res")
    text = md.read_text()
    assert "unprimed" in text and "primed" in text
    assert "φ" in text or "phi" in text
