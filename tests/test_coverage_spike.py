"""Real Qiling hook/snapshot regressions using fixed, compiler-free guest bytes.

The batch lifecycle is exercised with Qiling itself. ELF marshalling and trace
equivalence remain covered by test_driver's Podman-built corpus/probe fixtures.
"""

from contextlib import contextmanager
from itertools import pairwise

import pytest
from qiling import Qiling
from qiling.const import QL_ARCH, QL_OS

from reschema.driver import calling
from tools.coverage_spike import MASK, _hook_recorder, _timed


@pytest.fixture
def batch_vm(tmp_path, monkeypatch):
    # test edi,edi; jz zero; inc eax; jmp done; zero: dec eax; done: nop
    code = bytes.fromhex("85 ff 74 04 ff c0 eb 02 ff c8 90")
    vms = []

    def boot(_binary):
        ql = Qiling(
            code=code,
            rootfs=str(tmp_path),
            archtype=QL_ARCH.X8664,
            ostype=QL_OS.LINUX,
            verbose=0,
            console=False,
        )
        ql.arch.regs.eax = 0
        vms.append(ql)
        return ql

    def run(ql, _addr, _params, case):
        ql.arch.regs.edi = case["x"]
        start = ql.os.entry_point
        ql.emu_start(start, start + len(code))
        ql.mem.write(start + 0x100, b"x")
        return {"ret": ql.arch.regs.eax, "mem": {}, "exit_code": 0, "events": []}

    monkeypatch.setattr(calling, "_boot_vm", boot)
    monkeypatch.setattr(calling, "_fn_has_syscall", lambda *_: False)
    monkeypatch.setattr(calling, "_run_case", run)
    return vms, run


def _batch(cases, scope=None):
    return calling.batch_call_original("fixture", 0, [], cases, hook_scope=scope)


def test_batch_hook_registers_once_and_counts_each_block_once(batch_vm):
    vms, run = batch_vm
    deltas = []
    registrations = []
    removed = []
    fired = []

    @contextmanager
    def scope(ql):
        handle = ql.hook_block(lambda _ql, address, _size: fired.append(address))
        registrations.append(handle)
        boundaries = []
        try:
            yield lambda: boundaries.append(len(fired))
        finally:
            boundaries.append(len(fired))
            deltas.extend(b - a for a, b in pairwise(boundaries))
            ql.hook_del(handle)
            removed.append(handle)

    outs = _batch([{"x": 1}] * 4, scope)
    assert [o["ret"] for o in outs] == [1] * 4
    assert deltas[0] > 0 and deltas == [deltas[0]] * 4
    assert len(registrations) == 1 and removed == registrations
    before = len(fired)
    run(vms[0], 0, [], {"x": 1})
    assert len(fired) == before  # removal is effective, not merely invoked


def test_batch_hook_reset_observes_restored_registers_and_memory(batch_vm):
    observed = []

    @contextmanager
    def scope(ql):
        yield lambda: observed.append(
            (ql.arch.regs.eax, bytes(ql.mem.read(ql.os.entry_point + 0x100, 1)))
        )

    _batch([{"x": 1}] * 3, scope)
    assert observed == [(0, b"\0")] * 3


def test_batch_hook_resets_edges_between_cases(batch_vm):
    cases = [{"x": 0}, {"x": 1}, {"x": 0}]
    independent_bits = bytearray(MASK)
    independent_hits = 0
    for case in cases:
        scope, bits, hits = _hook_recorder()
        _batch([case], scope)
        independent_bits[:] = bytes(a | b for a, b in zip(independent_bits, bits))
        independent_hits += hits[0]
    scope, bits, hits = _hook_recorder()
    _batch(cases, scope)
    assert bits == independent_bits
    assert hits[0] == independent_hits > 0


