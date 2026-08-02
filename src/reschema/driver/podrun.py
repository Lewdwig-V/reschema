"""Host-side spawn of the podman toolchain worker + mandatory image guard.

One pinned image for everything binary: corpus seeds (gcc+clang matrix), all
model compiles (level A + B), and level-B native execution happen ONLY inside
one-shot rootless containers (spec 2026-08-02-level-b-containment). Missing
podman/image is a hard refusal, never a native fallback.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

IMAGE = "localhost/reschema-toolchain:1"
BUILD_CMD = "podman build -t localhost/reschema-toolchain:1 -f Containerfile ."

# src/ mounts here so the in-image python imports the checkout's worker module.
_SRC = Path(__file__).resolve().parents[2]


def ensure_image() -> None:
    if (
        subprocess.run(
            ["podman", "image", "exists", IMAGE], capture_output=True, check=False
        ).returncode
        != 0
    ):
        raise RuntimeError(f"level-B worker image missing; build it: {BUILD_CMD}")


def run_worker(job: dict, workdir: Path, timeout: int = 240) -> dict:
    """One validation round inside a throwaway rootless container; returns result JSON."""
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
