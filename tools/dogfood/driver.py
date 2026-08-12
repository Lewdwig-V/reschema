"""Campaign orchestration: TOML plan -> slot specs -> bounded pool -> JSONL.

Primed chains ({family, rep} O0->O1->O2) run SEQUENTIALLY inside one worker
task, sharing the memory root layout_root gives them: a later slot reads
slot 0's auto-written verified_fact. A chain slot that is not accepted aborts
the rest of the chain as `aborted: priming-failed`, WITHOUT an agent run —
priming presupposes acceptance, so running them cold would silently bias the
primed arm. An `infra-error` chain slot (transient endpoint flap, NOT a
priming rejection) instead leaves the rest of the chain UNRECORDED — the
shared root persists under runs/, so a resumed campaign retries those slots
rather than truncating the primed arm. Unprimed slots stay independent.

Resume honesty: a slot is skipped ONLY when its existing record carries a
budget-consuming outcome (accepted / aborted:*). `infra-error` records are
RETRIED — an endpoint outage must not silently shrink the paired-runs floor.
infra-error records themselves are written faithfully here; EXCLUDING them
from downstream statistics is the Task-7 render contract, not this module's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .measure import render_report
from .prompt import template_hash
from .runners.base import AgentRunner, SlotSpec
from .runners.opencode_v1 import OpenCodeV1Runner
from .slot import SlotGuard, _driver_revision, run_slot

INFRA_STREAK_ABORT = 3
BUDGET_CONSUMING = (
    "accepted",
    "aborted: timeout",
    "aborted: probe-ceiling",
    "aborted: agent-exit",
    "aborted: priming-failed",
)


def expand_campaign(cfg_path: Path) -> list[SlotSpec]:
    """TOML `[[chains]]` -> per-slot specs: reps x {unprimed, primed} x slots."""
    specs = []
    try:
        doc = tomllib.loads(Path(cfg_path).read_text())
        for chain in doc["chains"]:
            if not isinstance(chain["slots"], list):
                raise TypeError("expected `slots = [...]` as a list")
            for rep in range(1, chain.get("reps", 5) + 1):
                for cond in ("unprimed", "primed"):
                    for i, slot in enumerate(chain["slots"]):
                        specs.append(
                            SlotSpec(
                                family=chain["family"],
                                condition=cond,
                                slot=slot,
                                slot_index=i,
                                rep=rep,
                                task_id=f"{chain['family']}::{slot}",
                            )
                        )
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as e:
        # hand-edited campaign files must fail readably, not as a traceback
        raise ValueError(f"campaign TOML: malformed ({e})") from e
    stems = [s.result_stem for s in specs]
    dups = sorted({st for st in stems if stems.count(st) > 1})
    if dups:  # e.g. the same family listed twice — records would clobber
        raise ValueError(f"duplicate result stems in campaign: {dups}")
    return specs


def _completed(path: Path) -> bool:
    """Resume skips budget-consuming outcomes ONLY — infra-error retries."""
    if not path.exists():
        return False
    return json.loads(path.read_text().splitlines()[0]).get("outcome") in (
        BUDGET_CONSUMING
    )


def run_campaign(
    cfg_path: Path,
    *,
    runner_factory: Callable[[], AgentRunner],
    corpus_source: Path,
    pool_size: int = 4,
    out_dir: Path,
    guards: SlotGuard | None = None,
    poll_s: int | None = None,
) -> int:
    """Run a TOML campaign. Records land flat in out_dir; returns 0.

    Chain roots and staging live under out_dir/runs (run_slot owns the
    staging path layout); each finished record is moved flat into out_dir,
    which is the resume predicate's single source of truth.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = expand_campaign(cfg_path)
    families = sorted({s.family for s in specs})
    primed_chains: dict[tuple[str, int], list[SlotSpec]] = {}
    singles: list[SlotSpec] = []
    for s in specs:
        if s.condition == "primed":
            primed_chains.setdefault((s.family, s.rep), []).append(s)
        else:
            singles.append(s)

    run_header = {
        "model": os.environ.get("RESCHEMA_2C_MODEL", "gemma4"),
        "endpoint": os.environ.get("RESCHEMA_2C_ENDPOINT"),
        # corpus identity + prompt + driver revision must reach even the
        # SYNTHETIC priming-failed records (run_slot re-derives the same
        # values from its mounted copies for real slot records)
        "manifest_sha256": hashlib.sha256(
            (Path(corpus_source) / "manifest.json").read_bytes()
        ).hexdigest(),
        "prompt_sha256": template_hash(),
        "driver_revision": _driver_revision(),
    }
    sidecar = Path(corpus_source) / "canonicalizer_version"
    if sidecar.exists():
        run_header["canonicalizer_version"] = sidecar.read_text()
    infra_streak = 0

    def one(spec: SlotSpec) -> Path:
        nonlocal infra_streak
        final = out_dir / f"{spec.result_stem}.jsonl"
        if _completed(final):
            return final
        staged = run_slot(
            spec,
            campaign_dir=out_dir / "runs",
            runner=runner_factory(),
            corpus_source=corpus_source,
            guards=guards,
            poll_s=poll_s,
            run_header=run_header,
        )
        # run_slot pins output to campaign_dir.parent/results; out_dir owns
        # the flat record the resume predicate reads (atomic same-fs rename)
        staged.replace(final)
        # ponytail: streak counter is pool-shared and racy; an interleaved
        # success only makes the abort LESS eager, the safe direction
        if json.loads(final.read_text())["outcome"] == "infra-error":
            infra_streak += 1
        else:
            infra_streak = 0
        if infra_streak >= INFRA_STREAK_ABORT:
            raise RuntimeError("endpoint infra-error streak: aborting campaign")
        return final

    def store_flat(stem: str, record: dict) -> None:
        """Same atomicity as run_slot's path: stage beside the runs, rename."""
        stage = out_dir / "results" / f"{stem}.jsonl"
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_text(json.dumps(record) + "\n")
        stage.replace(out_dir / f"{stem}.jsonl")

    def chain(chain_specs: list[SlotSpec]) -> None:
        """One primed chain, sequentially: later slots read earlier memory."""
        for i, spec in enumerate(chain_specs):
            rec = json.loads(one(spec).read_text())
            outcome = rec["outcome"]
            if outcome == "accepted":
                continue
            if outcome == "infra-error":
                # transient flap, not a priming rejection: leave the rest of
                # the chain UNRECORDED (shared root persists under runs/) so
                # resume retries instead of truncating the primed arm
                return
            for later in chain_specs[i + 1 :]:
                if _completed(out_dir / f"{later.result_stem}.jsonl"):
                    continue
                store_flat(
                    later.result_stem,
                    {
                        "slot_id": later.slot_id,
                        "family": later.family,
                        "condition": later.condition,
                        "slot": later.slot,
                        "slot_index": later.slot_index,
                        "rep": later.rep,
                        "outcome": "aborted: priming-failed",
                        "abort_reason": (
                            f"chain slot {spec.slot} not accepted: {outcome}"
                        ),
                        "E": 0.0,
                        "n_exp": 0,
                        "n_sub": 0,
                        "accepted": False,
                        "wall_s": 0.0,
                        "run_header": run_header,
                        "transcript_tail": "",  # no agent ran; key parity holds
                    },
                )
            return

    def work(job: list[SlotSpec]) -> None:
        if len(job) > 1:
            chain(job)
        else:
            one(job[0])

    jobs = list(primed_chains.values()) + [[s] for s in singles]
    try:
        with ThreadPoolExecutor(max_workers=pool_size) as ex:
            list(ex.map(work, jobs))
    finally:
        # "campaign abort, report so far": render whatever flat records exist
        # — on success AND on streak-abort, where the report IS the abort
        # evidence. One report per family: the floor campaign has two.
        for fam in families:
            render_report(out_dir, family=fam, out_dir=out_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Human entry point for the smoke/floor campaigns — real opencode runner,
    real repo corpus. Tests drive run_campaign directly; this is dispatch-only."""
    ap = argparse.ArgumentParser(prog="python -m tools.dogfood.driver")
    ap.add_argument("campaign", type=Path, help="TOML campaign plan")
    ap.add_argument(
        "--out",
        type=Path,
        required=True,
        help="results dir — keep it INSIDE the repo (see AGENTS.md §2C smoke)",
    )
    ap.add_argument("--pool", type=int, default=4)
    args = ap.parse_args(argv)
    corpus = Path(".reschema/corpus")
    if not (corpus / "manifest.json").exists():
        raise SystemExit(
            "no corpus manifest at .reschema/corpus/manifest.json — run "
            "`uv run python -m reschema.corpus.generate` first"
        )
    if not os.environ.get("RESCHEMA_2C_ENDPOINT"):
        raise SystemExit(
            "RESCHEMA_2C_ENDPOINT is not set — point it at an OpenAI-compatible "
            "base URL (preflight checks /models and /chat/completions)"
        )
    rc = run_campaign(
        args.campaign,
        runner_factory=OpenCodeV1Runner,
        corpus_source=corpus,
        pool_size=args.pool,
        out_dir=args.out,
    )
    for md in sorted(args.out.glob("report-*.md")):
        print(f"report: {md}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
