import pytest

from tools.dogfood.measure import phi_family, slot_efficiency


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
