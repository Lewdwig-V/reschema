"""Run a static ELF under qiling; capture observable trace."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from qiling import Qiling
from qiling.const import QL_INTERCEPT, QL_VERBOSE

LOG_SYSCALLS = ("read", "write", "writev", "openat", "close", "brk", "mmap", "exit_group")


class Sink(io.RawIOBase):
    def __init__(self):
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        return len(b)


def record(binary: str | Path, argv: list[str], stdin: bytes = b"", timeout_us: int = 3_000_000) -> dict:
    """argv excludes the binary; trace includes both. Returns a JSON-serializable dict."""
    out, err = Sink(), Sink()
    # qiling 1.4.6 adjustments vs Task 3 plan:
    # - Qiling() has no stdin/stdout/stderr kwargs; assign ql.os.std* after construction
    #   (QlOsPosix setters rewire the fd table).
    # - verbose=0 accepted, but console=False is what silences logger chatter.
    ql = Qiling([str(binary), *argv], "/", verbose=QL_VERBOSE.OFF, console=False)
    ql.os.stdin = io.BytesIO(stdin)
    ql.os.stdout = out
    ql.os.stderr = err
    events = []

    def _hook(name, phase):
        def h(ql, *args):
            raw = list(args)
            e = {"phase": phase, "sc": name}
            if phase == "exit":
                # qiling 1.4.6: EXIT hooks receive (ql, *params, retval) — last arg is the
                # syscall return value (plan assumed ql.reg.rax; that reads garbage here).
                e["args"] = [_fmt(a) for a in raw[:-1]]
                e["result"] = _fmt(raw[-1]) if raw else None
            else:
                e["args"] = [_fmt(a) for a in raw]
            events.append(e)
            # implicit None return == "no param override" for qiling ENTER hooks
        return h

    for sc in LOG_SYSCALLS:
        ql.os.set_syscall(sc, _hook(sc, "enter"), QL_INTERCEPT.ENTER)
        ql.os.set_syscall(sc, _hook(sc, "exit"), QL_INTERCEPT.EXIT)
    try:
        ql.run(timeout=timeout_us)
        exit_code = ql.os.exit_code if ql.os.exit_code is not None else 0
    except Exception as e:  # noqa: BLE001 - any emulation fault is trace data, not a recorder crash
        exit_code = -1
        events.append({"phase": "fault", "sc": "crash", "args": [repr(e)]})
    return {
        "argv": [str(binary), *argv],
        "stdin_hex": stdin.hex(),
        "stdin_sha256": hashlib.sha256(stdin).hexdigest(),
        "stdout": bytes(out.buf).hex(),
        "stderr": bytes(err.buf).hex(),
        "exit_code": exit_code,
        "files_written": {},  # v1: seeds write no files; spec reserves the field
        "events": events,
    }


def _fmt(a):
    return hex(a) if isinstance(a, int) else str(a)
