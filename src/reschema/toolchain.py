"""The pinned toolchain image: its ID, and the corpus build's container run.

One image for everything binary (see ARCHITECTURE.md): the corpus matrix and
`strip` run here through `run_build`; the checkers' jobs and the worker sandbox
run the same image through Warranted, pinned by `image_id()`. Agent C never
goes through `run_build`: it is compiled and run only in Warranted jobs.
Missing podman or image is a hard refusal, never a native fallback.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

IMAGE = "localhost/reschema-toolchain:1"
BUILD_CMD = "podman build -t localhost/reschema-toolchain:1 -f Containerfile ."

# src/ mounts here so the in-image python imports the checkout's worker module.
_SRC = Path(__file__).resolve().parents[1]


def _podman(*args: str, **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["podman", *args], check=False, **kw)
    except FileNotFoundError as e:
        raise RuntimeError("podman not installed; install podman (rootless)") from e


def image_id() -> str:
    """Content identity of the image behind the mutable IMAGE tag: what
    Warranted jobs require (a pinned image) and record in `jobs.json`.
    Uncached: a long-lived host must see a rebuild made while it runs."""
    p = _podman(
        "image", "inspect", "--format", "{{.Id}}", IMAGE, capture_output=True, text=True
    )
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"toolchain image missing; build it: {BUILD_CMD}")
    return p.stdout.strip()


def run_build(job: dict, workdir: Path, timeout: int = 600) -> dict:
    """One corpus build round (native_worker's compile/strip modes) in a
    throwaway rootless container; returns the worker's result JSON.

    Trusted inputs only: `workdir` is bind-mounted rw at /work, which is how
    the binaries come back (too large for a job's capped stdout)."""
    image_id()  # refuses early, with the build command, when the image is missing
    p = _podman(
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
        input=json.dumps(job).encode(),
        capture_output=True,
        timeout=timeout,
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
