"""Compile agent C model; replay canonical traces; structured accept/reject."""

from __future__ import annotations

import random
import string
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from ..driver import podrun
from ..exec.canonical import canonicalize
from ..exec.recorder import record

CFLAGS = ["gcc", "-O1", "-static", "-fno-pie", "-no-pie", "-g0"]


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    divergence: dict | None = None


def compile_model(c_source: str, out: Path) -> tuple[bool, str]:
    out.with_suffix(".c").write_text(c_source)  # debug artifact lands in the task dir
    try:
        r = podrun.run_worker(
            {
                "mode": "compile",
                "jobs": [
                    {
                        "c_source": c_source,
                        "out": out.name,
                        "compiler": "gcc",
                        "flags": CFLAGS[1:],
                    }
                ],
            },
            out.parent,
        )
    except RuntimeError as e:  # missing podman/image: mandatory containment
        return False, f"compile infra: {e}"
    if "stage" in r:
        return False, f"compile infra: {r['detail']}"
    res = r["results"][0]
    return res["rc"] == 0, res["stderr"]


# Observable channel: write-family + exit_group only. Address tokens are wildcards:
# stored traces canonicalize over the FULL event stream at record time, so a model
# whose libc mallocs/brks differently ordinals buffers differently in write args —
# a false divergence on address-shaped args carrying no semantics (stdout bytes are
# compared separately). Kept: sc + fd + count, exit_group status, write exit result
# (bytes written — small literal, meaningful).
OBS = ("write", "writev", "exit_group")
ADDR_PREFIX = "ADDR_"


def _shown(hexstr: str) -> str:
    # latin-1: any byte sequence decodes; previews are for humans, hex stays authoritative.
    return bytes.fromhex(hexstr).decode("latin-1")


def _obs_events(trace: dict) -> list[dict]:
    evs = []
    for e in trace["events"]:
        if e["sc"] not in OBS:
            continue
        e = dict(e)
        e["args"] = ["ADDR_*" if a.startswith(ADDR_PREFIX) else a for a in e["args"]]
        evs.append(e)
    return evs


# Backward dependency slicing (research slot 2B-6): attach a small fd-linked
# chain (fd-producing open-family syscall + the writes that followed it) to
# event-divergence and files-mismatch payloads. io-mismatch stays slice-less
# (stdout errors are already covered by decoded previews + fault markers).
_OPEN_FAMILY = ("openat", "open", "creat")


def _fd_table(events: list[dict]) -> dict[str, str]:
    """Literal fd -> FD_<n> for write-intent opens, mirroring canonicalize's walk."""
    from ..exec.canonical import _write_intent

    fds: dict[str, str] = {}
    pending = False
    for e in events:
        sc, phase = e["sc"], e["phase"]
        if sc in _OPEN_FAMILY and phase == "enter":
            pending = _write_intent(sc, e["args"])
        elif sc in _OPEN_FAMILY and phase == "exit":
            res = e.get("result")
            if pending and isinstance(res, str) and res.startswith("0x"):
                fds.setdefault(res, f"FD_{len(fds)}")
            pending = False
    return fds


def _dep_slice(trace: dict, focus_index: int) -> list[dict] | None:
    """Chain for an event-divergence focus: opener pair for the focus fd, then
    every write event on that fd up to (and incl) the focus, capped at 6."""
    evs = trace["events"]
    if not (0 <= focus_index < len(evs)):
        return None
    focus = evs[focus_index]
    fd = focus["args"][0] if focus["args"] else None
    if fd is None:
        return None
    inv = {v: k for k, v in _fd_table(evs).items()}
    literal = inv.get(fd, fd)  # canonical FD_<n> -> original literal
    from ..exec.canonical import _write_intent

    # fd reuse: an earlier read-only open can return the same literal the real
    # write-open reuses — the anchor must be the WRITE-INTENT open, not any
    # syscall that happened to yield the same fd number (keeps collecting: the
    # LAST qualifying opener before the focus owns the fd at divergence time).
    chain, enter = [], None
    for e in evs[: focus_index + 1]:
        if e["sc"] in _OPEN_FAMILY and e["phase"] == "enter":
            enter = e if _write_intent(e["sc"], e["args"]) else None
        elif (
            e["sc"] in _OPEN_FAMILY
            and e["phase"] == "exit"
            and e.get("result") == literal
            and enter is not None
        ):
            chain = [enter, e]
    chain.extend(
        e
        for e in evs[: focus_index + 1]
        if e["sc"] in ("write", "writev") and e["args"] and e["args"][0] == fd
    )
    return (chain or None) and chain[-6:]


