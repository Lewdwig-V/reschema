"""The program gate's host side: double-recorded ground truth, the hidden
suite, and `compare` (the model's canonical traces vs ground truth). The model
itself is compiled and replayed only in a job (checks/program_replay.py)."""

from __future__ import annotations

import random
import string
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..exec.canonical import _mapper, _write_intent, canonicalize
from ..exec.recorder import record

HIDDEN_N = 8  # distinct usable hidden inputs each submission must survive
# ponytail: manifest-driven input-space deferred
# Hidden-input mode per stdin-driven seed (unlisted seeds take argv);
# "stdin-bytes" = stdin in the RAW byte domain (binary-safe seeds).
INPUT_MODE = {"check": "stdin", "filewrite": "stdin-bytes", "pkfmt": "stdin-bytes"}
CFLAGS = ["-O1", "-static", "-fno-pie", "-no-pie", "-g0"]  # gcc


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    divergence: dict | None = None


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
    fd_of = _mapper("FD")
    fds: dict[str, str] = {}
    pending = False
    for e in events:
        sc, phase = e["sc"], e["phase"]
        if sc in _OPEN_FAMILY and phase == "enter":
            pending = _write_intent(sc, e["args"])
        elif sc in _OPEN_FAMILY and phase == "exit":
            res = e.get("result")
            if pending and isinstance(res, str) and res.startswith("0x"):
                fds.setdefault(res, fd_of(res))
            pending = False
    return fds


def _chain(evs: list[dict], literal: str, fd: str) -> list[dict] | None:
    """Opener pair for `literal` + every write on `fd`, capped at the last 6.

    fd reuse: an earlier read-only open can return the same literal the real
    write-open reuses — the anchor must be the WRITE-INTENT open, not any
    syscall that happened to yield the same fd number (keeps collecting: the
    LAST qualifying opener owns the fd)."""
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
    return _chain(evs[: focus_index + 1], literal, fd)


def _dep_slice_for_files(trace: dict) -> list[dict] | None:
    """Chain for a files_written mismatch: the fd-producing open + its writes.

    ponytail: last write-intent open is the file in question (works for the
    single-writer corpus; a multi-file seed needs path-aware slicing later).
    """
    table = _fd_table(trace["events"])
    if not table:
        return None
    literal, fd = next(reversed(list(table.items())))  # last write-open
    return _chain(trace["events"], literal, fd)


def _io(t: dict) -> dict:
    return {
        "stdout": t["stdout"],
        "stderr": t["stderr"],
        "exit_code": t["exit_code"],
        "stdout_decoded": _shown(t["stdout"]),
        "stderr_decoded": _shown(t["stderr"]),
    }


def compare(traces: list[dict], got_traces: Iterable[dict]) -> Verdict:
    """Stored traces vs the model's canonical traces, pairwise in order; the
    first divergence decides. Also judges traces replayed elsewhere (the M8
    program-replay checker's in-job replay)."""
    for tr, got in zip(traces, got_traces, strict=False):
        argv = tr["argv"][1:]  # argv[0] is the original's path; model uses its own
        if (
            got["stdout"] != tr["stdout"]
            or got["stderr"] != tr["stderr"]
            or got["exit_code"] != tr["exit_code"]
        ):
            divergence = {"argv": argv, "expected": _io(tr), "actual": _io(got)}
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
                # the i-th observable event's index in the FULL stored stream
                full_idx = [j for j, x in enumerate(tr["events"]) if x["sc"] in OBS][i]
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


