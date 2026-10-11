"""Re-grade recorded checks under the current checker: how a judge change is
audited (#112, rebuilt on Warranted check evidence in M8 S7).

For each completed check in a Warranted project's ledger, rebuild exactly what
the checker saw from the recorded evidence: the submitted candidate files, the
task's worker-visible and private files (the corpus binary among them), and
the seeds it drew, in order. Re-run the CURRENT checker of that name on them
and report whether the recorded verdict still holds. Everything but the judge
is pinned by the evidence itself, so a flip is the judge's change:

- a corpus rebuild cannot move a verdict (the binary is the recorded private
  file, not today's corpus);
- the hidden and fuzz draws replay from `seeds.json`; a checker that draws
  more seeds than were recorded is `unreplayable`, never a fresh draw;
- the image is pinned too: a check whose jobs ran another image than the
  domain's current one is `unreplayable: image changed` (toolchain drift is
  not a judge change).

Tampered evidence is refused per row: Warranted's `Ledger.lookup` re-verifies
every input and result artifact against its digest, and the candidate files
must match the submission record's digests.

Read-only: nothing is written to the project. Interface (Warranted host kit,
`warranted.host`): the ledger lives at `<project>/ledger`, checks are the
completed operations of kind `check` named `run/<run>/submission/<n>/check/<name>`,
with the channels the task layer documents (`verdict.json`, `seeds.json`,
`jobs.json`). Warranted v0.3.0 has no public re-check entry point, and
`Project` refuses to open once the domain identity changed (which is exactly
the judge change being audited): see Lewdwig-V/warranted#78.

stdout: one JSON line per check. stderr: totals.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from warranted import CheckContext, PodmanJobs
from warranted.host import CorruptArtifact, Ledger

_CHECK = re.compile(r"run/(?P<run>[^/]+)/submission/(?P<n>\d+)/check/(?P<name>[^/]+)")
_EVIDENCE = re.compile(r"observation/(?P<seq>\d+)/(?:revision/\d+/)?(?P<channel>.+)")
# The worker's notes ride beside the candidate but are not part of it: the
# task layer withholds them from checkers.
_NOTES = ("notes.json", "notes-error.txt")


class Refused(Exception):
    """The recorded evidence does not hold together; nothing is re-judged."""


class Unreplayable(Exception):
    """The evidence is intact but cannot pin everything but the judge."""


class _Replay(CheckContext):
    """A CheckContext whose draws replay the recorded seeds, in order."""

    def __init__(self, recorded: list[int], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._recorded = recorded

    def draw_seed(self) -> int:
        if len(self.seeds) == len(self._recorded):
            raise Unreplayable("the checker draws more seeds than were recorded")
        seed = self._recorded[len(self.seeds)]
        self.seeds.append(seed)
        return seed


def _json(ledger: Ledger, ref):
    return json.loads(ledger.read_artifact(ref))


def _evidence(ledger: Ledger, op, observations: dict) -> dict:
    """What the recorded check saw and decided, re-verified."""
    candidate, inputs, private, target = {}, {}, {}, None
    for name, ref in op.request.origin.inputs.items():
        m = _EVIDENCE.fullmatch(name)
        channel = m["channel"] if m else ""
        kind, _, file = channel.partition("/")
        if channel == "submission.json":
            target = (int(m["seq"]), ref)
        elif kind == "input" and file:
            inputs[file] = ledger.read_artifact(ref)
        elif kind == "private" and file:
            private[file] = ledger.read_artifact(ref)
        else:
            raise Refused(f"unexpected check input {name!r}")
    if target is None or target[0] not in observations:
        raise Refused("the check names no recorded submission")
    claimed = _json(ledger, target[1])
    for name, ref in observations[target[0]].origin.inputs.items():
        file = name.partition("/candidate/")[2]
        if claimed.get(file) != ref.digest:
            raise Refused(f"candidate file {file!r} does not match the submission")
        if file not in _NOTES:
            candidate[file] = ledger.read_artifact(ref)
    if len(claimed) != len(observations[target[0]].origin.inputs):
        raise Refused("the submission names files that were not captured")
    artifacts = op.completion.observation.artifacts
    return {
        "candidate": candidate,
        "inputs": inputs,
        "private": private,
        "verdict": _json(ledger, artifacts["verdict.json"]),
        "seeds": _json(ledger, artifacts["seeds.json"]),
        "images": {j["image"] for j in _json(ledger, artifacts["jobs.json"])},
    }


def regrade(project: Path, domain=None, *, run=None, submission=None, jobs=None):
    """One row per completed check (filtered by run ID and submission number):
    the recorded and re-graded verdict status and whether it holds."""
    if domain is None:
        from .domain import ReSchemaDomain

        domain = ReSchemaDomain()
    jobs = PodmanJobs() if jobs is None else jobs
    rows = []
    with Ledger.open(Path(project) / "ledger") as ledger:
        observations = {o.sequence: o for o in ledger.history()}
        for op in ledger.operations():
            m = _CHECK.fullmatch(op.request.origin.operation_id)
            if op.request.origin.kind != "check" or m is None or not op.completion:
                continue
            if (run is not None and m["run"] != run) or (
                submission is not None and int(m["n"]) != submission
            ):
                continue
            row = {"run": m["run"], "submission": int(m["n"]), "check": m["name"]}
            rows.append(row)
            try:
                # lookup re-verifies identity and every input and result artifact
                ev = _evidence(ledger, ledger.lookup(op.request), observations)
            except (Refused, CorruptArtifact, FileNotFoundError, ValueError) as e:
                row["refused"] = f"tampered evidence: {e}"
                continue
            row["recorded"] = ev["verdict"]["status"]
            checker = domain.checkers.get(m["name"])
            if checker is None:
                row["unreplayable"] = "no current checker of that name"
                continue
            row["checker_version"] = {
                "recorded": op.request.origin.producer_version,
                "current": checker.version,
            }
            if ev["images"] and ev["images"] != {domain.worker_image}:
                row["unreplayable"] = "image changed"
                continue
            ctx = _Replay(
                ev["seeds"], ev["candidate"], ev["inputs"], ev["private"], jobs
            )
            try:
                verdict = checker.check(ctx)  # a checker raise is a judge bug: raises
            except Unreplayable as e:
                row["unreplayable"] = str(e)
                continue
            row["regraded"] = str(verdict.status)
            row["holds"] = row["regraded"] == row["recorded"]
            if not row["holds"]:
                row["feedback"] = verdict.feedback
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reschema.regrade")
    ap.add_argument("project", type=Path, help="a Warranted project directory")
    ap.add_argument("--run", help="only this run")
    ap.add_argument("--submission", type=int, help="only this submission number")
    a = ap.parse_args(argv)
    rows = regrade(a.project, run=a.run, submission=a.submission)
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    totals = Counter(
        "refused"
        if "refused" in r
        else "unreplayable"
        if "unreplayable" in r
        else "holds"
        if r["holds"]
        else "flipped"
        for r in rows
    )
    print(json.dumps(dict(totals), sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
