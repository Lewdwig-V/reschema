import random
import re

import pytest

from reschema.corpus.generate import _symtab
from reschema.driver.calling import batch_call_original, call_original, gen_inputs
from reschema.driver.podrun import run_worker
from reschema.driver.spec import Param

MODEL = r"""
#include <stdint.h>
static int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi){
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi)) int32_t sum_range(int32_t lo, int32_t hi){
    int32_t s=0; for(int32_t i=lo;i<=hi;i++) s=clamp_i32(s+i,-1000,1000); return s;
}"""

# Test-only probe binary (NOT a corpus seed): exercises driver failure modes.
PROBE = r"""
#include <stdint.h>
int32_t bigframe_sum(int32_t *buf, int32_t n){
    char scratch[3000];          /* local frame > 1KB headroom ceiling */
    for (int i = 0; i < 3000; i++) scratch[i] = (char)i;
    int32_t s = 0;
    for (int i = 0; i < n; i++) s += buf[i];  /* read args AFTER frame writes */
    return s + scratch[5];       /* scratch[5] == 5, deterministic */
}
__attribute__((sysv_abi)) int32_t spin(int32_t x){
    volatile int32_t v = x; while (1) {} return v;
}
/* Static state: batch ground truth must reset .bss between cases or n accumulates. */
int32_t bump(int32_t x){ static int32_t n = 0; n += x; return n; }
/* Syscall witness: raw getpid (qiling 1.4.6 returns a fixed 0x512) + a static
   x accumulator — if process state ever bled case N -> N+1 on a shared VM,
   case N+1's return carries the residue. Must route batch to fresh VMs. */
__attribute__((sysv_abi)) int32_t pidish(int32_t x){
    static int32_t n = 0;
    long r;
    __asm__ volatile("mov $39, %0\n\tsyscall" : "=a"(r) : : "rcx", "r11");
    n += x;
    return (int32_t)r + n;
}
/* Faults only for oversized n: walks buf past the mapped stack. */
int32_t fallible(int32_t *buf, int32_t n){
    int32_t s = 0; for (int32_t i = 0; i < n; i++) s += buf[i]; return s;
}
int main(void){ return 0; }
"""

STATEFUL = r"""
#include <stdint.h>
int32_t bump(int32_t x){ static int32_t n = 0; n += x; return n; }
"""


@pytest.fixture(scope="module")
def manifest(built_corpus):
    return built_corpus


@pytest.fixture(scope="module")
def probe_bin(tmp_path_factory):
    d = tmp_path_factory.mktemp("probe")
    # -fno-stack-protector: the canary read (fs:[0x28] TLS) is unmapped when qiling jumps
    # straight to a function address — orthogonal to the frame-placement bug under test.
    r = run_worker(
        {
            "mode": "compile",
            "jobs": [
                {
                    "c_source": PROBE,
                    "out": "probe",
                    "compiler": "gcc",
                    "flags": [
                        "-O1",
                        "-static",
                        "-fno-pie",
                        "-no-pie",
                        "-fno-stack-protector",
                        "-g0",
                    ],
                }
            ],
        },
        d,
    )
    assert r.get("results"), r  # infra failure surfaces the podrun stage detail
    res = r["results"][0]
    assert res["rc"] == 0, res["stderr"]
    binp = d / "probe"
    return str(binp), _symtab(binp)


def _slot(manifest, seed, func, stripped=False, opt="-O2"):
    t = next(
        x
        for x in manifest
        if x["seed"] == seed
        and x["opt"] == opt
        and x["stripped"] == stripped
        and x["compiler"] == "gcc"
    )
    return t["binary"], t["functions"][func]["addr"]


def _pw_hash_ref(s: bytes) -> int:
    h = 5381
    for c in s.rstrip(b"\0"):
        h = (h * 33 + c) & 0xFFFFFFFF
    return h


def _as_int32(v: int) -> int:
    return v - 0x100000000 if v & 0x80000000 else v


def test_rot13_char_ground_truth(manifest):
    binary, addr = _slot(manifest, "rot13", "rot13_char")
    params = [Param("c", "i32")]
    for cin, cout in (("a", "n"), ("n", "a"), ("z", "m"), ("A", "N"), ("!", "!")):
        out = call_original(binary, addr, params, {"c": ord(cin)})
        assert out["ret"] & 0xFF == ord(cout)


def test_rot13_in_out_memory(manifest):
    binary, addr = _slot(manifest, "rot13", "rot13")
    params = [Param("in_out", "cstring", direction="in_out")]
    case = {"in_out": b"hello\x00"}
    out = call_original(binary, addr, params, case)
    assert out["mem"]["in_out"] == b"uryyb\x00"


