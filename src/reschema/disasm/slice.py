"""Disassemble one function from a binary using manifest address + .text bounds."""

from __future__ import annotations

from pathlib import Path

import capstone
from elftools.elf.elffile import ELFFile


def disasm_function(binary: str | Path, addr: int, max_bytes: int = 256) -> str:
    with open(binary, "rb") as f:
        elf = ELFFile(f)
        text = elf.get_section_by_name(".text")
        base, data = text["sh_addr"], text.data()
    off = addr - base
    blob = data[off : off + max_bytes]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    lines = []
    # stop after first `ret` following 3 insns (dead-simple function bound)
    for seen, i in enumerate(md.disasm(blob, addr), 1):
        lines.append(f"0x{i.address:x}:\t{i.mnemonic}\t{i.op_str}")
        if i.mnemonic == "ret" and seen >= 3:
            break
    return "\n".join(lines)
