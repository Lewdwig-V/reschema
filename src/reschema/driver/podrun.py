"""Host-side spawn of the podman toolchain worker + mandatory image guard.

One pinned image for everything binary: corpus seeds (gcc+clang matrix), all
model compiles (level A + B), and level-B native execution happen ONLY inside
one-shot rootless containers (see ARCHITECTURE.md). Missing podman/image is a
hard refusal, never a native fallback.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from pathlib import Path

IMAGE = "localhost/reschema-toolchain:1"
BUILD_CMD = "podman build -t localhost/reschema-toolchain:1 -f Containerfile ."

# src/ mounts here so the in-image python imports the checkout's worker module.
_SRC = Path(__file__).resolve().parents[2]


def _podman_env() -> dict[str, str]:
    """Rootless podman keys its image store off XDG_DATA_HOME — which the 2C
    dogfood runner RELOCATES (HOME/XDG pins isolate opencode's config layers
    into the sandbox). Under that env, `podman image exists` consults a fresh
    empty store and would hard-refuse every level-B gate (and the pinned image
    is localhost/-only, so it can never be pulled to fix itself). The image is
    per-ACCOUNT infra — built once, shared across all slots and campaigns; it
    is not protocol state (that lives in RESCHEMA_HOME) — so resolve the real
    account home via getpwuid(2), never the ambient, pinnable $HOME."""
    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return {**os.environ, "XDG_DATA_HOME": str(real / ".local" / "share")}


def ensure_image() -> None:
    try:
        missing = (
            subprocess.run(
                ["podman", "image", "exists", IMAGE],
                capture_output=True,
                check=False,
                env=_podman_env(),
            ).returncode
            != 0
        )
    except FileNotFoundError as e:
        # No podman binary at all: still an actionable RuntimeError, never a bare
        # traceback up to the MCP 'internal' catch-all.
        raise RuntimeError(
            "podman not installed; install podman (rootless) for level-B work"
        ) from e
    if missing:
        raise RuntimeError(f"level-B worker image missing; build it: {BUILD_CMD}")


def run_worker(job: dict, workdir: Path, timeout: int | None = None) -> dict:
    """One validation round inside a throwaway rootless container; returns result JSON.

    MOUNT CONTRACT: workdir is bind-mounted rw as the container's only writable
    tree, and its contents are fully visible (readable) to agent-authored C —
    callers compiling/executing agent sources MUST pass a scratch dir holding
    nothing but the model source (traces/ledger/accepts never enter the mount).

    Timeout scales with the fuzz budget: N cases can each burn a full per-case
    budget inside the worker (compile may also run); a fixed cap would kill the
    container mid-report and turn valid crash verdicts into infra failures.
    """
    if timeout is None:
        from .native_worker import CASE_TIMEOUT_S

        timeout = 120 + len(job.get("cases", ())) * (CASE_TIMEOUT_S + 2)
    ensure_image()
    p = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m",
            "--memory",
            "1g",
            "--pids-limit",
            "128",
            "-v",
            f"{_SRC}:/app:ro",
            "-e",
            "PYTHONPATH=/app",
            "-v",
            f"{workdir.resolve()}:/work:rw",
            IMAGE,
        ],
        input=json.dumps(job).encode(),
        capture_output=True,
        timeout=timeout,
        check=False,
        env=_podman_env(),
    )
    if p.returncode != 0:
        return {
            "stage": "infra",
            "detail": f"container rc={p.returncode}: {p.stderr.decode(errors='replace')}",
        }
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {
            "stage": "infra",
            "detail": f"worker produced non-JSON stdout: {p.stdout[:400]!r}",
        }
