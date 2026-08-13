"""Heuristic function facts for task_open: callees, arity guess, returning hint.

Everything here is a LABELED GUESS: coarse capstone evidence shaping, not a
contract — the agent still declares the falsifiable param spec; the engine's
semantics are unchanged. Rules were validated against the full corpus matrix
(48 build slots x seed functions with known call graphs):

- callees: exact (direct `call` rel32 targets resolved via manifest addresses).
- arity: arg-register GROUPS (an arg register and its narrower aliases, e.g.
  rdi/edi/di/dil) READ (purely, not read-write) before any write into the
  group, scanning the preamble up to the first call site; plus pass-through
  credit at the first resolved call for callee-arg groups never written here
  (tail-call/thin-wrapper shapes like check_pw). Read-write ops are excluded
  so implicit-instruction scratch (cdq/idiv edx) can't fake an arg.
- returning hint: an eax-group write in any basic block ending in ret (value
  materialized at the epilogue) OR a leading reg/imm-sourced eax write in the
  first 4 instructions (accumulator-init shapes, e.g. clang -O2 sum_range).
"""

from __future__ import annotations

from pathlib import Path

import capstone
from capstone import CS_AC_READ, CS_AC_WRITE
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_OP_REG
from elftools.elf.elffile import ELFFile

# SysV int-arg register alias groups, in argument order (NOT "families" —
# ARCHITECTURE.md reserves that word for seed-level task families).
BAREGS = (
    ("rdi", "edi", "di", "dil"),
    ("rsi", "esi", "si", "sil"),
    ("rdx", "edx", "dx", "dl"),
    ("rcx", "ecx", "cx", "cl"),
    ("r8", "r8d", "r8w", "r8b"),
    ("r9", "r9d", "r9w", "r9b"),
)
FAM_IDX = {r: i for i, g in enumerate(BAREGS) for r in g}
RAX = ("rax", "eax", "ax", "al", "ah")
BRANCHY = (
    "jmp",
    "je",
    "jne",
    "jg",
    "jge",
    "jl",
    "jle",
    "ja",
    "jae",
    "jb",
    "jbe",
    "js",
    "jns",
    "jp",
    "jnp",
    "jo",
    "jno",
    "jecxz",
    "jrcxz",
)

LABELED = "heuristic guess — declare the falsifiable param spec yourself"


def function_insns(binary: str, addr: int, size: int) -> list:
    with open(binary, "rb") as f:
        elf = ELFFile(f)
        text = elf.get_section_by_name(".text")
        base, data = text["sh_addr"], text.data()
    off = addr - base
    if not 0 <= off < len(data) or off + size > len(data):
        raise ValueError(
            f"{binary}: addr 0x{addr:x} size 0x{size:x} outside .text "
            f"[0x{base:x}, 0x{base + len(data):x})"
        )
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    return list(md.disasm(data[off : off + size], addr))


def disasm_function(binary: str | Path, addr: int, size: int) -> str:
    """Disassemble one function from a binary using manifest address + symbol size."""
    return "\n".join(
        f"0x{i.address:x}:\t{i.mnemonic}\t{i.op_str}"
        for i in function_insns(str(binary), addr, size)
    )


def _direct_fams(ii: list) -> tuple[set, set]:
    """Arg alias groups pure-read before written, preamble (pre-first-call)."""
    fam, written = set(), set()
    for ins in ii:
        if ins.mnemonic == "call":
            break
        for op in ins.operands:
            if op.type == X86_OP_REG:
                f = FAM_IDX.get(ins.reg_name(op.reg))
                if f is None:
                    continue
                if (
                    op.access & CS_AC_READ
                    and not op.access & CS_AC_WRITE
                    and f not in written
                ):
                    fam.add(f)
                if op.access & CS_AC_WRITE:
                    written.add(f)
            elif op.type == X86_OP_MEM and op.access & CS_AC_READ and op.mem.base:
                # pointer-arg dereference ([buf]) reads the base register
                f = FAM_IDX.get(ins.reg_name(op.mem.base))
                if f is not None and f not in written:
                    fam.add(f)
    return fam, written


def _callees(ii: list, addr2name: dict[int, str]) -> list[dict]:
    out = []
    for ins in ii:
        if (
            ins.mnemonic == "call"
            and ins.operands
            and ins.operands[0].type == X86_OP_IMM
        ):
            tgt = ins.operands[0].imm
            out.append({"address": hex(tgt), "name": addr2name.get(tgt)})
    return out


def _returns_hint(ii: list) -> bool:
    def rax_writes(ins) -> list:
        return [
            (
                ins.reg_name(op.reg),
                any(
                    o2.type == X86_OP_MEM and o2.access & CS_AC_READ
                    for o2 in ins.operands
                ),
            )
            for op in ins.operands
            if op.type == X86_OP_REG
            and op.access & CS_AC_WRITE
            and ins.reg_name(op.reg) in RAX
        ]

    block_start, r6 = 0, False
    for k, ins in enumerate(ii):
        if ins.mnemonic in BRANCHY or ins.mnemonic == "ret":
            if ins.mnemonic == "ret" and any(
                rax_writes(i2) for i2 in ii[block_start : k + 1]
            ):
                r6 = True
            block_start = k + 1
    # leading accumulator init: reg/imm-sourced rax write in the first 4 insns
    lead = any(not has_mem for ins in ii[:4] for _name, has_mem in rax_writes(ins))
    return r6 or lead


def analyze_function(binary: str, functions: dict[str, dict]) -> dict[str, dict]:
    """Facts for every manifest function of one binary (two passes: direct
    families first, then boundary credit from the first resolved callee)."""
    insns_of = {
        fn: function_insns(binary, f["addr"], f["size"]) for fn, f in functions.items()
    }
    addr2name = {f["addr"]: n for n, f in functions.items()}
    base_arity = {fn: len(_direct_fams(ii)[0]) for fn, ii in insns_of.items()}

    out = {}
    for fn, ii in insns_of.items():
        fam, written = _direct_fams(ii)
        for ins in ii:  # boundary pass-through credit at the first resolved call
            if (
                ins.mnemonic == "call"
                and ins.operands
                and ins.operands[0].type == X86_OP_IMM
            ):
                n = addr2name.get(ins.operands[0].imm)
                if n is not None:
                    fam |= {f for f in range(base_arity[n]) if f not in written}
                break
        out[fn] = {
            "arity_guess": len(fam),
            "returns_hint": _returns_hint(ii),
            "callees": _callees(ii, addr2name),
            "labeled": LABELED,
        }
    return out
