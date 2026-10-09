"""Crash-gap census (roadmap "level B never compares inputs on which the
original faults") — measurement ONLY, no gate change.

validate/function.py skips every case where the ORIGINAL faults. Before any
ADR on comparing fault-or-not, measure what those skips are: genuine original
behavior, or harness/spec artifacts. Per function slot, two specs:

- ref:    the true signature (pointers as buffer/cstring), default ranges
- mistyped: every param declared i32, ret i32 (pointers -> register junk).
  NOT the agent-facing `_abi_template`: that gives void functions a memory
  channel (codex P2 on #140). This is a spec an agent can declare by hand,
  i.e. the attack surface; for non-void pointer functions it coincides with
  the template.

Cases are built exactly as the gate builds them (gen_inputs + 109-A scout
merge, pinned seed). Faulting cases are re-run once: a fault that does not
recur is nondeterminism, which would make any fault-or-not compare unsound.

stdout: one JSON line per (task, func, spec). stderr: per-spec totals.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter

from reschema.driver.calling import batch_call_original, gen_inputs
from reschema.driver.spec import Param
from reschema.engine import load_manifest
from reschema.validate.function import N_FUZZ
from reschema.validate.scout import merge_scout_cases, scout_inputs, scrape_immediates

SEED = 0xC4A5
I, CS = "i32", "cstring"
REF = {
    "clamp_i32": [Param("v", I), Param("lo", I), Param("hi", I)],
    "sum_range": [Param("lo", I), Param("hi", I)],
    "scale_buf": [
        Param("buf", "buffer_i32", "in_out", "n", ret="void"),
        Param("n", I),
        Param("factor", I),
    ],
    "rot13_char": [Param("c", I)],
    "rot13": [Param("in_out", CS, "in_out", ret="void")],
    "pw_hash": [Param("s", CS)],
    "check_pw": [Param("s", CS)],
    "xform_byte": [Param("b", I), Param("i", I)],
    "pk_version_ok": [Param("v", I)],
}


def _mistyped(params: list[Param]) -> list[Param]:
    return [Param(p.name, I) for p in params]


def _fault(out: dict) -> str:
    ev = out["events"][-1]
    return "timeout" if ev["sc"] == "timeout" else ev["args"][0].split(" (")[0]


def census(task: dict, func: str, info: dict, params: list[Param]) -> dict:
    binary, addr = task["binary"], info["addr"]
    cases = gen_inputs(params, random.Random(SEED), N_FUZZ)
    imms = scrape_immediates(binary, addr, info.get("size") or 0x400)
    cases = merge_scout_cases(cases, scout_inputs(params, imms), N_FUZZ)
    outs = batch_call_original(binary, addr, params, cases)
    bad = [(c, o) for c, o in zip(cases, outs) if o["exit_code"] == -1]
    again = (
        batch_call_original(binary, addr, params, [c for c, _ in bad]) if bad else []
    )
    flaky = sum(
        (a["exit_code"] != -1) or _fault(a) != _fault(o)
        for (_, o), a in zip(bad, again)
    )
    return {
        "skipped": len(bad),
        "n": len(cases),
        "faults": dict(Counter(_fault(o) for _, o in bad)),
        "nondeterministic": flaky,
    }


def main() -> int:
    totals: dict[str, Counter] = {}
    for task in load_manifest():
        for func, info in task["functions"].items():
            for spec, params in (
                ("ref", REF[func]),
                ("mistyped", _mistyped(REF[func])),
            ):
                row = census(task, func, info, params)
                print(
                    json.dumps(
                        {"task": task["task_id"], "func": func, "spec": spec, **row}
                    )
                )
                t = totals.setdefault(spec, Counter())
                t["slots"] += 1
                t["cases"] += row["n"]
                t["skipped"] += row["skipped"]
                t["slots_with_skips"] += row["skipped"] > 0
                t["nondeterministic"] += row["nondeterministic"]
    for spec, t in totals.items():
        print(f"{spec}: {dict(t)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
