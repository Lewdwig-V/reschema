"""Build the synthetic corpus matrix; write manifest with function addresses."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[3]
SEEDS = ROOT / "src" / "reschema" / "corpus" / "seeds"
OUT_ROOT = ROOT / ".reschema" / "corpus"

FUNCS = {
    "rot13": ["rot13_char", "rot13"],
    "check": ["pw_hash", "check_pw"],
    "calc": ["clamp_i32", "sum_range", "scale_buf"],
}
COMPILERS = ["gcc", "clang"]
OPTS = ["-O0", "-O1", "-O2"]


def _symtab(binary: Path) -> dict[str, int]:
    with open(binary, "rb") as f:
        sym = ELFFile(f).get_section_by_name(".symtab")
        if not sym:
            return {}
        return {
            s.name: int(s["st_value"])
            for s in sym.iter_symbols()
            if s["st_info"]["type"] == "STT_FUNC"
        }


def build(out_root: Path = OUT_ROOT) -> list[dict]:
    manifest = []
    for seed in sorted(SEEDS.glob("*.c")):
        name = seed.stem
        for cc in COMPILERS:
            if not shutil.which(cc):
                continue
            for opt in OPTS:
                for strip in (False, True):
                    slot = f"{name}/{cc}-{opt.lstrip('-')}-{'stripped' if strip else 'sym'}"
                    out = out_root / slot
                    out.mkdir(parents=True, exist_ok=True)
                    binary = out / "prog"
                    cmd = [
                        cc, opt, "-static", "-fno-pie", "-no-pie", "-g0",
                        str(seed), "-o", str(binary),
                    ]
                    subprocess.run(cmd, check=True)
                    funcs = {f: syms[f] for f in FUNCS[name] if f in (syms := _symtab(binary))}
                    if strip:
                        subprocess.run(["strip", "-s", str(binary)], check=True)
                    manifest.append({
                        "seed": name,
                        "compiler": cc,
                        "opt": opt,
                        "stripped": strip,
                        "binary": str(binary),
                        "task_id": f"{name}::{cc}-{opt.lstrip('-')}-{'stripped' if strip else 'sym'}",
                        "functions": funcs,
                    })
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    m = build()
    print(f"{len(m)} corpus slots")
