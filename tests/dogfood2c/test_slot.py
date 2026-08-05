import hashlib
import json

from tools.dogfood.prompt import template_hash
from tools.dogfood.runners.base import SlotSpec
from tools.dogfood.slot import SlotGuard, _driver_revision, layout_root, run_slot

from .fakes import FakeRunner, PreflightFakeRunner


class LateWriterFake(FakeRunner):
    """A real agent publishes its ledger minutes into a run — spawn() is not
    the write time. This fake publishes at the first exited() poll instead,
    and records whether a ledger already existed at spawn (residue of a
    crashed predecessor in a reused run root)."""

    def __init__(self, script):
        super().__init__(script)
        self.stale_at_spawn = None
        self._prompt = None
        self._published = False

    def spawn(self, prompt):
        self._prompt = prompt
        d = (
            self.cfg.run_root
            / ".reschema"
            / "tasks"
            / prompt.split('"')[1].replace("::", "__")
        )
        self.stale_at_spawn = (d / "ledger.json").exists()

    def exited(self):
        if not self._published:
            self._published = True
            super().spawn(self._prompt)  # the agent's life reaches disk now
        return super().exited()


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
    # wait()'s evidence tail lands in the record (guard-table promise)
    assert rec["transcript_tail"] == "fake"


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
    assert rec["transcript_tail"] == ""  # no agent ran: empty, but the key exists
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


def test_run_header_carries_canonicalizer_version(tmp_path, stub_corpus):
    # corpus comparability evidence: the mounted sidecar travels into the record
    (stub_corpus / "canonicalizer_version").write_text("2.1")
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
    assert rec["run_header"]["canonicalizer_version"] == "2.1"


def test_run_header_carries_driver_revision(tmp_path, stub_corpus):
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
    assert isinstance(rec["run_header"]["driver_revision"], str)
    assert rec["run_header"]["driver_revision"]


def test_driver_revision_is_unknown_outside_a_git_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # /tmp is no git repo: records must not crash
    assert _driver_revision() == "unknown"


def test_infra_error_record_carries_prompt_sha256(tmp_path, stub_corpus):
    # pre-preflight hash: an endpoint-dead record still pins the prompt it ran
    out = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=PreflightFakeRunner({"task_id": "rot13::gcc-O0-sym"}),
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30),
        poll_s=0,
    )
    rec = json.loads(out.read_text())
    assert rec["outcome"] == "infra-error"
    assert rec["run_header"]["prompt_sha256"] == template_hash()


def test_run_slot_wipes_stale_task_ledger_before_spawn(tmp_path, stub_corpus):
    # resume after a mid-slot driver crash: the reused run root still holds the
    # crashed agent's ledger. It must NOT launder into the fresh slot's record.
    spec = _spec()
    root = layout_root(spec, tmp_path / "runs", stub_corpus)
    task_dir = root / ".reschema/tasks" / "rot13__gcc-O0-sym"
    task_dir.mkdir(parents=True)
    (task_dir / "ledger.json").write_text(
        json.dumps({"accepted": ["program"], "submissions": 99, "probes": 99})
    )
    runner = LateWriterFake(
        {
            "task_id": "rot13::gcc-O0-sym",
            "ledger": {"accepted": ["program"], "submissions": 1, "probes": 2},
        }
    )
    out = run_slot(
        spec,
        campaign_dir=tmp_path / "runs",
        runner=runner,
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
    )
    rec = json.loads(out.read_text())
    # the FRESH agent's counters, not the crashed predecessor's inflated ones
    assert (rec["n_exp"], rec["n_sub"]) == (2, 1)
    # and the dirty "accepted" must not survive to spawn: no instant
    # pre-spawn acceptance off a ledger the fresh agent never wrote
    assert runner.stale_at_spawn is False
