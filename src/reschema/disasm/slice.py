"""Disassemble one function from a binary using manifest address + symbol size."""

from __future__ import annotations

from pathlib import Path

import capstone
from elftools.elf.elffile import ELFFile


def disasm_function(binary: str | Path, addr: int, size: int) -> str:
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
    blob = data[off : off + size]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    return "\n".join(
        f"0x{i.address:x}:\t{i.mnemonic}\t{i.op_str}" for i in md.disasm(blob, addr)
    )
