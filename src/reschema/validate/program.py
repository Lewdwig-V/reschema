"""Compile agent C model; replay canonical traces; structured accept/reject."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..exec.canonical import canonicalize
from ..exec.recorder import record

CFLAGS = ["gcc", "-O1", "-static", "-fno-pie", "-no-pie", "-g0"]


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    divergence: dict | None = None


def compile_model(c_source: str, out: Path) -> tuple[bool, str]:
    src = out.with_suffix(".c")
    src.write_text(c_source)
    p = subprocess.run(
        [*CFLAGS, str(src), "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return p.returncode == 0, p.stderr


# Observable channel: write-family + exit_group only. Address tokens are wildcards:
# stored traces canonicalize over the FULL event stream at record time, so a model
# whose libc mallocs/brks differently ordinals buffers differently in write args —
# a false divergence on address-shaped args carrying no semantics (stdout bytes are
# compared separately). Kept: sc + fd + count, exit_group status, write exit result
# (bytes written — small literal, meaningful).
OBS = ("write", "writev", "exit_group")
ADDR_PREFIX = "ADDR_"


def _obs_events(trace: dict) -> list[dict]:
    evs = []
    for e in trace["events"]:
        if e["sc"] not in OBS:
            continue
        e = dict(e)
        e["args"] = ["ADDR_*" if a.startswith(ADDR_PREFIX) else a for a in e["args"]]
        evs.append(e)
    return evs


def replay_against(model_bin: Path, traces: list[dict]) -> Verdict:
    for tr in traces:
        argv = tr["argv"][
            1:
        ]  # argv[0] is the original binary's path; model uses its own
        stdin = bytes.fromhex(tr["stdin_hex"])
        got = canonicalize(record(model_bin, argv, stdin))
        if (
            got["stdout"] != tr["stdout"]
            or got["stderr"] != tr["stderr"]
            or got["exit_code"] != tr["exit_code"]
        ):
            return Verdict(
                False,
                "io-mismatch",
                {
                    "argv": argv,
                    "expected": {
                        "stdout": tr["stdout"],
                        "stderr": tr["stderr"],
                        "exit_code": tr["exit_code"],
                    },
                    "actual": {
                        "stdout": got["stdout"],
                        "stderr": got["stderr"],
                        "exit_code": got["exit_code"],
                    },
                },
            )
        ge, te = _obs_events(got), _obs_events(tr)
        for i, (e, a) in enumerate(
            zip(te, ge)
        ):  # te=stored(expected), ge=model(actual)
            if a != e:
                return Verdict(
                    False,
                    "event-divergence",
                    {
                        "argv": argv,
                        "first_diverging_event_index": i,
                        "expected": e,
                        "actual": a,
                    },
                )
        if len(ge) != len(te):
            return Verdict(
                False,
                "event-length",
                {"argv": argv, "expected_len": len(te), "actual_len": len(ge)},
            )
    return Verdict(True)