def test_pw_hash_matches_reference(manifest):
    binary, addr = _slot(manifest, "check", "pw_hash")
    params = [Param("s", "cstring")]
    for s in (b"abc\0", b"hello world\0", b"\0"):
        out = call_original(binary, addr, params, {"s": s})
        assert out["ret"] == _as_int32(_pw_hash_ref(s))


def test_check_pw_rejects_wrong_password(manifest):
    binary, addr = _slot(manifest, "check", "check_pw")
    params = [Param("s", "cstring")]
    out = call_original(binary, addr, params, {"s": b"abc\0"})
    assert out["ret"] == 0


def test_scale_buf_in_out_buffer(manifest):
    binary, addr = _slot(manifest, "calc", "scale_buf")
    params = [
        Param("buf", "buffer_i32", direction="in_out", length_param="n"),
        Param("n", "i32"),
        Param("factor", "i32"),
    ]
    case = {"buf": [1, -50, 10, 5], "n": 4, "factor": 3}
    out = call_original(binary, addr, params, case)
    assert out["mem"]["buf"] == [3, -100, 30, 15]


def test_sum_range_matches_worker_model(manifest, tmp_path):
    binary, addr = _slot(manifest, "calc", "sum_range")
    params = [Param("lo", "i32", range=(-20, 10)), Param("hi", "i32", range=(10, 30))]
    cases = gen_inputs(params, random.Random(1), 4)
    r = run_worker(
        {
            "mode": "validate",
            "c_source": MODEL,
            "fname": "sum_range",
            "params": [p.to_json() for p in params],
            "cases": cases,
        },
        tmp_path,
    )
    assert r["ok"] is True
    for case, got in zip(cases, r["results"]):
        assert case["lo"] <= case["hi"]
        assert call_original(binary, addr, params, case)["ret"] == got["ret"]


# Pinned: addresses come from the manifest pre-strip, so both slots must work.
@pytest.mark.parametrize("stripped", [False, True])
def test_clamp_i32_ground_truth(manifest, stripped):
    binary, addr = _slot(manifest, "calc", "clamp_i32", stripped)
    params = [Param("v", "i32"), Param("lo", "i32"), Param("hi", "i32")]
    for v, lo, hi, want in ((5, 0, 10, 5), (-5, 0, 10, 0), (50, 0, 10, 10)):
        out = call_original(binary, addr, params, {"v": v, "lo": lo, "hi": hi})
        assert out["ret"] == want


def test_bigframe_stack_buffers_unclobbered(probe_bin):
    """A callee frame > ~1KB must not overwrite marshalled buffers (reviewer's bigframe probe)."""
    binary, syms = probe_bin
    params = [Param("buf", "buffer_i32", length_param="n"), Param("n", "i32")]
    case = {"buf": [10, 20, 30, 40], "n": 4}
    out = call_original(binary, syms["bigframe_sum"][0], params, case)
    assert out["exit_code"] == 0
    assert out["ret"] == sum(case["buf"]) + 5


def test_timeout_returns_fault_not_garbage(probe_bin):
    binary, syms = probe_bin
    out = call_original(binary, syms["spin"][0], [Param("x", "i32")], {"x": 7})
    assert out["exit_code"] == -1
    assert out["events"][-1] == {"phase": "fault", "sc": "timeout", "args": []}


def test_emulation_crash_returns_fault(probe_bin):
    binary, _ = probe_bin
    out = call_original(binary, 0x111, [Param("x", "i32")], {"x": 1})
    assert out["exit_code"] == -1
    ev = out["events"][-1]
    assert ev["phase"] == "fault"
    assert ev["sc"] == "crash"
    # recorder.py convention: "TypeName: message", never repr()
    assert re.match(r"^\w+: ", ev["args"][0])


def test_model_state_isolated_per_call(tmp_path):
    """Two identical calls must give identical results; stale .so images must not bleed statics."""
    params = [Param("x", "i32")]
    r = run_worker(
        {
            "mode": "validate",
            "c_source": STATEFUL,
            "fname": "bump",
            "params": [p.to_json() for p in params],
            "cases": [{"x": 5}, {"x": 5}],
        },
        tmp_path,
    )
    assert r["ok"] is True
    assert (r["results"][0]["ret"], r["results"][1]["ret"]) == (5, 5)


def test_beyond_six_args_raises(probe_bin):
    binary, syms = probe_bin
    params = [Param(f"a{i}", "i32") for i in range(7)]
    case = {p.name: i for i, p in enumerate(params)}
    with pytest.raises(NotImplementedError):
        call_original(binary, syms["bigframe_sum"][0], params, case)


