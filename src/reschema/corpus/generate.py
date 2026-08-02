"""Build the synthetic corpus matrix; write manifest with function addresses.

All compiles run inside the pinned toolchain image (driver/podrun), so corpus
binaries carry identical toolchain/libc encodings on every machine — the
darwin/CI/local gcc matrix is pinned, not ambient.

Targeted builds (seed_ids/matrix): merge into manifest.json in canonical
(full-build) order, updating only their slots, so a targeted slot's entry is
byte-identical to its full-build counterpart.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from elftools.elf.elffile import ELFFile

from ..driver import podrun
from ..exec.canonical import CANONICALIZER_VERSION

ROOT = Path(__file__).resolve().parents[3]
SEEDS = ROOT / "src" / "reschema" / "corpus" / "seeds"
OUT_ROOT = ROOT / ".reschema" / "corpus"

FUNCS = {
    "rot13": ["rot13_char", "rot13"],
    "check": ["pw_hash", "check_pw"],
    "calc": ["clamp_i32", "sum_range", "scale_buf"],
    "filewrite": ["xform_byte"],
}
COMPILERS = ["gcc", "clang"]
OPTS = ["-O0", "-O1", "-O2"]

# Direct-jump emulation can't read the TLS canary (fs:0x28) — a future seed with
# a local array would crash under the call driver.
CFLAGS = ["-static", "-fno-pie", "-no-pie", "-g0", "-fno-stack-protector"]


def _symtab(binary: Path) -> dict[str, tuple[int, int]]:
    with open(binary, "rb") as f:
        sym = ELFFile(f).get_section_by_name(".symtab")
        if not sym:
            return {}
        return {
            s.name: (int(s["st_value"]), int(s["st_size"]))
            for s in sym.iter_symbols()
            if s["st_info"]["type"] == "STT_FUNC"
        }


def _slot_sort_key(task_id: str) -> tuple:
    seed, rest = task_id.split("::")
    cc, opt, variant = rest.split("-")
    return (
        seed,
        COMPILERS.index(cc),
        OPTS.index("-" + opt),
        ["sym", "stripped"].index(variant),
    )


def build(
    out_root: Path = OUT_ROOT,
    *,
    seed_ids: list[str] | None = None,
    matrix: list[str] | None = None,
) -> list[dict]:
    """Build corpus slots. `seed_ids` filters seed names; `matrix` filters
    '<cc>-<opt>-<sym|stripped>' slot selectors; None means the full matrix.
    Returns the entries BUILT (binaries -> manifest written last)."""
    jobs, plan = [], []
    for seed in sorted(SEEDS.glob("*.c")):
        name = seed.stem
        if seed_ids is not None and name not in seed_ids:
            continue
        for cc in COMPILERS:
            for opt in OPTS:
                for strip in (False, True):
                    variant = "stripped" if strip else "sym"
                    if (
                        matrix is not None
                        and f"{cc}-{opt.lstrip('-')}-{variant}" not in matrix
                    ):
                        continue
                    out = out_root / f"{name}/{cc}-{opt.lstrip('-')}-{variant}"
                    out.mkdir(parents=True, exist_ok=True)
                    jobs.append(
                        {
                            "src_path": f"reschema/corpus/seeds/{seed.name}",
                            "out": str((out / "prog").relative_to(out_root)),
                            "compiler": cc,
                            "flags": [opt, *CFLAGS],
                        }
                    )
                    plan.append((name, cc, opt, strip, out / "prog"))
    if not plan:
        return []
    r = podrun.run_worker({"mode": "compile", "jobs": jobs}, out_root, timeout=600)
    if "stage" in r:
        raise RuntimeError(f"corpus toolchain container failed: {r['detail']}")
    bad = [j for j in r["results"] if j["rc"] != 0]
    if bad:
        raise RuntimeError(
            f"corpus compile failed for {bad[0]['out']}: {bad[0]['stderr']}"
        )

    can_strip = shutil.which("strip") is not None
    if not can_strip:
        print("note: binutils `strip` not found, leaving binaries unstripped")
    built = []
    for name, cc, opt, strip, binary in plan:
        slot = f"{name}/{cc}-{opt.lstrip('-')}-{'stripped' if strip else 'sym'}"
        syms = _symtab(binary)
        funcs = {
            f: {"addr": syms[f][0], "size": syms[f][1]}
            for f in FUNCS[name]
            if f in syms
        }
        assert funcs, f"no functions captured for {slot}"
        if strip and can_strip:
            subprocess.run(["strip", "-s", str(binary)], check=True)
        built.append(
            {
                "seed": name,
                "compiler": cc,
                "opt": opt,
                "stripped": strip,
                "binary": str(binary),
                "task_id": f"{name}::{cc}-{opt.lstrip('-')}-{'stripped' if strip else 'sym'}",
                "functions": funcs,
            }
        )
    mf_path = out_root / "manifest.json"
    manifest = (
        {x["task_id"]: x for x in json.loads(mf_path.read_text())}
        if mf_path.exists()
        else {}
    )
    for x in built:
        manifest[x["task_id"]] = x
    mf_path.write_text(
        json.dumps(
            [manifest[k] for k in sorted(manifest, key=_slot_sort_key)], indent=2
        )
    )
    # Machine-checkable canonicalizer stamp: engine refuses to load a corpus
    # recorded under different sanitizer rules (rules change = corpus re-record).
    (out_root / "canonicalizer_version").write_text(CANONICALIZER_VERSION)
    return built


if __name__ == "__main__":
    m = build()
    print(f"{len(m)} corpus slots")
