"""Run a static ELF under qiling; capture observable trace.

Schema: files_written maps rootfs-relative paths to final content hex, scraped
from the record rootfs after the run (post-state, not syscall interception).

Containment: agent C is executed deliberately, so guest file ops must never reach
the host fs. The record runs against a fresh EMPTY rootfs holding only a copy of
the binary itself (qiling needs the binary inside the rootfs namespace: static
glibc readlinks /proc/self/exe at startup). Every host-mutating file op qiling
emulates (open/unlink/rename/mkdir/chmod/...) resolves inside the scratch dir,
which is discarded afterwards. Static binaries need nothing else from a rootfs.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tempfile
from pathlib import Path

from qiling import Qiling
from qiling.const import QL_INTERCEPT, QL_VERBOSE

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

    # Fresh EMPTY rootfs per record: contains ALL host-mutating file ops.
    with tempfile.TemporaryDirectory(prefix="reschema-rootfs-") as rootfs:
        guest = Path(rootfs) / Path(binary).name  # binary must live in the namespace
        try:
            shutil.copy2(binary, guest)
            ql = Qiling(
                [str(guest), *argv], rootfs, verbose=QL_VERBOSE.OFF, console=False
            )
            ql.os.stdin = io.BytesIO(stdin)
            ql.os.stdout = out
            ql.os.stderr = err
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
        # Post-state scrape: everything the guest left behind is the capture —
        # correct truncate/rename/multi-open semantics come free from real fs
        # behavior; only regular files count (the binary copy itself excluded).
        files_written = {
            str(p.relative_to(rootfs)): p.read_bytes().hex()
            for p in Path(rootfs).rglob("*")
            if p.is_file() and not p.is_symlink() and p != guest
        }
    return {
        "argv": [str(binary), *argv],
        "stdin_hex": stdin.hex(),
        "stdin_sha256": hashlib.sha256(stdin).hexdigest(),
        "stdout": bytes(out.buf).hex(),
        "stderr": bytes(err.buf).hex(),
        "exit_code": exit_code,
        "files_written": files_written,
        "events": events,
    }


def _fmt(a):
    return hex(a) if isinstance(a, int) else str(a)