@pytest.mark.parametrize("fallback", [True, None])
def test_batch_fallback_instruments_each_fresh_vm(batch_vm, monkeypatch, fallback):
    vms, run = batch_vm
    cases = [{"x": 0}, {"x": 1}]
    scope, bits, hits = _hook_recorder()
    snapshot = _batch(cases, scope)
    want_bits, want_hits = bytes(bits), hits[0]
    vms.clear()
    monkeypatch.setattr(calling, "_fn_has_syscall", lambda *_: fallback)
    scope, bits, hits = _hook_recorder()
    lifecycle = []

    @contextmanager
    def tracked(ql):
        lifecycle.append("attach")
        with scope(ql) as reset:

            def before_case():
                lifecycle.append("reset")
                reset()

            yield before_case
        lifecycle.append("detach")

    fresh = _batch(cases, tracked)
    assert len(vms) == len(cases) and vms[0] is not vms[1]
    assert all(o["batch_mode"] == "fresh-vm-fallback" for o in fresh)
    assert [{**o, "batch_mode": "batched-snapshot"} for o in fresh] == snapshot
    assert bytes(bits) == want_bits and hits[0] == want_hits > 0
    assert lifecycle == ["attach", "reset", "detach"] * len(cases)
    for ql in vms:
        run(ql, 0, [], {"x": 1})
    assert hits[0] == want_hits  # both VM attachments were actually removed


@pytest.mark.parametrize(
    "stage", ["normal", "fault", "timeout", "restore", "reset", "run"]
)
def test_batch_hook_scope_cleans_up_on_every_exit(batch_vm, monkeypatch, stage):
    vms, run = batch_vm
    fired, unrelated = [], []
    restored = []

    def fail(*_args):
        raise RuntimeError(stage)

    @contextmanager
    def scope(ql):
        ql.hook_block(lambda _ql, addr, _size: unrelated.append(addr))
        handle = ql.hook_block(lambda _ql, addr, _size: fired.append(addr))
        if stage == "restore":
            restored.append(ql.restore)
            ql.restore = fail
        try:
            yield fail if stage == "reset" else lambda: None
        finally:
            ql.hook_del(handle)
            if restored:
                ql.restore = restored[0]

    if stage == "run":
        monkeypatch.setattr(calling, "_run_case", fail)
    elif stage in ("fault", "timeout"):
        monkeypatch.setattr(calling, "_run_case", lambda *_: {"exit_code": -1})
    if stage in ("restore", "reset", "run"):
        with pytest.raises(RuntimeError, match=stage):
            _batch([{"x": 1}], scope)
    else:
        _batch([{"x": 1}], scope)
    before = len(fired), len(unrelated)
    run(vms[0], 0, [], {"x": 1})
    assert len(fired) == before[0]
    assert len(unrelated) > before[1]  # cleanup preserves hooks it does not own


@pytest.mark.parametrize("case_count", [1, 2])
def test_batch_hook_callback_error_fails_measurement(batch_vm, monkeypatch, case_count):
    vms, run = batch_vm
    # Trigger an error in the actual recorder callback. Mimic _run_case's
    # guest-fault conversion to prove the scope still rejects this measurement.
    scope, bits, _ = _hook_recorder()
    bits.clear()

    def swallow(ql, addr, params, case):
        try:
            return run(ql, addr, params, case)
        except Exception:  # noqa: BLE001 - mirror the driver's target-fault conversion
            return {"exit_code": -1}

    monkeypatch.setattr(calling, "_run_case", swallow)
    with pytest.raises(RuntimeError, match="coverage hook failed") as error:
        _batch([{"x": 1}] * case_count, scope)
    assert isinstance(error.value.__cause__, IndexError)
    run(vms[0], 0, [], {"x": 1})  # faulty callback was removed


def test_empty_batch_does_not_create_hook_scope(batch_vm):
    vms, _ = batch_vm

    def fail(_ql):
        pytest.fail("empty batch created a hook scope")

    assert _batch([], fail) == []
    assert not vms


def test_coverage_spike_trials_have_independent_recorders(batch_vm):
    args = ("fixture", 0, [], [{"x": 0}, {"x": 1}])
    _, plain, no_stats = _timed(args)
    _, first, stats1 = _timed(args, instrumented=True)
    _, second, stats2 = _timed(args, instrumented=True)
    assert first == second == plain
    assert no_stats is None
    assert stats1 == stats2 and stats1["block_hits"] > 0


@pytest.mark.parametrize("cases", [[], [{"x": 1}]])
def test_coverage_spike_rejects_empty_or_fallback_measurements(
    batch_vm, monkeypatch, cases
):
    monkeypatch.setattr(calling, "_fn_has_syscall", lambda *_: True)
    with pytest.raises(AssertionError, match="nonempty snapshot batch"):
        _timed(("fixture", 0, [], cases), instrumented=True)
