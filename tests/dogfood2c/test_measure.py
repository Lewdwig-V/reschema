import json
import math

import pytest

from tools.dogfood.measure import phi_family, render_report, slot_efficiency

E0 = math.exp(-0.75)  # unprimed cold-start reference (6 probes, 1 submission)
HEADROOM = 1.0 - E0


def _write_rec(d, name, **kw):
    base = {
        "slot_id": name,
        "family": "rot13",
        "condition": "unprimed",
        "slot_index": 0,
        "rep": 1,
        "outcome": "accepted",
        "E": 0.0,
        "n_exp": 0,
        "n_sub": 1,
        "accepted": True,
        "wall_s": 1.0,
        "run_header": {},
    }
    (d / name).write_text(json.dumps({**base, **kw}) + "\n")


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


def test_render_report_writes_markdown_and_excludes_infra_error(tmp_path):
    (tmp_path / "rot13-unprimed-gcc-O0-sym-r1.jsonl").write_text(
        '{"slot_id":"a","family":"rot13","condition":"unprimed","slot_index":0,'
        '"rep":1,"outcome":"accepted","E":0.5,"n_exp":6,"n_sub":1,'
        '"accepted":true,"wall_s":1.0,"run_header":{}}\n'
    )
    (tmp_path / "rot13-unprimed-gcc-O1-sym-r1.jsonl").write_text(
        '{"slot_id":"b","family":"rot13","condition":"unprimed","slot_index":1,'
        '"rep":1,"outcome":"infra-error","E":0.0,"n_exp":0,"n_sub":0,'
        '"accepted":false,"wall_s":0.0,"run_header":{}}\n'
    )
    md = render_report(tmp_path, family="rot13", out_dir=tmp_path)
    text = md.read_text()
    assert "rot13" in text and "infra-error" in text  # evidence shown...
    # ...but its E=0.0 must NOT enter statistics: the accepted-only trajectory
    # present, no crash on the mixed population. Visible markers: the counts
    # line shows the infra-error tally, and φ renders "None" (no primed arm)
    # rather than a number poisoned by the adapter-failure record.
    assert "1 infra-error" in text
    assert "unprimed trajectory: [" in text
    # The discriminating pin: the infra record's E=0.0 must NOT enter the
    # trajectory — mutation (drop the `measured` filter) grows this to
    # [e, 0.0] and this assertion bites. Value computed via slot_efficiency
    # so engine-constant drift moves both sides together.
    assert f"unprimed trajectory: [{slot_efficiency(True, 6, 1)!r}]" in text
    # denominator honesty: records (not slots), with per-condition split
    # over the measured population — the infra record is counted separately
    assert "records: 2 total (primed 0, unprimed 1)" in text


def test_render_report_renders_per_slot_deltas_rows(tmp_path):
    # Ascending unprimed trajectory (8 probes at O0, 4 at O1) → non-flat →
    # the deltas section must carry one ROW per primed later slot, including
    # slot 2 whose unprimed baseline is missing (renders "—").
    _write_rec(tmp_path, "rot13-unprimed-gcc-O0-sym-r1.jsonl", slot_index=0, n_exp=8)
    _write_rec(tmp_path, "rot13-unprimed-gcc-O1-sym-r1.jsonl", slot_index=1, n_exp=4)
    _write_rec(
        tmp_path,
        "rot13-primed-gcc-O0-sym-r1-s0.jsonl",
        condition="primed",
        slot_index=0,
        n_exp=0,
    )
    _write_rec(
        tmp_path,
        "rot13-primed-gcc-O1-sym-r1-s1.jsonl",
        condition="primed",
        slot_index=1,
        n_exp=0,
    )
    _write_rec(
        tmp_path,
        "rot13-primed-gcc-O2-sym-r1-s2.jsonl",
        condition="primed",
        slot_index=2,
        n_exp=0,
    )
    text = render_report(tmp_path, family="rot13", out_dir=tmp_path).read_text()
    assert "## per-slot deltas" in text
    assert "- slot 1:" in text and "- slot 2:" in text
    assert "—" in text  # no unprimed baseline at slot 2: None renders as dash


def test_render_report_shows_phi_base_and_abort_classes(tmp_path):
    _write_rec(tmp_path, "rot13-unprimed-gcc-O0-sym-r1.jsonl", slot_index=0, n_exp=6)
    _write_rec(
        tmp_path,
        "rot13-primed-gcc-O0-sym-r1-s0.jsonl",
        condition="primed",
        slot_index=0,
        n_exp=0,
    )
    _write_rec(
        tmp_path,
        "rot13-primed-gcc-O1-sym-r1-s1.jsonl",
        condition="primed",
        slot_index=1,
        n_exp=0,
        outcome="aborted: priming-failed",
        accepted=False,
    )
    _write_rec(
        tmp_path,
        "rot13-unprimed-gcc-O1-sym-r1.jsonl",
        condition="unprimed",
        slot_index=1,
        outcome="aborted: timeout",
        accepted=False,
    )
    text = render_report(tmp_path, family="rot13", out_dir=tmp_path).read_text()
    # thin population visible beside φ: one rep fed it
    assert "1 rep(s) contributing" in text
    # abort classes listed separately, not lumped into one "aborted" count
    assert "aborted: priming-failed ×1" in text
    assert "aborted: timeout ×1" in text


def test_load_filters_family_and_skips_empty_records(tmp_path):
    _write_rec(tmp_path, "rot13-unprimed-gcc-O0-sym-r1.jsonl", slot_index=0, n_exp=6)
    # the "rot13-*" glob also matches the rot13-EXT stem: filter on the field
    _write_rec(
        tmp_path,
        "rot13-ext-unprimed-gcc-O0-sym-r1.jsonl",
        family="rot13-ext",
        slot_index=0,
        n_exp=6,
    )
    (tmp_path / "rot13-unprimed-gcc-O1-sym-r1.jsonl").write_text("")  # crashed write
    text = render_report(tmp_path, family="rot13", out_dir=tmp_path).read_text()
    assert "records: 1 total" in text


def test_phi_family_within_rep_uses_mean_over_later_slots():
    # Protocol §2: WITHIN a rep, φ is the MEAN over later slots; median/IQR
    # is reserved for the across-rep spread. 3 later slots where mean differs
    # from median — the 2-slot case masks the distinction.
    recs = [_rec("unprimed", 0, True, 6, rep=1)]
    recs += [
        _rec("primed", 1, True, 0, rep=1),
        _rec("primed", 2, True, 0, rep=1),
        _rec("primed", 3, False, 0, rep=1),  # rejected -> E=0.0
    ]
    r = phi_family(recs)
    expected_mean = (1.0 + 1.0 + (0.0 - E0) / HEADROOM) / 3
    assert r["phi_median"] == pytest.approx(expected_mean)
    assert r["phi_median"] != pytest.approx(1.0)  # the median would sit at 1.0