def _pkfmt_grammar_packet(rng: random.Random) -> bytes:
    """pkfmt seed-grammar generator (codex P1 on #125): construction
    knowledge of the format's own shape, so hidden suites on this seed see
    real packets with appreciable probability. 60% of draws are
    built from these with structured variety: real magic, version range,
    records 0-2, len fields matching payloads, plus minority attack-variants
    — flipped magic, out-of-range versions, and overclaimed record lengths
    (the truncation class). Draws never precede recorded cases semantically:
    they're fresh entropy, just grammar-shaped."""
    magic_bad = rng.random() < 0.15
    ver = rng.choice((2, 3, 3, 4, 4, 5))  # range [2..4] valid; ~1/6 bad
    n = rng.randint(0, 2)
    pkt = [0xB1, 0x5A, ver, n]
    for index in range(n):
        ln = rng.randint(0, 6)
        declared = ln
        # Overclaim only the last payload: no following record's bytes can
        # accidentally satisfy its length. Keep the actual payload short.
        if index == n - 1 and rng.random() < 0.18:
            declared += rng.randint(8, 32)
        pkt += [rng.choice((0x20, 0x30)), declared & 0xFF, (declared >> 8) & 0xFF]
        pkt += [rng.randrange(256) for _ in range(ln)]
    if magic_bad:
        pkt[0] ^= 0xFF
    return bytes(pkt)


# Per-seed hidden grammar overrides. Seeds with real wire formats get the
# submission-hardening they need here (hidden_input_stream looks them up by name);
# seeds without one take the uniform stream, unchanged.
_SEED_GRAMMARS = {"pkfmt": _pkfmt_grammar_packet}


def hidden_input_stream(
    rng: random.Random, modes: tuple, seed: str | None = None
) -> Iterator[tuple[list[str], bytes]]:
    """Endless candidate inputs from the task input space.

    Byte-domain stdin ("stdin-bytes"): raw random bytes, every draw guaranteed to
    contain a NUL and a >=0x80 byte — text/C-string overfits must fail reliably
    (random CONTENT keeps draws unguessable; the corner bytes are by construction).

    Grammar-aware seeds (_SEED_GRAMMARS): 60% of draws come from the seed's
    own packet generator (fresh entropy, structured variety, attack variants),
    interleaved with the uniform stream so robustness coverage does not die.
    """
    grammar = _SEED_GRAMMARS.get(seed) if seed else None
    while True:
        if grammar is not None and rng.random() < 0.6:
            yield [], grammar(rng)
            continue
        if "stdin-bytes" in modes:
            body = bytearray(rng.randrange(256) for _ in range(rng.randint(4, 48)))
            nul, hi = rng.sample(range(len(body)), 2)  # distinct: corner bytes survive
            body[nul] = 0
            body[hi] = rng.randrange(0x80, 0x100)
            yield [], bytes(body)
            continue
        s = "".join(rng.choice(_CHARSET) for _ in range(rng.randint(1, 24)))
        yield ([s], b"") if "argv" in modes else ([], (s + "\n").encode())


def record_stable(binary: str | Path, argv: list[str], stdin: bytes) -> dict | None:
    """Double-record ground truth; canonical trace, or None on flaky divergence."""
    a = canonicalize(record(binary, argv, stdin))
    return a if a == canonicalize(record(binary, argv, stdin)) else None


def hidden_suite(
    binary: str | Path, seed_name: str, rec: list[dict], hidden_seed: str
) -> list[dict]:
    """Up to HIDDEN_N distinct hidden cases drawn from `hidden_seed`, ground
    truth double-recorded on `binary`, none repeating a `rec` input; fewer
    than HIDDEN_N means the draw starved."""
    modes = (INPUT_MODE.get(seed_name, "argv"),)
    known = {(tuple(t["argv"][1:]), t["stdin_hex"]) for t in rec}
    # A flaky or crashing draw invalidates the INPUT (redraw), never the
    # model: a crash trace is not a behavior spec.
    rng = random.Random(hidden_seed)
    fresh = []
    for (argv, stdin), _attempt in zip(
        hidden_input_stream(rng, modes, seed=seed_name),
        range(10 * HIDDEN_N),
        strict=False,
    ):
        if len(fresh) == HIDDEN_N:
            break
        key = (tuple(argv), stdin.hex())
        if key in known:
            continue
        known.add(key)  # dedupes both against recorded cases and within the suite
        t = record_stable(binary, argv, stdin)
        if t is None or t["exit_code"] == -1:
            continue
        fresh.append(t)
    return fresh
