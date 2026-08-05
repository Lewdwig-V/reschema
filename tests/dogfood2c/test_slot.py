import hashlib
import json

from tools.dogfood.runners.base import SlotSpec
from tools.dogfood.slot import SlotGuard, layout_root, run_slot

from .fakes import FakeRunner, PreflightFakeRunner


def _spec(cond="unprimed", idx=0):
    return SlotSpec(
        family="rot13",
        condition=cond,
        slot="gcc-O0-sym",
        slot_index=idx,
        rep=1,
        task_id="rot13::gcc-O0-sym",
    )


def test_unprimed_layout_is_cold_and_copied_corpus(tmp_path, stub_corpus):
    root = layout_root(_spec(), tmp_path / "runs", stub_corpus)
    assert (root / ".reschema/corpus/manifest.json").exists()
    assert not (root / ".reschema/memory").exists()  # cold at slot open
    # manifest binary paths resolve in the source corpus root — binaries are
    # dead weight in the mount and must not be copied
    assert not (root / ".reschema/corpus/rot13/fake-binary").exists()


def test_primed_chain_shares_memory_root(tmp_path, stub_corpus):
    a = layout_root(_spec("primed", 0), tmp_path / "runs", stub_corpus)
    b = layout_root(_spec("primed", 1), tmp_path / "runs", stub_corpus)
    assert a == b  # same root: slot 0's verified_fact must be visible at slot 1


def test_run_slot_accepted_emits_jsonl(tmp_path, stub_corpus):
    script = {
        "task_id": "rot13::gcc-O0-sym",
        "ledger": {"accepted": ["program"], "submissions": 1, "probes": 3},
    }
    out = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=FakeRunner(script),
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
    )
    rec = json.loads(out.read_text())
    assert rec["outcome"] == "accepted" and rec["n_exp"] == 3
    assert rec["E"] > 0 and rec["slot_id"].endswith("r1")


def test_run_slot_timeout_is_typed_abort(tmp_path, stub_corpus):
    out = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=FakeRunner({"task_id": "rot13::gcc-O0-sym", "ledger": None}),
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=1, probe_ceiling=99),
        poll_s=0,
    )
    assert json.loads(out.read_text())["outcome"] == "aborted: timeout"


def test_run_slot_probe_ceiling_is_typed_abort(tmp_path, stub_corpus):
    script = {
        "task_id": "rot13::gcc-O0-sym",
        "alive": True,
        "ledger": {"accepted": [], "submissions": 0, "probes": 99},
    }
    out = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=FakeRunner(script),
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
    )
    assert json.loads(out.read_text())["outcome"] == "aborted: probe-ceiling"


def test_run_slot_natural_exit_without_acceptance_is_typed_abort(tmp_path, stub_corpus):
    script = {
        "task_id": "rot13::gcc-O0-sym",
        "ledger": {"accepted": [], "submissions": 0, "probes": 1},
    }
    out = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=FakeRunner(script),
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
    )
    assert json.loads(out.read_text())["outcome"] == "aborted: agent-exit"


def test_run_slot_preflight_failure_is_typed_infra_error(tmp_path, stub_corpus):
    normal = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=FakeRunner(
            {
                "task_id": "rot13::gcc-O0-sym",
                "ledger": {"accepted": ["program"], "submissions": 1, "probes": 1},
            }
        ),
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
    )
    out = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=PreflightFakeRunner({"task_id": "rot13::gcc-O0-sym"}),
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
    )
    rec = json.loads(out.read_text())
    assert rec["outcome"] == "infra-error"
    assert rec["abort_reason"] == "endpoint dead"
    # key parity with a terminal record — pinned by the one record builder
    assert set(rec) == set(json.loads(normal.read_text()))


def test_run_header_carries_manifest_sha256(tmp_path, stub_corpus):
    # protocol §5: every live result pins the corpus manifest it ran against
    out = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=FakeRunner(
            {
                "ledger": {"accepted": ["program"], "submissions": 1, "probes": 1},
            }
        ),
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30),
        poll_s=0,
    )
    rec = json.loads(out.read_text())
    assert (
        rec["run_header"]["manifest_sha256"]
        == hashlib.sha256((stub_corpus / "manifest.json").read_bytes()).hexdigest()
    )
