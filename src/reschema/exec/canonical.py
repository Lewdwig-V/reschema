"""Normalize legitimately-divergent values in recorded traces before diffing.

rules v2 (CANONICALIZER_VERSION below): address-shaped tokens (0x + >=6 hex) ->
ADDR_<n> ordinals (first sighting, trace-global); argv[0] -> basename; fds from
write-intent opens -> FD_<n> ordinals by first sighting (fds 0/1/2 stay ABI
literals; read-only opens never enter the table, so a model's extra read-open
cannot shift numbering), applied to fd-carrier events in both enter and exit
phases; absolute host paths inside string args -> PATH_<n>.
Spec section 8: a rule change here = corpus re-record (enforced via the version
sidecar next to the corpus manifest). 2.1: fd normalization of exit events too.
"""

from __future__ import annotations

import re
from pathlib import Path

CANONICALIZER_VERSION = "2.1"

ADDR = re.compile(r"0x[0-9a-f]{6,}")
ABSPATH = re.compile(r"/(?:[\w.-]+/)+[\w.-]+")

OPEN_FLAGS = {"openat": 2, "open": 1, "creat": -1}  # args index of the flags param
O_ACCMODE, O_CREAT = 0x3, 0x40
FD_CARRIERS = ("read", "write", "writev", "close")


def _mapper(prefix):
    m: dict[str, str] = {}

    def f(s: str) -> str:
        if s not in m:
            m[s] = f"{prefix}_{len(m)}"
        return m[s]

    return f


def _write_intent(sc: str, args: list[str]) -> bool:
    if sc == "creat":
        return True
    try:
        flags = int(args[OPEN_FLAGS[sc]], 16)
    except (IndexError, ValueError):
        return False
    return bool(flags & O_ACCMODE or flags & O_CREAT)


def canonicalize(trace: dict) -> dict:
    addr_of = _mapper("ADDR")
    fd_of = _mapper("FD")
    path_of = _mapper("PATH")
    fds: dict[str, str] = {}  # fd literal -> FD_<n>
    pending_write_open = False  # enter/exit pair (single-threaded static guests)
    evs = []
    for e in trace["events"]:
        e = dict(e)
        sc, phase = e["sc"], e["phase"]
        if sc in OPEN_FLAGS and phase == "enter":
            pending_write_open = _write_intent(sc, e["args"])
        elif sc in OPEN_FLAGS and phase == "exit":
            res = e.get("result")
            if pending_write_open and isinstance(res, str) and res.startswith("0x"):
                fds.setdefault(res, fd_of(res))
            pending_write_open = False
        if sc in FD_CARRIERS and e["args"]:  # enter AND exit share args[0]=fd
            a0 = e["args"][0]
            if a0 in fds:
                e["args"] = [fds[a0], *e["args"][1:]]
        e["args"] = [
            addr_of(a)
            if ADDR.fullmatch(a)
            else ABSPATH.sub(lambda m: path_of(m.group(0)), a)
            for a in e["args"]
        ]
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
