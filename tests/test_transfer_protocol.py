"""ISSUE-12 (Phase 2C): the transfer protocol as CI wiring.

For each family: play a slot chain (O0 -> O1 -> O2 sym) twice — UNPRIMED (a
cold deduction cache, isolated in tmp) measuring the baseline E trajectory;
then PRIMED (slot 1 pays the probe cost, filling the cache; later slots reuse
the injected verified_fact with zero extra probing). Phi = mean headroom
recovered on later slots, uniform weights (v1; tuning needs dogfood data).

The harness here is also the protocol description docs/benchmark-protocol.md
refers to for live-agent reruns.
"""

import pytest
from conftest import wipe_task

from reschema.engine import TaskStore, status_snapshot, submit_program


@pytest.fixture(scope="module", autouse=True)
def _corpus_once(built_corpus):
    pass


from reschema.memory import read_family

GOOD_ROT13 = r"""
#include <stdio.h>
int main(int argc, char **argv){ if(argc<2){puts("usage: rot13 WORD");return 2;}
for(char*p=argv[1];*p;p++){char c=*p;
 if(c>='a'&&c<='z')*p='a'+(c-'a'+13)%26; else if(c>='A'&&c<='Z')*p='A'+(c-'A'+13)%26;}
puts(argv[1]); return 0; }
"""

GOOD_CHECK_PROG = r"""
#include <stdio.h>
#include <string.h>
#include <stdint.h>
int main(void){ char buf[64];
 if(!fgets(buf,sizeof buf,stdin)) return 2;
 buf[strcspn(buf,"\n")]=0;
 uint32_t h=5381u; for(char*s=buf;*s;s++) h=h*33u+(unsigned char)*s;
 if(h==0x1F33E35Fu){ puts("OK"); return 0; }
 puts("NOPE"); return 1; }
"""

PROBE_ARGS = {
    "rot13": ["alpha", "Beta", "zzz", "hello", "world!", "rot13"],
    # txy"od is the known-check pre-image: no always-reject model can complete a
    # protocol whose probe set proves the seed ACCEPTS anything at all.
    "check": ["a", "hello", "hunter2", "preimage", "zzzzz", 'txy"od'],
}
SEEDS = {"rot13": GOOD_ROT13, "check": GOOD_CHECK_PROG}
SLOTS = ["gcc-O0-sym", "gcc-O1-sym", "gcc-O2-sym"]


def _store(task_id):
    st = TaskStore(task_id)
    wipe_task(st)
    return st


def _run_family(seed, primed, monkeypatch, tmp_path):
    monkeypatch.setattr("reschema.memory.MEMORY", tmp_path)
    src = SEEDS[seed]
    e_traj = []
    primed_source = None
    for k, slot in enumerate(SLOTS):
        st = _store(f"{seed}::{slot}")
        mem_root = tmp_path if primed else tmp_path / slot  # cold isolation per slot
        monkeypatch.setattr("reschema.memory.MEMORY", mem_root)
        if not primed:
            assert read_family(seed, root=mem_root) == []  # cold slot: no facts yet
        priming_done = primed and k > 0
        if not priming_done:
            # UNPRIMED probes every slot; primed pays probes only at slot 0
            for i, arg in enumerate(PROBE_ARGS[seed][:6]):
                if seed == "check":
                    st.record_case(f"e{i:02d}", [], (arg + "\n").encode())
                else:
                    st.record_case(f"e{i:02d}", [arg], b"")
        src = primed_source if priming_done else src
        r = submit_program(st, src)
        if k == 0 and primed:
            primed_source = read_family(seed, fn="__main__", root=mem_root)[0][
                "c_source"
            ]
        assert r["accepted"], (seed, slot, k, r)
        e_traj.append(status_snapshot(st)["efficiency"]["E"])
    return e_traj


def phi(baseline: list[float], primed: list[float]) -> float:
    base0 = baseline[0]
    headroom = 1.0 - base0
    return sum((p - base0) / headroom for p in primed[1:]) / max(len(primed) - 1, 1)


def test_transfer_protocol_rot13_and_check(monkeypatch, tmp_path):
    for seed in ("rot13", "check"):
        up = tmp_path / f"up_{seed}"
        pr = tmp_path / f"pr_{seed}"
        up.mkdir(exist_ok=True)
        pr.mkdir(exist_ok=True)
        baseline = _run_family(seed, False, monkeypatch, up)
        primed = _run_family(seed, True, monkeypatch, pr)
        # baseline: flat cost trajectory (no memory anywhere)
        assert baseline == pytest.approx([baseline[0]] * 3, abs=1e-9)
        # primed: later slots hit E==1.0 — the cache carries the source/scaffold
        assert primed[1:] == [1.0, 1.0]
        score = phi(baseline, primed)
        assert score == pytest.approx(1.0), f"{seed} transfer delta {score}"
        # protocol coldness pin: unprimed slots isolate memory per slot, so a
        # live agent following this protocol opens a COLD slot every time
        for slot in SLOTS:
            assert len(read_family(seed, root=up / slot)) == 1  # its own, post hoc


def test_check_family_rejects_always_nope_attack(tmp_path):
    # P1 pin: an always-"NOPE" model is a completed protocol only if the check
    # probe set lacks an accepting case. Shows the pre-image probe is load-bearing.
    st = _store("check::gcc-O0-sym")
    st.record_case("e00", [], b"nope\n")
    st.record_case("e01", [], b'txy"od\n')  # the known-accepting pre-image
    hard = r"""
#include <stdio.h>
int main(void){ puts("NOPE"); return 1; }
"""
    r = submit_program(st, hard)
    assert not r["accepted"]
    assert r["reason"] == "io-mismatch"  # dies on the accepting pre-image case
