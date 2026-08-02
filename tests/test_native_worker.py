from pathlib import Path

from reschema.driver.podrun import run_worker

SUM = r"""
__attribute__((sysv_abi)) int sum_range(int lo, int hi) {
    int s = 0; for (int i = lo; i <= hi; i++) s += i; return s;
}
"""
SUM_PARAMS = [
    {"name": "lo", "kind": "i32", "range": [-100, 100]},
    {"name": "hi", "kind": "i32", "range": [-100, 100]},
]


def test_validate_roundtrip(tmp_path):
    r = run_worker(
        {
            "mode": "validate",
            "c_source": SUM,
            "fname": "sum_range",
            "params": SUM_PARAMS,
            "cases": [{"lo": 1, "hi": 4}],  # 1+2+3+4
        },
        tmp_path,
    )
    assert r == {"ok": True, "results": [{"ret": 10, "mem": {}}]}


def test_validate_compile_stage_preserved(tmp_path):
    r = run_worker(
        {
            "mode": "validate",
            "c_source": "int main( {",
            "fname": "sum_range",
            "params": SUM_PARAMS,
            "cases": [],
        },
        tmp_path,
    )
    assert r["stage"] == "compile" and r["stderr"]


def test_validate_missing_symbol_stage_preserved(tmp_path):
    r = run_worker(
        {
            "mode": "validate",
            "c_source": "int other_fn(void){return 0;}",
            "fname": "sum_range",
            "params": SUM_PARAMS,
            "cases": [],
        },
        tmp_path,
    )
    assert r["stage"] == "symbol" and "sum_range" in r["detail"]


def test_validate_unresolved_extern_is_link_stage(tmp_path):
    r = run_worker(
        {
            "mode": "validate",
            "c_source": "extern int missing(void);\n"
            "__attribute__((sysv_abi)) int sum_range(int a, int b){return missing();}",
            "fname": "sum_range",
            "params": SUM_PARAMS,
            "cases": [],
        },
        tmp_path,
    )
    assert r["stage"] == "link"


MIXED = r"""
__attribute__((sysv_abi)) void scale_buf(int *buf, int n, int factor) {
    for (int i = 0; i < n; i++) buf[i] *= factor;
}
__attribute__((sysv_abi)) void squares(int *buf, int n) {
    for (int i = 0; i < n; i++) buf[i] = i * i;
}
__attribute__((sysv_abi)) void flip(char *s) { for (char *p = s; *p; p++) *p ^= 1; }
"""
SCALE_PARAMS = [
    {"name": "buf", "kind": "buffer_i32", "length_param": "n"},
    {"name": "n", "kind": "i32", "range": [0, 8]},
    {"name": "factor", "kind": "i32", "range": [-3, 3]},
]
STR_PARAMS = [{"name": "s", "kind": "cstring"}]


def test_model_segfault_is_per_case_result_not_worker_death(tmp_path):
    r = run_worker(
        {
            "mode": "validate",
            "c_source": "__attribute__((sysv_abi)) int sum_range(int lo, int hi) { int *p = 0; *p = 1; return lo; }",
            "fname": "sum_range",
            "params": SUM_PARAMS,
            "cases": [{"lo": 1, "hi": 1}, {"lo": 2, "hi": 2}],
        },
        tmp_path,
    )
    # the worker survives and reports the crash structured at its index, then
    # stops (validator rejects on the first crash; sibling cases are moot)
    assert r == {"ok": True, "results": [{"crash": {"signal": 11}}]}


def test_model_hang_is_timeout_result(tmp_path):
    r = run_worker(
        {
            "mode": "validate",
            "c_source": "__attribute__((sysv_abi)) int sum_range(int lo, int hi) { for (;;) {} }",
            "fname": "sum_range",
            "params": SUM_PARAMS,
            "cases": [{"lo": 1, "hi": 1}, {"lo": 2, "hi": 2}],
        },
        tmp_path,
    )
    # worker stops after the FIRST crash: the validator rejects on it anyway,
    # and skipping 63 pointless 5s hangs keeps run_worker's timeout honest
    assert r == {"ok": True, "results": [{"crash": {"timeout": True}}]}


def test_crash_isolated_to_the_faulting_case(tmp_path):
    r = run_worker(
        {
            "mode": "validate",
            "c_source": "__attribute__((sysv_abi)) int sum_range(int lo, int hi) { if (lo < 0) { int *p = 0; *p = 1; } int s = 0; for (int i = lo; i <= hi; i++) s += i; return s; }",
            "fname": "sum_range",
            "params": SUM_PARAMS,
            "cases": [{"lo": 1, "hi": 2}, {"lo": -1, "hi": 1}, {"lo": 0, "hi": 1}],
        },
        tmp_path,
    )
    assert r["ok"] is True
    # cases BEFORE the crash compare normally; the crash is reported at its index
    # and the results list ends there (validator rejects on the first crash)
    assert r["results"] == [{"ret": 3, "mem": {}}, {"crash": {"signal": 11}}]


