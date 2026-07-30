"""Normalize legitimately-divergent values in recorded traces before diffing."""

from __future__ import annotations

import re
from pathlib import Path

ADDR = re.compile(r"0x[0-9a-f]{6,}")


def _mapper(prefix):
    m: dict[str, str] = {}

    def f(s: str) -> str:
        if s not in m:
            m[s] = f"{prefix}_{len(m)}"
        return m[s]

    return f


def canonicalize(trace: dict) -> dict:
    addr_of = _mapper("ADDR")
    evs = []
    for e in trace["events"]:
        e = dict(e)
        e["args"] = [addr_of(a) if ADDR.fullmatch(a) else a for a in e["args"]]
        if (
            "result" in e
            and isinstance(e["result"], str)
            and ADDR.fullmatch(e["result"])
        ):
            e["result"] = addr_of(e["result"])
        evs.append(e)
    t = dict(trace)
    t["events"] = evs
    t["argv"] = [Path(t["argv"][0]).name, *t["argv"][1:]]
    return t
