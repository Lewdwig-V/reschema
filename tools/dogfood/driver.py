"""Campaign orchestration: TOML plan -> slot specs -> bounded pool -> JSONL.

Primed chains ({family, rep} O0->O1->O2) run SEQUENTIALLY inside one worker
task, sharing the memory root layout_root gives them: a later slot reads
slot 0's auto-written verified_fact. A chain slot that is not accepted aborts
the rest of the chain as `aborted: priming-failed`, WITHOUT an agent run —
priming presupposes acceptance, so running them cold would silently bias the
primed arm. Unprimed slots stay independent.

Resume honesty: a slot is skipped ONLY when its existing record carries a
budget-consuming outcome (accepted / aborted:*). `infra-error` records are
RETRIED — an endpoint outage must not silently shrink the paired-runs floor.
infra-error records themselves are written faithfully here; EXCLUDING them
from downstream statistics is the Task-7 render contract, not this module's.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .runners.base import AgentRunner, SlotSpec
from .slot import SlotGuard, run_slot

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
    doc = tomllib.loads(Path(cfg_path).read_text())
    specs = []
    for chain in doc["chains"]:
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
    primed_chains: dict[tuple[str, int], list[SlotSpec]] = {}
    singles: list[SlotSpec] = []
    for s in expand_campaign(cfg_path):
        if s.condition == "primed":
            primed_chains.setdefault((s.family, s.rep), []).append(s)
        else:
            singles.append(s)

    run_header = {
        "model": os.environ.get("RESCHEMA_2C_MODEL", "gemma4"),
        "endpoint": os.environ.get("RESCHEMA_2C_ENDPOINT"),
    }
    infra_streak = [0]

    def one(spec: SlotSpec) -> Path:
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
            infra_streak[0] += 1
        else:
            infra_streak[0] = 0
        if infra_streak[0] >= INFRA_STREAK_ABORT:
            raise RuntimeError("endpoint infra-error streak: aborting campaign")
        return final

    def chain(chain_specs: list[SlotSpec]) -> None:
        """One primed chain, sequentially: later slots read earlier memory."""
        for i, spec in enumerate(chain_specs):
            rec = json.loads(one(spec).read_text())
            if rec["outcome"] != "accepted":
                for later in chain_specs[i + 1 :]:
                    lp = out_dir / f"{later.result_stem}.jsonl"
                    if _completed(lp):
                        continue
                    lp.write_text(
                        json.dumps(
                            {
                                "slot_id": later.slot_id,
                                "family": later.family,
                                "condition": later.condition,
                                "slot": later.slot,
                                "slot_index": later.slot_index,
                                "rep": later.rep,
                                "outcome": "aborted: priming-failed",
                                "abort_reason": (
                                    f"chain slot {spec.slot} not accepted"
                                ),
                                "E": 0.0,
                                "n_exp": 0,
                                "n_sub": 0,
                                "accepted": False,
                                "wall_s": 0.0,
                                "run_header": run_header,
                            }
                        )
                        + "\n"
                    )
                break

    def work(job: list[SlotSpec]) -> None:
        if len(job) > 1:
            chain(job)
        else:
            one(job[0])

    jobs = list(primed_chains.values()) + [[s] for s in singles]
    with ThreadPoolExecutor(max_workers=pool_size) as ex:
        list(ex.map(work, jobs))
    return 0
