"""Phase-2A reference benchmark: a scripted deterministic agent plays a corpus
family end-to-end, yielding the reproducible E trajectory that any memory
feature (phase 2B+) must BEAT. Deterministic: probe counts and submission
counts are fixed; a correct model passes every hidden draw regardless of the
fresh entropy, so E depends only on the scripted history."""

import math

import pytest

from reschema.corpus.generate import build
from reschema.engine import TaskStore, status_snapshot, submit_program

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
    build()
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
