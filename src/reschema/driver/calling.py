"""Invoke a function: original in qiling. (Model execution lives in the level-B
podman worker — see validate/function.py.)

Traps (qiling 1.4.6):
- SENTINEL is a mapped return-address trap: write it at [rsp], run until RIP == SENTINEL.
  SENTINEL == qiling's API_HOOK_MEM numerically, but that region is only mapped for
  API-hook drivers (Windows etc.); ours run plain Linux user ELFs and never map there.
- Read stack pointer BEFORE any stack writes; ql.stack_write is rsp-relative, so set rsp first.
- 1.4.6 has no ql.os.stack_address / ql.reg — use ql.loader.stack_address / ql.arch.regs.
"""

from __future__ import annotations

import ctypes
import random
import shutil
import struct
import tempfile
from pathlib import Path

from elftools.elf.elffile import ELFFile
from qiling import Qiling

from ..disasm.analyze import function_insns
from .spec import Param

SENTINEL = 0x1000000  # mapped far from the 0x400000 static image base
BAREGS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]  # SysV int-arg order
TIMEOUT_US = 3_000_000
# Syscall-executing opcodes (x86-64 Linux): any of these in the function slice
# disqualifies it from the shared-VM batch path.
SYSCALL_MNEMONICS = ("syscall", "sysenter")


def _guard_arity(params: list[Param]) -> None:
    if len(params) > len(BAREGS):
        raise NotImplementedError("stack args unsupported")


def gen_inputs(params: list[Param], rng: random.Random, n: int) -> list[dict]:
    cases = []
    for _ in range(n):
        case = {}
        # pass 1: scalars first so length_param values exist before buffers reference them
        for p in params:
            lo, hi = p.range
            if p.kind == "i32":
                case[p.name] = rng.randint(lo, hi)
        # pass 2: buffers/strings
        for p in params:
            lo, hi = p.range
            if p.kind == "cstring":
                # ponytail: cstring ignores p.range — the spec type has no string
                # char/length range fields; hardcoded a-z, len 0-16 until one exists.
                ln = rng.randint(0, 16)
                case[p.name] = bytes(rng.randint(97, 122) for _ in range(ln)) + b"\0"
            elif p.kind == "buffer_i32":
                ln = case.get(p.length_param, rng.randint(1, 8))
                # out buffers are poison-filled too: a genuine out-function overwrites
                # every cell; a no-op leaves the poison → mem mismatch. Zeroed fill was
                # how a no-op validated against a zero-preserving original.
                case[p.name] = [rng.randint(lo, hi) for _ in range(ln)]
        cases.append(case)
    return cases


def _marshal(p: Param, val, write):
    """write() allocs memory, returns pointer."""
    if p.kind == "i32":
        return val, None
    if p.kind == "cstring":
        return write(val), None
    if p.kind == "buffer_i32":
        n = len(val) if isinstance(val, list) else val  # int => out buffer of n elems
        data = struct.pack(f"<{n}i", *val) if isinstance(val, list) else b"\x00" * 4 * n
        return write(data), n
    raise ValueError(f"unknown param kind: {p.kind}")


def _boot_vm(binary: str) -> Qiling:
    """Scratch rootfs + in-namespace binary copy (recorder.py pattern): originals
    are trusted corpus code, but any file op they perform stays contained."""
    rf = tempfile.TemporaryDirectory(prefix="reschema-rootfs-")
    guest = Path(rf.name) / Path(binary).name
    shutil.copy2(binary, guest)
    ql = Qiling([str(guest)], rf.name, verbose=0, console=False)
    ql.mem.map(SENTINEL, 0x1000)
    # ponytail: ql owns rf via this attribute; cm self-cleans on CPython refcount
    # drop at the caller's exit (same lifetime the inline version relied on).
    ql._reschema_rf = rf
    return ql


