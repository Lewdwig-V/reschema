"""M8 S7: re-grading recorded Warranted checks (src/reschema/regrade.py).

A run is recorded through Warranted's Project with a scripted worker (the
test_tasks harness), then re-graded from the ledger alone. Stub checkers make
the default-suite cases job-free; the real checker's re-grade is golden."""

import hashlib
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from test_tasks import CONFIG, GOLDEN, Model, NoJobs, project, submit
from warranted import JobResult, RunOutcome, Verdict, VerdictStatus
from warranted.host import Ledger

from reschema import regrade as rg
from reschema import tasks
from reschema.checks.function_fuzz import FunctionFuzz
from reschema.domain import ReSchemaDomain

SLOT = "rot13::gcc-O2-sym"


class Exact:
    """Passes exactly `want`; draws one seed (so replay is exercised)."""

    version = "exact"
    isolated = True
    want = b"ok"

    def check(self, ctx):
        seed = ctx.draw_seed()
        ok = ctx.candidate["model.c"] == self.want
        status = VerdictStatus.PASSED if ok else VerdictStatus.REJECTED
        return Verdict(status, {"accepted": ok, "seed": seed})


class Stricter(Exact):
    version = "stricter"
    want = b"ok\n"  # the judge change: the recorded accept no longer passes


class Parity(Exact):
    """The verdict is the drawn seed's parity: it holds on re-grade only if
    the recorded seed is replayed, never a fresh draw."""

    def check(self, ctx):
        ok = ctx.draw_seed() % 2 == 0
        return Verdict(VerdictStatus.PASSED if ok else VerdictStatus.REJECTED, {})


class TwoDraws(Exact):
    def check(self, ctx):
        ctx.draw_seed()
        ctx.draw_seed()
        raise AssertionError("unreachable: only one seed was recorded")


class Jobbing(Exact):
    """Runs one job on a pinned image other than the domain's."""

    def check(self, ctx):
        ctx.run_job("a" * 64, ["true"], {})
        return super().check(ctx)


class FakeJobs:
    identity = "fake-jobs"

    def run(self, *args, **kwargs):
        return JobResult(0, b"", b"")


def domain(checker):
    class D(ReSchemaDomain):
        checkers = MappingProxyType({"program-replay": checker})

    return D()


@pytest.fixture(scope="module")
def meta(built_corpus):
    from reschema.corpus.generate import slot

    return slot(SLOT)


def recorded(tmp_path, meta, checker, sources=(b"ok",), jobs=None):
    """One program-task run per source, each one submission, under `checker`."""
    proj, steps = project(tmp_path, [], domain(checker), jobs or NoJobs())
    task = tasks.program_task(meta, submissions=1, check_budget=1)
    for src in sources:
        steps.plan = [[submit(**{"model.c": src.decode(), "cases.json": "[]"})]]
        proj.start(task, CONFIG, Model())
    return tmp_path / "project"


def test_a_recorded_accept_regrades_as_accept(tmp_path, meta):
    root = recorded(tmp_path, meta, Exact())
    [row] = rg.regrade(root, domain(Exact()))
    assert (row["recorded"], row["regraded"], row["holds"]) == ("passed",) * 2 + (True,)
    assert row["submission"] == 1 and row["check"] == "program-replay"


def test_a_judge_change_flips_it(tmp_path, meta):
    root = recorded(tmp_path, meta, Exact())
    [row] = rg.regrade(root, domain(Stricter()))
    assert (row["recorded"], row["regraded"], row["holds"]) == (
        "passed",
        "rejected",
        False,
    )
    assert row["checker_version"] == {"recorded": "exact", "current": "stricter"}
    assert row["feedback"]["accepted"] is False


def test_recorded_seeds_are_replayed(tmp_path, meta):
    # Eight runs whose verdicts are their seeds' parity: with fresh draws a
    # re-grade would flip about half of them.
    root = recorded(tmp_path, meta, Parity(), sources=[b"x%d" % i for i in range(8)])
    rows = rg.regrade(root, domain(Parity()))
    assert len(rows) == 8 and all(r["holds"] for r in rows), rows


