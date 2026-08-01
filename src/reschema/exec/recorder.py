"""Run a static ELF under qiling; capture observable trace.

Schema: files_written maps the guest's path (as written at the open call) to its
final content hex. Write-intent opens are redirected to in-memory buffers: agent
C is executed deliberately, so guest file writes must never reach the host fs.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from qiling import Qiling
from qiling.const import QL_INTERCEPT, QL_VERBOSE
from qiling.os.posix.syscall.fcntl import ql_syscall_open, ql_syscall_openat

# x86-64-linux guest open(2) flag values (v1 is x86-64-only, spec §12).
O_ACCMODE = 0x3
O_CREAT, O_TRUNC, O_APPEND = 0x40, 0x200, 0x400

LOG_SYSCALLS = (
    "read",
    "write",
    "writev",
    "open",
    "openat",
    "creat",
    "close",
    "brk",
    "mmap",
    "exit_group",
)


class _Captured(io.BytesIO):
    def close(self):
        pass  # keep content readable for files_written; qiling frees the fd slot itself


class Sink(io.RawIOBase):
    def __init__(self):
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        return len(b)


def record(
    binary: str | Path, argv: list[str], stdin: bytes = b"", timeout_us: int = 3_000_000
) -> dict:
    """argv excludes the binary; trace includes both. Returns a JSON-serializable dict."""
    out, err = Sink(), Sink()
    # qiling 1.4.6 adjustments vs Task 3 plan:
    # - Qiling() has no stdin/stdout/stderr kwargs; assign ql.os.std* after construction
    #   (QlOsPosix setters rewire the fd table).
    # - verbose=0 accepted, but console=False is what silences logger chatter.
    events = []
    written: dict[str, _Captured] = {}

    # ponytail: one byte position per PATH, shared across opens — correct for the
    # single-writer corpus seeds (per-open offset table if a seed ever needs it)
    def _capture_open(ql, path: int, flags: int) -> int:
        vpath = ql.os.utils.read_cstring(path)
        buf = written.setdefault(vpath, _Captured())
        if flags & O_APPEND:
            buf.seek(0, 2)  # O_APPEND: position at end
        else:
            buf.seek(0)
            if flags & O_ACCMODE and flags & O_TRUNC:
                buf.truncate(0)
        idx = next((i for i in range(len(ql.os.fd)) if ql.os.fd[i] is None), -1)
        if idx == -1:
            return -24  # -EMFILE
        ql.os.fd[idx] = buf
        return idx

    def _openat_sandbox(ql, dirfd: int, path: int, flags: int, mode: int) -> int:
        if flags & O_ACCMODE or flags & O_CREAT:
            return _capture_open(ql, path, flags)
        return ql_syscall_openat(ql, dirfd, path, flags, mode)

    def _open_sandbox(ql, filename: int, flags: int, mode: int) -> int:
        if flags & O_ACCMODE or flags & O_CREAT:
            return _capture_open(ql, filename, flags)
        return ql_syscall_open(ql, filename, flags, mode)

    def _creat_sandbox(ql, filename: int, mode: int) -> int:
        return _capture_open(ql, filename, 0x1 | O_CREAT | O_TRUNC)  # 0x1: O_WRONLY

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

    try:
        ql = Qiling([str(binary), *argv], "/", verbose=QL_VERBOSE.OFF, console=False)
        ql.os.stdin = io.BytesIO(stdin)
        ql.os.stdout = out
        ql.os.stderr = err
        for sc, h in (
            ("openat", _openat_sandbox),
            ("open", _open_sandbox),
            ("creat", _creat_sandbox),
        ):
            ql.os.set_syscall(sc, h)  # CALL replacement: sandbox write-intent opens
        for sc in LOG_SYSCALLS:
            ql.os.set_syscall(sc, _hook(sc, "enter"), QL_INTERCEPT.ENTER)
            ql.os.set_syscall(sc, _hook(sc, "exit"), QL_INTERCEPT.EXIT)
        ql.run(timeout=timeout_us)
        if any(e["phase"] == "enter" and e["sc"] == "exit_group" for e in events):
            exit_code = ql.os.exit_code
        else:
            # qiling 1.4.6: exit_code defaults to 0 (not None) on a timed-out run;
            # a static-glibc guest exits only via exit_group, so no enter event
            # means the run stopped prematurely — report failure, not exit 0.
            exit_code = -1
            events.append({"phase": "fault", "sc": "timeout", "args": []})
    except Exception as e:  # noqa: BLE001 - any load or emulation fault is trace data, not a recorder crash
        exit_code = -1
        # qiling's QlErrorBase init-recurses: repr() recurses forever, avoid it
        events.append(
            {"phase": "fault", "sc": "crash", "args": [f"{type(e).__name__}: {e}"]}
        )
    return {
        "argv": [str(binary), *argv],
        "stdin_hex": stdin.hex(),
        "stdin_sha256": hashlib.sha256(stdin).hexdigest(),
        "stdout": bytes(out.buf).hex(),
        "stderr": bytes(err.buf).hex(),
        "exit_code": exit_code,
        "files_written": {p: b.getvalue().hex() for p, b in written.items()},
        "events": events,
    }


def _fmt(a):
    return hex(a) if isinstance(a, int) else str(a)