# Computes the right answer AND attempts two escapes: absolute-path write
# (ro root refuses) + outbound socket (--network none refuses). The escape
# attempts are folded into the return value so the test pins their failure
# deterministically, not just their containment.
ESCAPE = r"""
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
__attribute__((sysv_abi)) int sum_range(int lo, int hi) {
    FILE *f = fopen("/pwn", "w");
    int wrote = f != 0;
    if (f) fclose(f);
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a; memset(&a, 0, sizeof a);
    a.sin_family = AF_INET; a.sin_port = htons(443); a.sin_addr.s_addr = htonl(0x08080808);
    int conn = s >= 0 ? connect(s, (struct sockaddr *)&a, sizeof a) : -1;
    if (s >= 0) close(s);
    int q = 0; for (int i = lo; i <= hi; i++) q += i;
    return q * 1000 + wrote * 10 + (conn == 0);
}
"""


def test_containment_fs_write_and_network_refused(tmp_path):
    r = run_worker(
        {
            "mode": "validate",
            "c_source": ESCAPE,
            "fname": "sum_range",
            "params": SUM_PARAMS,
            "cases": [{"lo": 1, "hi": 4}],  # sum 10, wrote=0, conn!=0 -> 10000
        },
        tmp_path,
    )
    assert r == {"ok": True, "results": [{"ret": 10000, "mem": {}}]}
    assert not Path("/pwn").exists()  # host untouched


def test_compile_mode_gcc_clang_and_errors(tmp_path):
    # The pinned matrix toolchains are inside the image; seeds compile from a
    # repo-mounted path, models from inline source; failures carry stderr.
    r = run_worker(
        {
            "mode": "compile",
            "jobs": [
                {
                    "src_path": "reschema/corpus/seeds/rot13.c",
                    "out": "rot13-gcc",
                    "compiler": "gcc",
                    "flags": ["-O2", "-static", "-fno-pie", "-no-pie", "-g0"],
                },
                {
                    "src_path": "reschema/corpus/seeds/rot13.c",
                    "out": "rot13-clang",
                    "compiler": "clang",
                    "flags": ["-O2", "-static", "-fno-pie", "-no-pie", "-g0"],
                },
                {
                    "c_source": "int main( {",
                    "out": "broken",
                    "compiler": "gcc",
                    "flags": ["-O1"],
                },
            ],
        },
        tmp_path,
    )
    rcs = {j["out"]: j["rc"] for j in r["results"]}
    assert rcs == {"rot13-gcc": 0, "rot13-clang": 0, "broken": 1}
    assert (tmp_path / "rot13-gcc").exists() and (tmp_path / "rot13-clang").exists()
    assert next(j for j in r["results"] if j["out"] == "broken")["stderr"]
    # buffer_i32 in_out, buffer_i32 out-as-count, cstring (hex over JSON) —
    # marshaling parity with the old native path, all cases in one round trip.
    r = run_worker(
        {
            "mode": "validate",
            "c_source": MIXED,
            "fname": "scale_buf",
            "params": SCALE_PARAMS,
            "cases": [
                {"buf": [1, 2, 3], "n": 3, "factor": 3},
                {"buf": [0, 0], "n": 2, "factor": -2},
            ],
        },
        tmp_path,
    )
    assert r["ok"] is True
    # ret is register garbage for void fns (compare skips it); mem is the channel.
    assert [c["mem"] for c in r["results"]] == [{"buf": [3, 6, 9]}, {"buf": [0, 0]}]

    r = run_worker(
        {
            "mode": "validate",
            "c_source": MIXED,
            "fname": "squares",
            "params": SCALE_PARAMS[:2],
            "cases": [{"buf": 4, "n": 4}],  # int: out buffer of 4 elems
        },
        tmp_path,
    )
    assert r["ok"] is True
    assert [c["mem"] for c in r["results"]] == [{"buf": [0, 1, 4, 9]}]

    r = run_worker(
        {
            "mode": "validate",
            "c_source": MIXED,
            "fname": "flip",
            "params": STR_PARAMS,
            "cases": [{"s": b"abc\0".hex()}],
        },
        tmp_path,
    )
    assert r["ok"] is True
    assert [c["mem"] for c in r["results"]] == [{"s": b"`cb\0".hex()}]
