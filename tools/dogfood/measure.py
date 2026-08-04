"""Statistics over slot JSONL records. The only place numbers are computed;
slot/driver code stays dumb."""

from __future__ import annotations

import math
import statistics

from reschema.engine import E_ALPHA, E_BETA

FLAT_EPS = 1e-3  # "materially non-flat" threshold (protocol §5)


def slot_efficiency(accepted: bool, probes: int, subs: int) -> float:
    """E = accepted * exp(-(alpha*max(0,probes-1) + beta*(subs-1))) — engine's formula."""
    return (
        math.exp(-(E_ALPHA * max(0, probes - 1) + E_BETA * max(0, subs - 1)))
        if accepted
        else 0.0
    )


def _e_by_slot(records: list[dict]) -> dict[int, float]:
    """Median efficiency per slot; many reps of one slot aggregate here."""
    by_slot: dict[int, list[float]] = {}
    for r in records:
        by_slot.setdefault(r["slot_index"], []).append(
            slot_efficiency(True, r["n_exp"], r["n_sub"])
        )
    return {k: statistics.median(v) for k, v in by_slot.items()}


def phi_family(records: list[dict]) -> dict:
    """Median headroom recovery over later slots vs slot-0 unprimed baseline."""
    up_e = _e_by_slot(
        r for r in records if r["condition"] == "unprimed" and r["accepted"]
    )
    pr_e = _e_by_slot(
        r for r in records if r["condition"] == "primed" and r["accepted"]
    )
    base0 = up_e.get(0)
    headroom = 1.0 - base0 if base0 is not None else None
    deltas, phis = [], []
    if headroom:
        for slot in sorted(pr_e):
            if slot == 0:
                continue
            e = pr_e[slot]
            deltas.append(
                {
                    "slot_index": slot,
                    "primed_e": e,
                    "unprimed_e": up_e.get(slot),
                    "delta": e - up_e.get(slot, 0.0),
                }
            )
            phis.append((e - base0) / headroom)
    flat = len({round(v, 6) for v in up_e.values()}) == 1 or (
        bool(up_e) and max(up_e.values()) - min(up_e.values()) < FLAT_EPS
    )
    return {
        "phi_median": statistics.median(phis) if phis else None,
        "phi_iqr": (
            (statistics.quantiles(phis, n=4)[2] - statistics.quantiles(phis, n=4)[0])
            if len(phis) >= 4
            else None
        ),
        "unprimed_flat": flat,
        "unprimed_traj": [up_e[k] for k in sorted(up_e)],
        "deltas": deltas,
        "n_deltas": len(deltas),
    }
