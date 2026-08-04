import math

import pytest

from tools.dogfood.measure import phi_family, slot_efficiency

E0 = math.exp(-0.75)  # unprimed cold-start reference (6 probes, 1 submission)
HEADROOM = 1.0 - E0


def _rec(condition, slot_index, accepted, n_exp, n_sub=1, rep=None):
    r = {
        "condition": condition,
        "slot_index": slot_index,
        "accepted": accepted,
        "n_exp": n_exp,
        "n_sub": n_sub,
    }
    if rep is not None:
        r["rep"] = rep
    return r


def test_slot_efficiency_matches_reference_arithmetic():
    # 6 probes + 1 submission, accepted -> exp(-0.75) (protocol §4 reference point)
    e = slot_efficiency(True, 6, 1)
    assert e == pytest.approx(0.4723665, abs=1e-6)
    assert slot_efficiency(True, 0, 1) == pytest.approx(1.0)  # primed reuse
    assert slot_efficiency(False, 6, 2) == 0.0  # unaccepted


def test_phi_family_median_and_flat_check():
    recs = [
        {
            "condition": "primed",
            "slot_index": 1,
            "accepted": True,
            "n_exp": 0,
            "n_sub": 1,
        },
        {
            "condition": "primed",
            "slot_index": 2,
            "accepted": True,
            "n_exp": 0,
            "n_sub": 1,
        },
        {
            "condition": "unprimed",
            "slot_index": 0,
            "accepted": True,
            "n_exp": 6,
            "n_sub": 1,
        },
        {
            "condition": "unprimed",
            "slot_index": 1,
            "accepted": True,
            "n_exp": 6,
            "n_sub": 1,
        },
        {
            "condition": "unprimed",
            "slot_index": 2,
            "accepted": True,
            "n_exp": 6,
            "n_sub": 1,
        },
    ]
    r = phi_family(recs)
    assert r["unprimed_flat"] is True
    assert r["phi_median"] == pytest.approx(1.0)
    assert r["n_deltas"] == 2  # one per later slot


def test_phi_family_rejected_primed_slot_counts_as_zero():
    # Survivor bias check: a rejected primed slot contributes E=0.0, so
    # phi_median must land near 0.05, NOT at the survivor-only 1.0.
    recs = [
        _rec("unprimed", 0, True, 6),
        _rec("primed", 1, False, 0),  # rejected -> E = 0.0
        _rec("primed", 2, True, 0),
    ]
    r = phi_family(recs)
    phi_rejected = (0.0 - E0) / HEADROOM
    assert r["phi_median"] == pytest.approx((phi_rejected + 1.0) / 2)  # ~0.05
    assert r["phi_median"] != pytest.approx(1.0)
    assert r["deltas"][0]["primed_e"] == 0.0


def test_phi_family_per_rep_median_and_iqr():
    # Three uneven reps; phi is per-rep (median over later slots), then
    # median/IQR across reps — the protocol's spread over paired runs.
    recs = [_rec("unprimed", 0, True, 6, rep=rep) for rep in (1, 2, 3)]
    recs += [
        _rec("primed", 1, True, 0, rep=1),  # rep 1: clean reuse twice
        _rec("primed", 2, True, 0, rep=1),
        _rec("primed", 1, False, 0, rep=2),  # rep 2: rejected slot, probed slot
        _rec("primed", 2, True, 2, rep=2),
        _rec("primed", 1, True, 3, rep=3),  # rep 3: one mildly probed slot
    ]
    r = phi_family(recs)
    phi_lo = ((0.0 - E0) / HEADROOM + (math.exp(-0.15) - E0) / HEADROOM) / 2  # rep 2
    phi_mid = (math.exp(-0.30) - E0) / HEADROOM  # rep 3
    phi_hi = 1.0  # rep 1
    assert phi_lo < phi_mid < phi_hi  # pin the intended ordering
    # median across reps; a pooled median over all five slots would be exp(-.15)'s phi
    assert r["phi_median"] == pytest.approx(phi_mid)
    # 3 rep values: quartile cutpoints land exactly on min/max -> IQR = max - min
    assert r["phi_iqr"] == pytest.approx(phi_hi - phi_lo)
    assert r["phi_iqr"] > 0


def test_phi_family_base0_missing_yields_deltas_without_phi():
    recs = [
        _rec("unprimed", 1, True, 6),  # no unprimed slot 0 -> no baseline
        _rec("primed", 1, True, 0),
        _rec("primed", 2, True, 0),  # no unprimed counterpart at slot 2
    ]
    r = phi_family(recs)
    assert r["phi_median"] is None
    assert r["phi_iqr"] is None
    assert r["n_deltas"] == 2
    d1, d2 = r["deltas"]
    assert set(d1) == {"slot_index", "primed_e", "unprimed_e", "delta"}
    assert d1["delta"] == pytest.approx(1.0 - E0)
    assert d2["unprimed_e"] is None and d2["delta"] is None


def test_phi_family_zero_headroom_yields_deltas_without_phi():
    recs = [
        _rec("unprimed", 0, True, 0),  # base0 = 1.0 -> zero headroom
        _rec("unprimed", 1, True, 6),
        _rec("primed", 1, True, 0),
    ]
    r = phi_family(recs)
    assert r["phi_median"] is None
    assert r["phi_iqr"] is None
    assert r["deltas"] == [
        {"slot_index": 1, "primed_e": 1.0, "unprimed_e": E0, "delta": 1.0 - E0}
    ]