@pytest.mark.parametrize(
    ("checker", "jobs", "reason"),
    [
        (TwoDraws(), None, "draws more seeds"),
        (Jobbing(), FakeJobs(), "image changed"),
    ],
)
def test_unpinnable_checks_are_unreplayable_not_flips(
    tmp_path, meta, checker, jobs, reason
):
    root = recorded(tmp_path, meta, Jobbing() if jobs else Exact(), jobs=jobs)
    [row] = rg.regrade(root, domain(checker), jobs=jobs)
    assert reason in row["unreplayable"] and "holds" not in row, row


def test_an_unknown_checker_is_unreplayable(tmp_path, meta):
    root = recorded(tmp_path, meta, Exact())
    d = domain(Exact())
    d.checkers = MappingProxyType({})
    [row] = rg.regrade(root, d)
    assert row["unreplayable"] == "no current checker of that name"


def _artifact(root: Path, digest: str) -> Path:
    return root / "ledger" / "artifacts" / "sha256" / digest


def _check_artifact(root: Path, channel: str) -> str:
    with Ledger.open(root / "ledger") as ledger:
        [op] = [o for o in ledger.operations() if o.request.origin.kind == "check"]
        return op.completion.observation.artifacts[channel].digest


@pytest.mark.parametrize("what", ["candidate", "seeds.json", "private binary"])
def test_tampered_evidence_is_refused(tmp_path, meta, what):
    root = recorded(tmp_path, meta, Parity())
    if what == "candidate":
        digest = hashlib.sha256(b"ok").hexdigest()
        forged = b"forged"
    elif what == "seeds.json":
        digest = _check_artifact(root, "seeds.json")
        forged = b"[0]"  # the seed that makes Parity pass
    else:
        digest = hashlib.sha256(Path(meta["binary"]).read_bytes()).hexdigest()
        forged = b"\x7fELF"
    path = _artifact(root, digest)
    path.chmod(0o644)
    path.write_bytes(forged)
    [row] = rg.regrade(root, domain(Parity()))
    assert row["refused"].startswith("tampered evidence"), row
    assert "holds" not in row


def test_filters_and_cli(tmp_path, meta, capsys):
    root = recorded(tmp_path, meta, Exact(), sources=[b"ok", b"no"])
    with Ledger.open(root / "ledger") as ledger:
        runs = sorted(
            {o.request.origin.operation_id.split("/")[1] for o in ledger.operations()
             if o.request.origin.kind == "check"}
        )  # fmt: skip
    assert len(rg.regrade(root, domain(Exact()), run=runs[0])) == 1
    assert rg.regrade(root, domain(Exact()), submission=2) == []

    # the CLI re-grades under the real domain; with both candidates tampered
    # it refuses both rows without running any checker
    for src in (b"ok", b"no"):
        path = _artifact(root, hashlib.sha256(src).hexdigest())
        path.chmod(0o644)
        path.write_bytes(b"forged")
    assert rg.main([str(root)]) == 0
    out, err = capsys.readouterr()
    rows = [json.loads(line) for line in out.splitlines()]
    assert len(rows) == 2 and all("refused" in r for r in rows)
    assert json.loads(err) == {"refused": 2}


@pytest.mark.golden
def test_the_real_checker_regrades_its_own_accept(tmp_path, meta):
    case = GOLDEN["rot13-o2-rot13char-s1-n8"]
    files = {"model.c": case["c_source"], "params.json": json.dumps(case["params"])}
    proj, _ = project(tmp_path, [[submit(**files)]])
    run = proj.start(tasks.function_task(meta, "rot13_char"), CONFIG, Model())
    assert run.outcome is RunOutcome.ACCEPTED
    [row] = rg.regrade(tmp_path / "project")
    assert row["holds"] is True and row["recorded"] == "passed", row

    class Never(FunctionFuzz):
        """A judge change on the real checker: it still runs, never accepts."""

        def check(self, ctx):
            v = super().check(ctx)
            return Verdict(VerdictStatus.REJECTED, {"was": str(v.status)})

    class D(ReSchemaDomain):
        checkers = MappingProxyType({"function-fuzz": Never()})

    [row] = rg.regrade(tmp_path / "project", D())
    assert row["holds"] is False and row["feedback"] == {"was": "passed"}, row