def _dep_slice_for_files(trace: dict) -> list[dict] | None:
    """Chain for a files_written mismatch: the fd-producing open + its writes.

    ponytail: last write-intent open is the file in question (works for the
    single-writer corpus; a multi-file seed needs path-aware slicing later).
    """
    evs, table = trace["events"], _fd_table(trace["events"])
    if not table:
        return None
    literal, fd = next(reversed(list(table.items())))  # last write-open
    from ..exec.canonical import _write_intent

    chain, enter = [], None
    for e in evs:
        if e["sc"] in _OPEN_FAMILY and e["phase"] == "enter":
            enter = e if _write_intent(e["sc"], e["args"]) else None
        elif (
            e["sc"] in _OPEN_FAMILY
            and e["phase"] == "exit"
            and e.get("result") == literal
            and enter is not None
        ):
            chain = [enter, e]
    chain.extend(
        e
        for e in evs
        if e["sc"] in ("write", "writev") and e["args"] and e["args"][0] == fd
    )
    return (chain or None) and chain[-6:]


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
            divergence = {
                "argv": argv,
                "expected": {
                    "stdout": tr["stdout"],
                    "stderr": tr["stderr"],
                    "exit_code": tr["exit_code"],
                    "stdout_decoded": _shown(tr["stdout"]),
                    "stderr_decoded": _shown(tr["stderr"]),
                },
                "actual": {
                    "stdout": got["stdout"],
                    "stderr": got["stderr"],
                    "exit_code": got["exit_code"],
                    "stdout_decoded": _shown(got["stdout"]),
                    "stderr_decoded": _shown(got["stderr"]),
                },
            }
            if got["exit_code"] == -1 and got["events"]:
                # Model crashed/timed out: surface the fault marker (where it died).
                divergence["actual_fault"] = got["events"][-1]
            return Verdict(False, "io-mismatch", divergence)
        if got["files_written"] != tr["files_written"]:
            # File channel: path set + content hex compared byte-exact. Hex is
            # authoritative (files may be binary) — no decoded previews here.
            divergence = {
                "argv": argv,
                "expected": tr["files_written"],
                "actual": got["files_written"],
            }
            sl = _dep_slice_for_files(tr)
            if sl:
                divergence["dep_slice"] = sl
            return Verdict(False, "files-mismatch", divergence)
        ge, te = _obs_events(got), _obs_events(tr)
        for i, (e, a) in enumerate(
            zip(te, ge)
        ):  # te=stored(expected), ge=model(actual)
            if a != e:
                divergence = {
                    "argv": argv,
                    "first_diverging_event_index": i,
                    "expected": e,
                    "actual": a,
                }
                full_idx = next(
                    pos
                    for pos, n in zip(
                        (j for j, x in enumerate(tr["events"]) if x["sc"] in OBS),
                        range(len(te)),
                    )
                    if n == i
                )
                sl = _dep_slice(tr, full_idx)
                if sl:
                    divergence["dep_slice"] = sl
                return Verdict(False, "event-divergence", divergence)
        if len(ge) != len(te):
            return Verdict(
                False,
                "event-length",
                {"argv": argv, "expected_len": len(te), "actual_len": len(ge)},
            )
    return Verdict(True)


_CHARSET = (
    string.ascii_letters
    + string.digits
    + string.punctuation.replace('"', "").replace("\\", "")
)


def hidden_input_stream(
    rng: random.Random, modes: tuple
) -> Iterator[tuple[list[str], bytes]]:
    """Endless candidate inputs from the task input space.

    Byte-domain stdin ("stdin-bytes"): raw random bytes, every draw guaranteed to
    contain a NUL and a >=0x80 byte — text/C-string overfits must fail reliably
    (random CONTENT keeps draws unguessable; the corner bytes are by construction).
    """
    while True:
        if "stdin-bytes" in modes:
            body = bytearray(rng.randrange(256) for _ in range(rng.randint(4, 48)))
            nul, hi = rng.sample(range(len(body)), 2)  # distinct: corner bytes survive
            body[nul] = 0
            body[hi] = rng.randrange(0x80, 0x100)
            yield [], bytes(body)
            continue
        s = "".join(rng.choice(_CHARSET) for _ in range(rng.randint(1, 24)))
        yield ([s], b"") if "argv" in modes else ([], (s + "\n").encode())


def gen_hidden_inputs(
    task_id: str, n: int = 8, modes: tuple = ("argv",), seed: str | None = None
) -> list[tuple[list[str], bytes]]:
    """Deterministic fresh inputs given a seed; submissions pass fresh entropy."""
    rng = random.Random(seed if seed is not None else f"hidden:{task_id}")
    return list(islice(hidden_input_stream(rng, modes), n))
