"""Invoke a function: original in qiling, model natively via ctypes.

Traps (qiling 1.4.6):
- SENTINEL is a mapped return-address trap: write it at [rsp], run until RIP == SENTINEL.
- Read stack pointer BEFORE any stack writes; ql.stack_write is rsp-relative, so set rsp first.
- 1.4.6 has no ql.os.stack_address / ql.reg — use ql.loader.stack_address / ql.arch.regs.
"""

from __future__ import annotations

import ctypes
import random
import struct

from qiling import Qiling

from .spec import Param

SENTINEL = 0x1000000  # mapped far from the 0x400000 static image base
BAREGS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]  # SysV int-arg order
TIMEOUT_US = 3_000_000


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
                ln = rng.randint(0, 16)
                case[p.name] = bytes(rng.randint(97, 122) for _ in range(ln)) + b"\0"
            elif p.kind == "buffer_i32":
                ln = case.get(p.length_param, rng.randint(1, 8))
                if p.direction != "out":
                    case[p.name] = [rng.randint(lo, hi) for _ in range(ln)]
                else:
                    case[p.name] = ln  # out buffer: length marker, allocated zeroed
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


def call_original(binary: str, addr: int, params: list[Param], case: dict) -> dict:
    """Function-level trace {"ret": i32, "mem": {...}} — schema load-bearing for validate/function."""
    ql = Qiling([binary], "/", verbose=0, console=False)
    ql.mem.map(SENTINEL, 0x1000)
    sp = ql.loader.stack_address  # initial stack top; read BEFORE any stack writes
    regs = ql.arch.regs
    ptrs = {}
    top = sp - 0x400  # buffers sit below the call frame; ~1KB of push room

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
    regs.rsp = sp  # set BEFORE ql.stack_write (it is rsp-relative)
    ql.stack_write(0, SENTINEL)
    ql.run(begin=addr, end=SENTINEL, timeout=TIMEOUT_US)
    out = {"ret": ctypes.c_int32(regs.rax).value, "mem": {}}
    for p in params:
        if p.kind == "buffer_i32" and p.name in ptrs:
            n = len(case[p.name]) if isinstance(case[p.name], list) else case[p.name]
            out["mem"][p.name] = list(
                struct.unpack(f"<{n}i", bytes(ql.mem.read(ptrs[p.name], 4 * n)))
            )
        if p.kind == "cstring" and p.direction != "in" and p.name in ptrs:
            out["mem"][p.name] = bytes(ql.mem.read(ptrs[p.name], len(case[p.name])))
    return out


def call_model_native(so_path: str, func: str, params: list[Param], case: dict) -> dict:
    lib = ctypes.CDLL(so_path)
    fn = getattr(lib, func)
    fn.restype = ctypes.c_int32
    keep = []
    args, watched = [], {}
    for p in params:
        if p.kind == "i32":
            args.append(ctypes.c_int32(case[p.name]))
        elif p.kind == "cstring":
            b = ctypes.create_string_buffer(bytes(case[p.name]), len(case[p.name]))
            keep.append(b)
            args.append(ctypes.cast(b, ctypes.c_char_p))
            watched[p.name] = (b, "cstring")
        elif p.kind == "buffer_i32":
            n = len(case[p.name]) if isinstance(case[p.name], list) else case[p.name]
            arr = (ctypes.c_int32 * n)(
                *(case[p.name] if isinstance(case[p.name], list) else [0] * n)
            )
            keep.append(arr)
            args.append(arr)
            watched[p.name] = (arr, "buffer_i32")
    out = {"ret": fn(*args), "mem": {}}
    for name, (buf, kind) in watched.items():
        # pure-input cstrings read back no mem: an effect of a read-only buffer
        # is not observable — mirrors call_original's mem keys exactly
        if kind == "cstring" and name in watched and _dir(params, name) == "in":
            continue
        out["mem"][name] = list(buf) if kind == "buffer_i32" else bytes(buf)
    return out


def _dir(params: list[Param], name: str) -> str:
    return next(p.direction for p in params if p.name == name)
