"""Disassemble one function from a binary using manifest address + symbol size."""

from __future__ import annotations

from pathlib import Path

from .analyze import function_insns


def disasm_function(binary: str | Path, addr: int, size: int) -> str:
    return "\n".join(
        f"0x{i.address:x}:\t{i.mnemonic}\t{i.op_str}"
        for i in function_insns(str(binary), addr, size)
    )
