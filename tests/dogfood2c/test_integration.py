from tools.dogfood.driver import run_campaign
from tools.dogfood.slot import SlotGuard

from .fakes import FakeRunner


class ScriptedFake(FakeRunner):
    """Ledger derived from the slot's run-root name: primed slots reuse the
    shared chain memory (0 probes); the unprimed trajectory ASCENDS (8 probes
    at O0, 4 at O1) — one fixture hosting a non-flat trajectory AND φ = 1.0."""

    def prepare(self, cfg):
        super().prepare(cfg)
        name = str(cfg.run_root)
        probes = 0 if "-primed-" in name else (8 if "gcc-O0" in name else 4)
        self.script = {
            "ledger": {"accepted": ["program"], "submissions": 1, "probes": probes}
        }


def test_mini_campaign_end_to_end(tmp_path, stub_corpus):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[[chains]]\nfamily="rot13"\nslots=["gcc-O0-sym","gcc-O1-sym"]\nreps=1\n'
    )

    def mk():
        return ScriptedFake({})

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
    md = tmp_path / "res" / "report-rot13.md"  # rendered by the driver itself
    assert md.exists()
    text = md.read_text()
    assert "unprimed" in text and "primed" in text
    assert "φ" in text or "phi" in text
    # all-accept + primed reuse (probes=0 -> E=1) recovers full headroom
    assert "φ median: 1.0" in text
    # non-flat unprimed trajectory -> the deltas section carries real rows
    assert "- slot 1:" in text
    # thin-population base visible beside φ: one rep fed it
    assert "1 rep" in text
