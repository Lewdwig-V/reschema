"""Static-immediates scout slice (#109-A): seeds carved from the ORIGINAL's
own .text, once per round — the binary is the pheromone.

The magic-value blind spot (#109): uniform `gen_inputs` over declared ranges
provably cannot reach sparse cmp-immediate branches, so a wrong-branch stub
can pass differential fuzzing. This module derives a small pool of immediate
constants by disassembling the function itself and reinterprets them as
per-kind fuzz cases merged into the SAME n_fuzz envelope. It is deliberately
memoryless (no cross-submission persistence, no coverage maps, no solvers):
freshness equals the entropy policy's own — the binary cannot collude with
the candidate it will be compared against.

Capstone immediates may exceed the declared Param.range: that is the point.
A cmp-immediate is evidence the declared range was wrong; the gate must test
beyond it. Everything else about the merged set (keep/skip, compare,
<2-distinct vacuity floor) runs through the identical paths as uniform draws.
"""

from __future__ import annotations

from capstone.x86_const import X86_OP_IMM

from ..disasm.analyze import function_insns
from ..driver.spec import Param

# Harness-owned budget knob (test seam): scout cases consume AT MOST this many
# positions of the n_fuzz envelope — and never more than a quarter of it.
SCOUT_SLICE_MAX = 16
# Mnemonics whose operand immediates act as comparison/decision thresholds.
# cmp/test are the equality relations proper; sub/add/imul feed the derived
# compare pattern compilers emit for them — seeds are evidence, not verdicts,
# so over-collection is safe (a dud seed costs one cheap case, nothing else).
_THRESH_MNEMONICS = ("cmp", "test", "sub", "add", "imul")


def scrape_immediates(binary: str, addr: int, size: int) -> list[int]:
    """uint32-normalized immediate constants from threshold-family insns in a
    function slice, first-seen order, deduped. Pure disassembly — no VM.
    The window is clamped at .text end (strict `function_insns` semantics
    without the failure mode — an overlong window is never a caller bug
    worth stopping a fuzz round for)."""
    from elftools.elf.elffile import ELFFile

    with open(binary, "rb") as f:
        text = ELFFile(f).get_section_by_name(".text")
        base, data = text["sh_addr"], text.data()
    off = addr - base
    if not 0 <= off < len(data):
        return []  # bogus entry point: an empty pool, never a raised fuzz round
    clamped = min(size, len(data) - off)
    if clamped <= 0:
        return []
    out, seen = [], set()
    for ins in function_insns(binary, addr, clamped):
        if ins.mnemonic not in _THRESH_MNEMONICS:
            continue
        for op in ins.operands:
            if op.type != X86_OP_IMM:
                continue
            v = op.imm & 0xFFFFFFFF
            if v in seen:
                continue
            seen.add(v)
            out.append(v)
    return out


def scout_inputs(params: list[Param], immediates: list[int]) -> list[dict]:
    """Per-immediate fuzz cases reinterpreted per Param kind:
    - i32: the needle (uint32); values >= 2^31 also appear signed
    - cstring: the needle's little-endian bytes, NUL-terminated
    - buffer_i32: the needle in cell 0 (length_param snapped to 1)
    """
    cases = []
    for raw in immediates:
        readings = [raw] if raw < 0x80000000 else [raw, raw - 0x100000000]
        for needle in readings:
            case = {}
            for p in params:
                if p.kind == "i32":
                    case[p.name] = needle
                elif p.kind == "cstring":
                    case[p.name] = raw.to_bytes(4, "little") + b"\0"
                elif p.kind == "buffer_i32":
                    case[p.name] = [needle]
            if case:
                cases.append(case)
    return cases


def scout_slice_budget(n_fuzz: int) -> int:
    """Harness-owned scout envelope: at most SCOUT_SLICE_MAX positions, and
    never more than a quarter of the round (uniform mixing must dominate)."""
    return min(SCOUT_SLICE_MAX, n_fuzz // 4)


def merge_scout_cases(
    uniform_cases: list[dict], scout_cases: list[dict], n_fuzz: int
) -> list[dict]:
    """Scout cases occupy the FIRST slice positions of the n_fuzz envelope;
    uniform draws keep their rng-derived sequence bit-identical for the rest.
    Scout count never exceeds scout_slice_budget(n_fuzz)."""
    k = min(scout_slice_budget(n_fuzz), len(scout_cases), n_fuzz)
    return scout_cases[:k] + uniform_cases[k:]
