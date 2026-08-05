"""One live-agent run of one corpus slot (spec §slot.py)."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .measure import slot_efficiency
from .prompt import render, template_hash
from .runners.base import AgentRunner, RunnerConfig, SlotSpec

DEFAULT_POLL_S = 30


@dataclass
class SlotGuard:
    timeout_s: int = 2700  # 45 min
    probe_ceiling: int = 30


def layout_root(spec: SlotSpec, runs_dir: Path, corpus_source: Path) -> Path:
    """Primed chains share one root across slots; unprimed gets a fresh root
    per slot — memory-cold-by-filesystem, the CI isolation invariant."""
    chain = (
        f"{spec.family}-primed-r{spec.rep}"
        if spec.condition == "primed"
        else spec.slot_id
    )
    root = runs_dir / chain
    corp = root / ".reschema/corpus"
    if not corp.exists():
        corp.mkdir(parents=True)
        # Manifest "binary" paths are baked at corpus build time (generate.py)
        # and resolve in the ORIGINAL corpus root, never in this mount — the
        # mount is manifest(+sidecar) only; binaries would be dead weight.
        shutil.copy2(corpus_source / "manifest.json", corp)
        sidecar = corpus_source / "canonicalizer_version"
        if sidecar.exists():
            shutil.copy2(sidecar, corp)
    (root / ".reschema/tasks").mkdir(parents=True, exist_ok=True)
    return root


def _read_ledger(root: Path, task_id: str) -> dict:
    p = root / ".reschema/tasks" / task_id.replace("::", "__") / "ledger.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _record(
    spec: SlotSpec,
    *,
    outcome: str,
    abort_reason: str | None,
    e: float,
    n_exp: int,
    n_sub: int,
    accepted: bool,
    wall_s: float,
    run_header: dict,
    transcript_tail: str = "",
) -> dict:
    """The ONE 15-key JSONL record shape — both the terminal path and the
    preflight infra-error path build here, so key parity holds by construction.
    transcript_tail is "" when no agent ran (infra-error)."""
    return {
        "slot_id": spec.slot_id,
        "family": spec.family,
        "condition": spec.condition,
        "slot": spec.slot,
        "slot_index": spec.slot_index,
        "rep": spec.rep,
        "outcome": outcome,
        "abort_reason": abort_reason,
        "E": e,
        "n_exp": n_exp,
        "n_sub": n_sub,
        "accepted": accepted,
        "wall_s": wall_s,
        "run_header": run_header,
        "transcript_tail": transcript_tail,
    }


def run_slot(
    spec: SlotSpec,
    *,
    campaign_dir: Path,
    runner: AgentRunner,
    corpus_source: Path,
    guards: SlotGuard | None = None,
    poll_s: int | None = None,
    run_header: dict | None = None,
) -> Path:
    guards = guards or SlotGuard()
    poll = poll_s if poll_s is not None else DEFAULT_POLL_S
    root = layout_root(spec, campaign_dir, corpus_source)
    # Resume honesty: layout_root REUSES roots, so a driver killed mid-slot
    # leaves the crashed agent's ledger behind — the poll loop would launder a
    # stale "accepted"/inflated counters into the fresh run's record. Wipe THIS
    # task's dir before the agent spawns; chain memory and sibling slot
    # ledgers are untouched.
    shutil.rmtree(
        root / ".reschema/tasks" / spec.task_id.replace("::", "__"),
        ignore_errors=True,
    )
    out = campaign_dir.parent / "results" / f"{spec.result_stem}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    run_header = run_header or {}
    cfg = RunnerConfig(
        model=run_header.get("model", "unknown"),
        endpoint=run_header.get("endpoint"),
        sandbox=root / "sandbox",
        run_root=root,
    )
    cfg.sandbox.mkdir(parents=True, exist_ok=True)
    # corpus identity is comparability evidence (protocol §5) — merge before
    # preflight so even an infra-error record carries it
    run_header = {
        **run_header,
        "manifest_sha256": hashlib.sha256(
            (root / ".reschema/corpus/manifest.json").read_bytes()
        ).hexdigest(),
    }
    preflight = getattr(runner, "preflight", None)
    if preflight is not None:
        try:
            run_header = {**run_header, **preflight(cfg)}
        except RuntimeError as e:
            out.write_text(
                json.dumps(
                    _record(
                        spec,
                        outcome="infra-error",
                        abort_reason=str(e),
                        e=0.0,
                        n_exp=0,
                        n_sub=0,
                        accepted=False,
                        wall_s=0.0,
                        run_header=run_header,
                    )
                )
                + "\n"
            )
            return out
    runner.prepare(cfg)
    started = time.monotonic()
    runner.spawn(render(spec.task_id))

    outcome, abort = "aborted: agent-exit", None
    while True:
        led = _read_ledger(root, spec.task_id)
        probes = led.get("probes", 0)
        print(
            f"[{spec.result_stem}] {time.monotonic() - started:.0f}s probes={probes}",
            flush=True,
        )  # heartbeat
        if "program" in led.get("accepted", []):
            outcome = "accepted"
            break
        if runner.exited():  # natural exit without acceptance: typed below
            outcome = "aborted: agent-exit"
            break
        if time.monotonic() - started > guards.timeout_s:
            runner.kill()
            outcome, abort = "aborted: timeout", "wall-clock guard"
            break
        if probes > guards.probe_ceiling:
            runner.kill()
            outcome, abort = "aborted: probe-ceiling", f"probes>{guards.probe_ceiling}"
            break
        time.sleep(poll)
    res = runner.wait()  # after kill() or natural exit; must return promptly
    led = _read_ledger(root, spec.task_id)
    accepted = "program" in led.get("accepted", [])
    if accepted:  # evidence of the aborted poll already lives in agent_exit
        outcome, abort = "accepted", None
    subs, probes = led.get("submissions", 0), led.get("probes", 0)
    rec = _record(
        spec,
        outcome=outcome,
        abort_reason=abort,
        e=slot_efficiency(accepted, probes, subs),
        n_exp=probes,
        n_sub=subs,
        accepted=accepted,
        wall_s=round(time.monotonic() - started, 1),
        run_header={
            **run_header,
            "prompt_sha256": template_hash(),
            "agent_exit": res.exit_kind,
        },
        transcript_tail=res.transcript_tail,
    )
    out.write_text(json.dumps(rec) + "\n")
    return out
