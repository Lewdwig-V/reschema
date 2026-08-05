"""Statistics over slot JSONL records. The only place numbers are computed;
slot/driver code stays dumb."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable
from pathlib import Path

from reschema.engine import E_ALPHA, E_BETA

FLAT_EPS = 1e-3  # "materially non-flat" threshold (protocol §5)


def slot_efficiency(accepted: bool, probes: int, subs: int) -> float:
    """E = accepted * exp(-(alpha*max(0,probes-1) + beta*max(0,subs-1))) — engine's formula."""
    return (
        math.exp(-(E_ALPHA * max(0, probes - 1) + E_BETA * max(0, subs - 1)))
        if accepted
        else 0.0
    )


def _e_by_slot(records: Iterable[dict]) -> dict[int, float]:
    """Median efficiency per slot; many reps of one slot aggregate here.
    Rejected slots count as E=0.0 — dropping them would be survivor bias."""
    by_slot: dict[int, list[float]] = {}
    for r in records:
        by_slot.setdefault(r["slot_index"], []).append(
            slot_efficiency(r["accepted"], r["n_exp"], r["n_sub"])
        )
    return {k: statistics.median(v) for k, v in by_slot.items()}


def _phis(up_e: dict[int, float], pr_e: dict[int, float]) -> list[float]:
    """Per-slot headroom-recovery fractions over later slots; [] without a
    usable slot-0 unprimed baseline (missing base0 or zero headroom)."""
    base0 = up_e.get(0)
    if base0 is None:
        return []
    headroom = 1.0 - base0
    if headroom <= 0:
        return []
    return [(pr_e[s] - base0) / headroom for s in sorted(pr_e) if s != 0]


def phi_family(records: list[dict]) -> dict:
    """Median headroom recovery over later slots vs slot-0 unprimed baseline.

    Records for ONE family; the caller groups. With a "rep" field present, φ
    is computed per rep (median over the rep's later slots) and phi_median /
    phi_iqr are the median and IQR ACROSS reps — the protocol's spread measure
    over paired runs. Without "rep", phi_median pools all later slots and
    phi_iqr is None. deltas are always populated: they are the fallback
    evidence when φ is uninterpretable (no base0 / zero headroom).
    """
    up_e = _e_by_slot(r for r in records if r["condition"] == "unprimed")
    pr_e = _e_by_slot(r for r in records if r["condition"] == "primed")

    deltas = []
    for slot in sorted(pr_e):
        if slot == 0:
            continue
        e, u = pr_e[slot], up_e.get(slot)
        deltas.append(
            {
                "slot_index": slot,
                "primed_e": e,
                "unprimed_e": u,
                "delta": e - u if u is not None else None,
            }
        )

    reps = sorted({r["rep"] for r in records if "rep" in r})
    phi_median = phi_iqr = None
    if reps:
        rep_phis = []
        for rep in reps:
            sub = [r for r in records if r.get("rep") == rep]
            vals = _phis(
                _e_by_slot(r for r in sub if r["condition"] == "unprimed"),
                _e_by_slot(r for r in sub if r["condition"] == "primed"),
            )
            if vals:
                rep_phis.append(statistics.median(vals))
        if rep_phis:
            phi_median = statistics.median(rep_phis)
            if len(rep_phis) >= 2:
                quartiles = statistics.quantiles(rep_phis, n=4)
                phi_iqr = quartiles[2] - quartiles[0]
    else:
        pooled = _phis(up_e, pr_e)
        if pooled:
            phi_median = statistics.median(pooled)

    flat = bool(up_e) and (max(up_e.values()) - min(up_e.values())) < FLAT_EPS
    return {
        "phi_median": phi_median,
        "phi_iqr": phi_iqr,
        "unprimed_flat": flat,
        "unprimed_traj": [up_e[k] for k in sorted(up_e)],
        "deltas": deltas,
        "n_deltas": len(deltas),
    }


def _load(results_dir: Path, family: str) -> list[dict]:
    return [
        json.loads(p.read_text().splitlines()[0])
        for p in sorted(results_dir.glob(f"{family}-*.jsonl"))
    ]


def render_report(results_dir: Path, *, family: str, out_dir: Path) -> Path:
    """Markdown summary of one family's campaign records.

    infra-error records are EXCLUDED from every statistic (adapter failure is
    not agent failure) but SHOWN in the counts line as evidence; a φ computed
    on a thin population stays visibly thin — None renders as None, never a
    dressed-up number.
    """
    recs = _load(results_dir, family)
    measured = [r for r in recs if r.get("outcome") != "infra-error"]
    accepted = [r for r in measured if r.get("accepted")]
    aborted = [r for r in measured if r.get("outcome", "").startswith("aborted")]
    infra = [r for r in recs if r.get("outcome") == "infra-error"]
    stats = phi_family(measured)
    lines = [
        f"# 2C live-agent campaign — {family}",
        "",
        (
            f"- slots: {len(recs)} total, {len(accepted)} accepted, "
            f"{len(aborted)} aborted, {len(infra)} infra-error (excluded from stats)"
        ),
        f"- φ median: {stats['phi_median']}  IQR: {stats['phi_iqr']}",
        (
            f"- unprimed trajectory: {stats['unprimed_traj']} "
            f"(flat: {stats['unprimed_flat']})"
        ),
        "",
        (
            "## per-slot deltas"
            if not stats["unprimed_flat"]
            else "## primed headroom recoveries"
        ),
        "",
        (
            "*reference-agent trajectories are instrument-wiring checks "
            "(protocol §4); these rows are the measurement.*"
        ),
    ]
    md = Path(out_dir) / "report.md"
    md.write_text("\n".join(lines) + "\n")
    return md