def _strip_mode(traces: list[dict]) -> list[dict]:
    """Trace equality modulo the batch_mode routing annotation."""
    return [{k: v for k, v in t.items() if k != "batch_mode"} for t in traces]


def test_batch_syscalling_original_falls_back_to_fresh_vms(probe_bin):
    """A syscall insn in the function slice must reroute the round to per-case
    fresh VMs — ql.save/restore spans registers+memory only, not rootfs/fd/OS
    state (codex: snapshot is no isolation contract for syscalling originals)."""
    binary, syms = probe_bin
    params = [Param("x", "i32", range=(-50, 50))]
    cases = gen_inputs(params, random.Random(80), 8)
    outs = batch_call_original(binary, syms["pidish"][0], params, cases)
    assert [o["batch_mode"] for o in outs] == ["fresh-vm-fallback"] * len(cases)
    assert all(o["exit_code"] == 0 for o in outs)  # the syscall really ran
    fresh = [call_original(binary, syms["pidish"][0], params, c) for c in cases]
    # identity pin, residue included: fallback output == per-case fresh output
    assert _strip_mode(outs) == fresh


# The issue-41 judgment guard: one VM serving N cases must match N fresh VMs
# byte-for-byte. O0 slots (unoptimized stack traffic) + a static-state witness
# (bump) leave state bleed nowhere to hide.
def test_batch_call_original_matches_fresh_per_case(manifest, probe_bin):
    pbin, syms = probe_bin
    shapes = [
        (
            *_slot(manifest, "calc", "sum_range", opt="-O0"),
            [Param("lo", "i32", range=(-20, 10)), Param("hi", "i32", range=(10, 30))],
        ),
        (
            *_slot(manifest, "calc", "scale_buf", opt="-O0"),
            [
                Param(
                    "buf",
                    "buffer_i32",
                    direction="in_out",
                    length_param="n",
                    range=(51, 100),
                ),
                Param("n", "i32", range=(3, 4)),
                Param("factor", "i32", range=(2, 5)),
            ],
        ),
        (
            *_slot(manifest, "rot13", "rot13", opt="-O0"),
            [Param("in_out", "cstring", direction="in_out")],
        ),
        (pbin, syms["bump"][0], [Param("x", "i32", range=(-50, 50))]),
    ]
    for binary, addr, params in shapes:
        cases = gen_inputs(params, random.Random(41), 16)
        fresh = [call_original(binary, addr, params, c) for c in cases]
        batched = batch_call_original(binary, addr, params, cases)
        assert _strip_mode(batched) == fresh
        # syscall-free kernels only: the snapshot path must serve all of them
        # (bump's static-state isolation included)
        assert all(o["batch_mode"] == "batched-snapshot" for o in batched)


def test_batch_pre_case_hook_fires_without_changing_outputs(probe_bin):
    # #121 measurement seam: the optional hook fires once per case (after the
    # snapshot restore) and MUST leave per-case outputs bit-identical.
    pbin, syms = probe_bin
    hits = []
    params = [Param("x", "i32", range=(-50, 50))]
    cases = gen_inputs(params, random.Random(41), 4)
    plain = batch_call_original(pbin, syms["bump"][0], params, cases)
    hooked = batch_call_original(
        pbin, syms["bump"][0], params, cases, pre_case_hook=hits.append
    )
    assert _strip_mode(hooked) == _strip_mode(plain) == _strip_mode(plain)
    assert len(hits) == len(cases)


def test_batch_call_original_fault_then_clean(probe_bin):
    """A crashing case must yield exit_code -1 for THAT case, with the next case clean."""
    binary, syms = probe_bin
    params = [Param("buf", "buffer_i32", length_param="n"), Param("n", "i32")]
    good1 = {"buf": [1, 2, 3, 4], "n": 4}
    boom = {"buf": [1, 2, 3, 4], "n": 10_000_000}  # walks off the mapped stack
    good2 = {"buf": [5, 6, 7, 8], "n": 4}
    outs = batch_call_original(
        binary, syms["fallible"][0], params, [good1, boom, good2]
    )
    assert [o["exit_code"] for o in outs] == [0, -1, 0]
    assert outs[1]["events"][-1]["sc"] == "crash"
    assert outs[1]["events"][-1]["phase"] == "fault"
    assert outs[0]["ret"] == sum(good1["buf"])
    assert outs[2]["ret"] == sum(good2["buf"])


def test_batch_call_original_timeout_isolated(probe_bin):
    binary, syms = probe_bin
    outs = batch_call_original(binary, syms["spin"][0], [Param("x", "i32")], [{"x": 7}])
    assert outs[0]["exit_code"] == -1
    assert outs[0]["events"][-1] == {"phase": "fault", "sc": "timeout", "args": []}
