"""Phase-2A reference benchmark: a scripted deterministic agent plays a corpus
family end-to-end, yielding the reproducible E trajectory that any memory
feature (phase 2B+) must BEAT. Deterministic: probe counts and submission
counts are fixed; a correct model passes every hidden draw regardless of the
fresh entropy, so E depends only on the scripted history."""

import math

import pytest

from reschema.engine import TaskStore, status_snapshot, submit_program


@pytest.fixture(scope="module", autouse=True)
def _corpus_once(built_corpus):
    pass


GOOD_ROT13 = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""

ROT13_FAMILY = ["gcc-O0-sym", "gcc-O1-sym", "gcc-O2-sym", "gcc-O2-stripped"]
PROBE_INPUTS = ["alpha", "Beta", "zzz", "hello", "world!", "rot13"]


def run_family_baseline():
    trajectory = []
    for slot in ROT13_FAMILY:
        st = TaskStore(f"rot13::{slot}")
        for p in st.dir.glob("trace_*.json"):
            p.unlink()
        st._path("ledger.json").unlink(missing_ok=True)
        for i, word in enumerate(PROBE_INPUTS):
            st.record_case(f"e{i:02d}", [word], b"")
        r = submit_program(st, GOOD_ROT13)
        assert r["accepted"], (slot, r)
        trajectory.append(status_snapshot(st)["efficiency"]["E"])
    return trajectory


def test_reference_family_trajectory_flat_without_memory():
    # No memory substrate exists yet: every slot costs the same scripted
    # history (6 probes + 1 submission) => uniform E. Phase 2B must move the
    # LATER slots' scores UP from this flat baseline.
    traj = run_family_baseline()
    want = math.exp(-0.15 * (len(PROBE_INPUTS) - 1))
    assert traj == pytest.approx([want] * len(ROT13_FAMILY), abs=1e-9)


def test_family_memory_bends_trajectory_up(monkeypatch, tmp_path):
    """ISSUE-2B-4: play rot13 O0->O1->O2 with the cache primed by an accepted
    first slot; later slots submit the cached verified source with ZERO probes
    and pass in one submission: E=1.0 vs the flat baseline exp(-0.75)."""
    from reschema.memory import read_family

    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    first = TaskStore("rot13::gcc-O0-sym")
    for p in first.dir.glob("trace_*.json"):
        p.unlink()
    first._path("ledger.json").unlink(missing_ok=True)
    for i, word in enumerate(["alpha", "Beta", "zzz", "hello", "world!", "rot13"]):
        first.record_case(f"e{i:02d}", [word], b"")
    r = submit_program(first, GOOD_ROT13)
    assert r["accepted"] and status_snapshot(first)["efficiency"]["E"] == pytest.approx(
        math.exp(-0.75)
    )
    cached = read_family("rot13", fn="__main__", root=tmp_path)
    assert (
        cached
        and cached[0]["tier"] == "verified_fact"
        and cached[0]["promoted"] is True
    )

    traj = [math.exp(-0.75)]
    for slot in ("rot13::gcc-O1-sym", "rot13::gcc-O2-sym"):
        st = TaskStore(slot)
        for p in st.dir.glob("trace_*.json"):
            p.unlink()
        st._path("ledger.json").unlink(missing_ok=True)
        # no probing: the family memory carries both the verified source AND the
        # proven ABI shape — submit from the cache, not from experiments
        res = submit_program(st, cached[0]["c_source"])
        assert res["accepted"] is not None
        e = status_snapshot(st)["efficiency"]
        assert e["n_exp"] == 0 and e["n_sub"] == 1 and e["E"] == 1.0
        traj.append(e["E"])
    assert traj == [math.exp(-0.75), 1.0, 1.0]
