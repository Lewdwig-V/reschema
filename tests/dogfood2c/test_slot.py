import hashlib
import json
import stat
import subprocess
from pathlib import Path

from tools.dogfood.prompt import template_hash
from tools.dogfood.runners.base import SlotSpec
from tools.dogfood.runners.opencode_v1 import OpenCodeV1Runner
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


def test_run_slot_accepted_kills_lingering_agent(tmp_path, stub_corpus):
    # regression: an agent that wrote an accepted ledger but NEVER exits must
    # not hang the slot in wait() — acceptance kills it before reap. Without
    # the kill this test hangs until the outer `timeout` wrapper fires.
    runner = FakeRunner(
        {
            "task_id": "rot13::gcc-O0-sym",
            "alive_after_accept": True,
            "ledger": {"accepted": ["program"], "submissions": 1, "probes": 2},
        }
    )
    out = run_slot(
        _spec(),
        campaign_dir=tmp_path / "runs",
        runner=runner,
        corpus_source=stub_corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
    )
    rec = json.loads(out.read_text())
    assert rec["outcome"] == "accepted" and rec["accepted"] is True
    assert runner._killed  # the accepted path terminates the agent


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


def test_driver_revision_anchors_to_the_checkout(tmp_path, monkeypatch):
    # a campaign launched from a foreign repo must still record OUR SHA
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git = ["git", "-C", str(foreign)]
    subprocess.run(["git", "init", "-q", str(foreign)], check=True)
    subprocess.run(
        [
            *git,
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    foreign_sha = subprocess.run(
        [*git, "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    checkout_sha = subprocess.run(
        [
            "git",
            "-C",
            str(Path(__file__).resolve().parents[2]),
            "rev-parse",
            "--short",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert foreign_sha != checkout_sha  # else the test can't disambiguate
    monkeypatch.chdir(foreign)
    assert _driver_revision() == checkout_sha


def test_driver_revision_is_unknown_outside_a_git_checkout(tmp_path):
    assert _driver_revision(tmp_path) == "unknown"


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


# --- #94: per-slot transcripts in chain-shared sandboxes ---

RECORD_KEYS = 15  # the one JSONL record shape; schema untouched by the fix


class _EchoRunner(OpenCodeV1Runner):
    """Real spawn/wait plumbing, no endpoint: a stub binary echoes its argv
    (the rendered prompt carries the task_id) and exits immediately."""

    def preflight(self, cfg):
        return {}


def _echo_binary(tmp_path):
    p = tmp_path / "stub-opencode"
    p.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def _chain_spec(idx):
    slot = f"gcc-O{idx}-sym"
    return SlotSpec(
        family="rot13",
        condition="primed",
        slot=slot,
        slot_index=idx,
        rep=1,
        task_id=f"rot13::{slot}",
    )


def _run(spec, tmp_path, corpus):
    return run_slot(
        spec,
        campaign_dir=tmp_path / "runs",
        runner=_EchoRunner(binary=_echo_binary(tmp_path)),
        corpus_source=corpus,
        guards=SlotGuard(timeout_s=30, probe_ceiling=5),
        poll_s=0,
    )


def test_chain_slots_keep_separate_transcripts(tmp_path, stub_corpus):
    # #94: the shared chain sandbox must hold ALL slots' session logs —
    # previously spawn("wb") truncated the one transcript.log per slot.
    out0 = _run(_chain_spec(0), tmp_path, stub_corpus)
    out1 = _run(_chain_spec(1), tmp_path, stub_corpus)
    rec0, rec1 = json.loads(out0.read_text()), json.loads(out1.read_text())
    assert len(rec0) == len(rec1) == RECORD_KEYS  # schema untouched

    sandbox = tmp_path / "runs" / "rot13-primed-r1" / "sandbox"
    t0 = sandbox / "transcript-rot13-primed-gcc-O0-sym-r1-s0.log"
    t1 = sandbox / "transcript-rot13-primed-gcc-O1-sym-r1-s1.log"
    assert t0.exists() and t1.exists()  # both slots preserved...
    assert "rot13::gcc-O0-sym" in t0.read_text()
    assert "rot13::gcc-O1-sym" in t1.read_text()
    # ...and distinguishable: each log holds ONLY its own slot's session
    assert "rot13::gcc-O1-sym" not in t0.read_text()
    assert "rot13::gcc-O0-sym" not in t1.read_text()
    # per-slot tails still reach the per-slot records
    assert "rot13::gcc-O0-sym" in rec0["transcript_tail"]
    assert "rot13::gcc-O1-sym" in rec1["transcript_tail"]


def test_rerun_truncates_only_its_own_transcript(tmp_path, stub_corpus):
    # #94 resume idempotence: re-running a slot must NOT resurrect/stitch a
    # stale transcript (same correctness shape as the stale-ledger wipe).
    _run(_chain_spec(0), tmp_path, stub_corpus)
    _run(_chain_spec(1), tmp_path, stub_corpus)
    sandbox = tmp_path / "runs" / "rot13-primed-r1" / "sandbox"
    t0 = sandbox / "transcript-rot13-primed-gcc-O0-sym-r1-s0.log"
    t1 = sandbox / "transcript-rot13-primed-gcc-O1-sym-r1-s1.log"
    before = t1.read_text()
    t0.write_text("STALE-SESSION-JUNK\n")

    _run(_chain_spec(0), tmp_path, stub_corpus)  # resume slot 0

    fresh = t0.read_text()
    assert "STALE-SESSION-JUNK" not in fresh  # re-truncated, not appended
    assert "rot13::gcc-O0-sym" in fresh
    assert t1.read_text() == before  # sibling slot's log untouched