def _run_case(ql: Qiling, addr: int, params: list[Param], case: dict) -> dict:
    """Marshal + run + readback for ONE case on an already-booted VM.

    Function-level trace {"ret": i32, "mem": {...}} — schema load-bearing for
    validate/function. On timeout/emulation fault, mirrors recorder.py's fault
    convention: exit_code -1 + a trailing {"phase": "fault", ...} event.

    Callers must guarantee pristine VM state (fresh boot or full snapshot restore);
    this function deliberately resets nothing itself."""
    sp = ql.loader.stack_address  # initial stack top; read BEFORE any stack writes
    regs = ql.arch.regs
    ptrs = {}
    top = sp  # buffers allocated downward from the stack top

    def write(data: bytes) -> int:
        nonlocal top
        top -= (len(data) + 15) & ~15
        ql.mem.write(top, data)
        return top

    regvals = []
    for p in params:
        v, _ = _marshal(p, case[p.name], write)
        regvals.append(v)
        if p.kind in ("buffer_i32", "cstring"):
            ptrs[p.name] = v
    for reg, v in zip(BAREGS, regvals):
        setattr(regs, reg, v)
    # rsp goes BELOW the lowest buffer + sentinel at [rsp]: the callee's frame descends from
    # rsp, so no frame of any size can reach the buffers above it. "% 16 == 8" = SysV entry
    # alignment (a real call leaves rsp ≡ 8 mod 16 after pushing the return address).
    rsp = top - ((top - 8) % 16)
    regs.rsp = rsp  # set BEFORE ql.stack_write (it is rsp-relative)
    ql.stack_write(0, SENTINEL)
    try:
        ql.run(begin=addr, end=SENTINEL, timeout=TIMEOUT_US)
    except Exception as e:  # noqa: BLE001 - emulation faults are trace data, not driver crashes
        return {
            "ret": 0,
            "mem": {},
            "exit_code": -1,
            # recorder.py convention: type name + message; qiling's QlErrorBase repr() recurses
            "events": [
                {"phase": "fault", "sc": "crash", "args": [f"{type(e).__name__}: {e}"]}
            ],
        }
    if regs.rip != SENTINEL:  # stopped anywhere but at the return trap = timed out
        return {
            "ret": 0,
            "mem": {},
            "exit_code": -1,
            "events": [{"phase": "fault", "sc": "timeout", "args": []}],
        }
    out = {
        "ret": ctypes.c_int32(regs.rax).value,
        "mem": {},
        "exit_code": 0,
        "events": [],
    }
    for p in params:
        if p.kind == "buffer_i32" and p.name in ptrs:
            n = len(case[p.name]) if isinstance(case[p.name], list) else case[p.name]
            out["mem"][p.name] = list(
                struct.unpack(f"<{n}i", bytes(ql.mem.read(ptrs[p.name], 4 * n)))
            )
        # Read back in ALL directions: a "pure-in" cstring that was written is a
        # mis-declared spec, and the mem mismatch is how the validator catches it.
        if p.kind == "cstring" and p.name in ptrs:
            out["mem"][p.name] = bytes(ql.mem.read(ptrs[p.name], len(case[p.name])))
    return out


def call_original(binary: str, addr: int, params: list[Param], case: dict) -> dict:
    """Fresh-VM trace for one case (see _run_case for schema/fault semantics)."""
    _guard_arity(params)
    return _run_case(_boot_vm(binary), addr, params, case)


def _fn_has_syscall(binary: str, addr: int) -> bool | None:
    """True/False on whether the function at addr executes a syscall opcode.

    None = unanswerable here: the slice is bounded by the binary's own symtab,
    so a stripped or zero-size symbol leaves no honest extent to scan."""
    with open(binary, "rb") as f:
        sym = ELFFile(f).get_section_by_name(".symtab")
        if not sym:
            return None
        size = next(
            (
                int(s["st_size"])
                for s in sym.iter_symbols()
                if s["st_info"]["type"] == "STT_FUNC" and int(s["st_value"]) == addr
            ),
            None,
        )
    if not size:
        return None
    return any(
        i.mnemonic in SYSCALL_MNEMONICS for i in function_insns(binary, addr, size)
    )


def batch_call_original(
    binary: str, addr: int, params: list[Param], cases: list[dict]
) -> list[dict]:
    """call_original for a whole case list; same per-case trace schema.

    Isolation (batched-snapshot): ql.save() after boot/SENTINEL map, ql.restore()
    before every case — registers AND memory (incl. fsbase/gsbase) return to the
    pristine post-init state, so case N cannot leave residue for case N+1. A
    fault only costs its own case: restore before the next case wipes the wedged
    emulation state.

    The snapshot spans registers+memory ONLY — rootfs contents, OS/fd objects
    and post-save mapped regions are outside it, so the contract holds for
    syscall-free kernels only. Guard: the function's symtab-bounded slice is
    scanned for syscall-executing opcodes; ANY hit (or an unboundable slice —
    stripped/zero-size symbol) reroutes the round to per-case fresh VMs. Each
    trace records which: batch_mode = "batched-snapshot" | "fresh-vm-fallback".
    """
    _guard_arity(params)
    if _fn_has_syscall(binary, addr) is not False:
        # Syscalling or unscannable original: per-case fresh VMs. (Fresh VMs
        # were the pre-batch behavior for every path — the suite already paid
        # this cost everywhere before #80, so the fallback has no new worst case.)
        outs = [call_original(binary, addr, params, c) for c in cases]
        for o in outs:
            o["batch_mode"] = "fresh-vm-fallback"
        return outs
    ql = _boot_vm(binary)
    snap = ql.save()
    outs = []
    for case in cases:
        ql.restore(snap)
        o = _run_case(ql, addr, params, case)
        o["batch_mode"] = "batched-snapshot"
        outs.append(o)
    return outs
